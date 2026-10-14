"""Run end-to-end tests for single PDK machine cells through OpenROAD and Factorio.

Each test does these steps:
1. Synthesizes a single-cell netlist with dedicated pins.
2. Runs OpenROAD floorplanning, pin placement, and detailed routing.
3. Verifies zero DRC violations in physical routing.
4. Compiles the routed DEF into a Factorio blueprint.
5. Simulates the blueprint in headless Factorio to verify physical crafting.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "foundry"))
sys.path.insert(0, str(REPO_ROOT / "flow"))
sys.path.insert(0, str(REPO_ROOT / "test"))

import check_blueprint
import def2bp
import lef
import liberty
import netlist
import pdk
import solve
from test_routing_primitives import run_factorio_simulation


def serialize_lua(obj: Any) -> str:
    """Serialize a Python data structure into valid Lua literal code."""
    if obj is None:
        return "nil"
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif isinstance(obj, (int, float)):
        return str(obj)
    elif isinstance(obj, str):
        return json.dumps(obj)
    elif isinstance(obj, (list, tuple)):
        return "{" + ", ".join(serialize_lua(x) for x in obj) + "}"
    elif isinstance(obj, dict):
        return (
            "{" + ", ".join(f"[{json.dumps(k)}] = {serialize_lua(v)}" for k, v in obj.items()) + "}"
        )
    raise TypeError(f"Cannot serialize {type(obj)} to Lua")


OPENROAD_IMAGE = "docker.io/openroad/orfs:latest"
REAL_USER_HOME = pwd.getpwuid(os.getuid()).pw_dir

TESTED_MACHINES: dict[str, str] = {
    "assembling-machine-1": "test_cell_assembling_machine_1",
    "assembling-machine-2": "test_cell_assembling_machine_2",
    "assembling-machine-3": "test_cell_assembling_machine_3",
    "stone-furnace": "test_cell_stone_furnace",
    "steel-furnace": "test_cell_steel_furnace",
    "electric-furnace": "test_cell_electric_furnace",
    "chemical-plant": "test_cell_chemical_plant",
    "oil-refinery": "test_cell_oil_refinery",
    "centrifuge": "test_cell_centrifuge",
    "rocket-silo": "test_cell_rocket_silo",
}


class TestMachineCells(unittest.TestCase):
    """Verify physical execution of PDK machine cells from OpenROAD to Factorio."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
        if not cls.fixture_path.is_file():
            raise unittest.SkipTest("Fixture dump-fixture.json not found.")

        try:
            cls.game_bin = check_blueprint.find_factorio_binary()
        except FileNotFoundError:
            raise unittest.SkipTest("Factorio binary not found.")

        if not shutil.which("podman"):
            raise unittest.SkipTest("podman command not found.")

        cls.data = solve.Data(str(cls.fixture_path))
        cls.pdk = pdk.Pdk(cls.data)

    def run_machine_cell_e2e(
        self,
        machine_name: str,
        recipe_name: str,
        input_feeds: dict[str, int],
        expected_output_item: str,
        expected_output_count: int,
        prefill_fluids: dict[str, float] | None = None,
        demand_rate: float = 60.0,
        ticks: int = 1200,
    ) -> None:
        """Execute single machine cell end-to-end through OpenROAD and Factorio."""
        with tempfile.TemporaryDirectory(dir="/tmp", prefix=f"cell-{machine_name}-") as tmp:
            work_dir = Path(tmp)

            # 1. Synthesize single-cell netlist
            allowed = {recipe_name: self.data.recipes[recipe_name]}
            nl = netlist.build(
                self.data,
                {expected_output_item: demand_rate},
                allowed,
                machine_override=machine_name,
            )
            plans = netlist.merge_plans_of(nl)
            split_plans = netlist.split_plans_of(nl)

            (work_dir / "netlist.v").write_text(netlist.emit_verilog(nl, plans, split_plans))
            (work_dir / "cells.v").write_text(netlist.emit_cells_v(nl))

            cells = list(nl.cells.values())
            lef.write_lef(self.pdk, cells, str(work_dir / "tech.lef"))
            liberty.write_liberty(cells, str(work_dir / "tech.lib"))

            fluids = [
                m for m in (nl.inputs + nl.outputs) if m in self.data.prototypes.get("fluid", {})
            ]
            sidecar = {
                "demand": {expected_output_item: demand_rate},
                "fluids": fluids,
                "inputs": nl.inputs,
                "outputs": nl.outputs,
                "cells": dict(nl.cells),
                "instances": [
                    {"name": i.name, "cell": i.cell_key, "recipe": i.recipe} for i in nl.instances
                ],
            }
            (work_dir / "netlist.json").write_text(json.dumps(sidecar, indent=2))

            # 2. Run OpenROAD physical placement and routing
            tcl_script = "/repo/flow/factorilog.tcl"
            cmd_orfs = [
                "podman",
                "run",
                "--rm",
                "-v",
                f"{REPO_ROOT}:/repo",
                "-v",
                f"{work_dir}:/work",
                "-w",
                "/work",
                OPENROAD_IMAGE,
                "bash",
                "-lc",
                f"source /OpenROAD-flow-scripts/env.sh >/dev/null && openroad -no_init -exit {tcl_script}",
            ]
            podman_env = {**os.environ, "TMPDIR": "/tmp", "HOME": REAL_USER_HOME}
            res_orfs = subprocess.run(cmd_orfs, env=podman_env, capture_output=True, text=True)
            self.assertEqual(
                res_orfs.returncode,
                0,
                f"OpenROAD execution failed:\nSTDOUT:\n{res_orfs.stdout}\nSTDERR:\n{res_orfs.stderr}",
            )

            # Verify 0 DRC violations
            drc_rpt = work_dir / "route_drc.rpt"
            if drc_rpt.is_file():
                drc_text = drc_rpt.read_text().strip()
                self.assertEqual(
                    drc_text, "", f"DRC violations detected in {machine_name}: {drc_text}"
                )

            def_path = work_dir / "factory.def"
            self.assertTrue(def_path.is_file(), "factory.def not generated by OpenROAD")

            # 3. Translate DEF to Factorio blueprint
            def_text = def_path.read_text()
            bp_string = def2bp.def_to_blueprint(def_text, sidecar, label=f"cell_{machine_name}")

            # Verify blueprint structure
            def_data = def2bp.parse_def(def_text)
            pins_data = def2bp.extract_pins(def_data, sidecar)
            in_pins = pins_data.get("inputs", [])
            out_pins = pins_data.get("outputs", [])
            if input_feeds:
                self.assertGreater(len(in_pins), 0, "No input pins found in DEF")
            self.assertGreater(len(out_pins), 0, "No output pins found in DEF")

            # 4. Physical simulation in headless Factorio
            feed_lua_lines = []
            for item_name, count in input_feeds.items():
                feed_lua_lines.append(f'storage.feed_targets["{item_name}"] = {count}')
                feed_lua_lines.append(f'storage.fed_counts["{item_name}"] = 0')

            prefill_lua_lines = []
            if prefill_fluids:
                for fluid_name, amount in prefill_fluids.items():
                    prefill_lua_lines.append(f"""
                    for _, ent in pairs(surface.find_entities()) do
                        if ent.insert_fluid then
                            ent.insert_fluid{{name = "{fluid_name}", amount = {amount}}}
                        end
                    end
                    """)

            init_lua = f"""
            game.forces.player.research_all_technologies()
            local inv = game.create_inventory(1)
            local stack = inv[1]
            stack.set_stack{{name = "blueprint", count = 1}}
            stack.import_stack("{bp_string}")

            local built = stack.build_blueprint{{
                surface = surface,
                force = game.forces.player,
                position = {{0, 0}},
                force_build = true
            }}

            local ghosts = surface.find_entities_filtered{{name = "entity-ghost"}}
            local min_gx, min_gy = 1000000, 1000000
            for _, g in pairs(ghosts) do
                if g.position.x < min_gx then min_gx = g.position.x end
                if g.position.y < min_gy then min_gy = g.position.y end
            end
            local bp_entities = stack.get_blueprint_entities() or {{}}
            local min_bx, min_by = 1000000, 1000000
            for _, e in pairs(bp_entities) do
                if e.position.x < min_bx then min_bx = e.position.x end
                if e.position.y < min_by then min_by = e.position.y end
            end
            local shift_x = math.floor((min_gx - min_bx) + 0.5)
            local shift_y = math.floor((min_gy - min_by) + 0.5)

            local revived = 0
            for _, g in pairs(ghosts) do
                if g.valid then
                    local _, ent = g.revive()
                    if ent and ent.valid then revived = revived + 1 end
                end
            end
            storage.revived = revived

            -- Supply electric power through adjacent EEI on each pole
            local poles = surface.find_entities_filtered{{name = "medium-electric-pole"}}
            for _, p in ipairs(poles) do
                surface.create_entity{{
                    name = "electric-energy-interface",
                    position = {{p.position.x, p.position.y - 1}},
                    force = "player"
                }}
            end

            -- Supply burner machines with fuel
            for _, ent in pairs(surface.find_entities()) do
                if ent.name == "steel-furnace" or ent.name == "stone-furnace" then
                    ent.insert{{name = "coal", count = 50}}
                end
            end
            {chr(10).join(prefill_lua_lines)}

            -- Map input pin belts and pipes
            storage.in_belts = {{}}
            storage.in_pipes = {{}}
            local input_pins_data = {serialize_lua(in_pins)}
            for _, pin in ipairs(input_pins_data) do
                local px = pin.x + shift_x
                local py = pin.y + shift_y
                local ents = surface.find_entities_filtered{{position = {{px, py}}}}
                for _, ent in pairs(ents) do
                    if ent.type == "transport-belt" or ent.type == "underground-belt" then
                        storage.in_belts[pin.name] = ent
                        break
                    elseif ent.name == "pipe" or ent.name == "pipe-to-ground" then
                        storage.in_pipes[pin.name] = ent
                        break
                    end
                end
            end

            -- Map output pin belts and setup output infinity-pipes for fluid outputs
            storage.out_belts = {{}}
            local output_pins_data = {serialize_lua(out_pins)}
            for _, pin in ipairs(output_pins_data) do
                local px = pin.x + shift_x
                local py = pin.y + shift_y
                if pin.type == "pipe" then
                    local p = surface.create_entity{{
                        name = "infinity-pipe",
                        position = {{px + 1, py}},
                        force = "player"
                    }}
                    if p then
                        p.set_infinity_pipe_filter({{name = pin.name, percentage = 0.0, mode = "at-most"}})
                    end
                else
                    local ents = surface.find_entities_filtered{{position = {{px, py}}}}
                    for _, ent in pairs(ents) do
                        if ent.type == "transport-belt" or ent.type == "underground-belt" then
                            storage.out_belts[pin.name] = ent
                            break
                        end
                    end
                end
            end

            storage.feed_targets = {{}}
            storage.fed_counts = {{}}
            {chr(10).join(feed_lua_lines)}
            storage.drained_count = 0
            """

            is_fluid = expected_output_item in self.data.prototypes.get("fluid", {})
            drain_lua = ""
            if not is_fluid:
                drain_lua = f"""
                -- Drain output items at output pin
                local out_belt = storage.out_belts["{expected_output_item}"]
                if out_belt and out_belt.valid then
                    for l = 1, 2 do
                        local line = out_belt.get_transport_line(l)
                        local count = line.get_item_count("{expected_output_item}")
                        if count > 0 then
                            line.remove_item({{name = "{expected_output_item}", count = count}})
                            storage.drained_count = storage.drained_count + count
                        end
                    end
                end
                """

            tick_lua = f"""
            -- Feed items onto input belts or fluids into input pipes
            if event.tick % 10 == 0 then
                for item_name, target in pairs(storage.feed_targets) do
                    local current = storage.fed_counts[item_name] or 0
                    if current < target then
                        local belt = storage.in_belts[item_name]
                        if belt and belt.valid then
                            local line = belt.get_transport_line(1)
                            if line.can_insert_at_back() then
                                line.insert_at_back({{name = item_name, count = 1}})
                                storage.fed_counts[item_name] = current + 1
                            end
                        else
                            local pipe = storage.in_pipes[item_name]
                            if pipe and pipe.valid then
                                local amt = math.min(50, target - current)
                                local inserted = pipe.insert_fluid{{name = item_name, amount = amt}}
                                if inserted and inserted > 0 then
                                    storage.fed_counts[item_name] = current + inserted
                                end
                            end
                        end
                    end
                end
            end

            {drain_lua}

            if event.tick == {ticks} then
                local surface = game.surfaces[1]
                local total = 0
                if {str(is_fluid).lower()} then
                    local fstats = game.forces.player.get_fluid_production_statistics(surface)
                    total = math.floor(fstats.get_input_count("{expected_output_item}"))
                else
                    local extra_count = 0
                    local m_ents = surface.find_entities_filtered{{name = "{machine_name}"}}
                    for _, m in ipairs(m_ents) do
                        if m.name == "rocket-silo" then
                            extra_count = extra_count + m.rocket_parts
                        end
                        local inv = m.get_output_inventory()
                        if inv then
                            extra_count = extra_count + inv.get_item_count("{expected_output_item}")
                        end
                    end
                    total = (storage.drained_count or 0) + extra_count
                end
                log(string.format("TEST_RESULT: {machine_name} total=%d revived=%d", total, storage.revived or 0))
            end
            """

            results = run_factorio_simulation(init_lua, tick_lua, ticks=ticks + 10)
            target_line = None
            for line in results:
                if f"TEST_RESULT: {machine_name}" in line:
                    target_line = line
                    break

            self.assertIsNotNone(
                target_line,
                f"No simulation results returned for {machine_name}. Log lines: {results}",
            )

            # Check revived count > 0 and total count >= expected
            m_total = re.search(r"total=(\d+)", target_line)
            m_revived = re.search(r"revived=(\d+)", target_line)
            self.assertIsNotNone(m_total, f"total count missing in {target_line}")
            self.assertIsNotNone(m_revived, f"revived count missing in {target_line}")
            total_count = int(m_total.group(1))
            revived_count = int(m_revived.group(1))
            self.assertGreater(revived_count, 0, f"No entities revived for {machine_name}")
            self.assertGreaterEqual(
                total_count,
                expected_output_count,
                f"Expected at least {expected_output_count} {expected_output_item}, got {total_count}. All results:\n"
                + "\n".join(results),
            )

    def test_cell_assembling_machine_1(self) -> None:
        """Test assembling machine 1 cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="assembling-machine-1",
            recipe_name="iron-gear-wheel",
            input_feeds={"iron-plate": 10},
            expected_output_item="iron-gear-wheel",
            expected_output_count=5,
            demand_rate=60.0,
            ticks=1200,
        )

    def test_cell_assembling_machine_2(self) -> None:
        """Test assembling machine 2 cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="assembling-machine-2",
            recipe_name="electronic-circuit",
            input_feeds={"iron-plate": 5, "copper-cable": 15},
            expected_output_item="electronic-circuit",
            expected_output_count=5,
            demand_rate=60.0,
            ticks=1200,
        )

    def test_cell_assembling_machine_3(self) -> None:
        """Test assembling machine 3 cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="assembling-machine-3",
            recipe_name="automation-science-pack",
            input_feeds={"copper-plate": 5, "iron-gear-wheel": 5},
            expected_output_item="automation-science-pack",
            expected_output_count=5,
            demand_rate=15.0,
            ticks=2400,
        )

    def test_cell_electric_furnace(self) -> None:
        """Test electric furnace cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="electric-furnace",
            recipe_name="iron-plate",
            input_feeds={"iron-ore": 5},
            expected_output_item="iron-plate",
            expected_output_count=5,
            demand_rate=30.0,
            ticks=1800,
        )

    def test_cell_steel_furnace(self) -> None:
        """Test steel furnace cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="steel-furnace",
            recipe_name="iron-plate",
            input_feeds={"iron-ore": 5},
            expected_output_item="iron-plate",
            expected_output_count=4,
            demand_rate=30.0,
            ticks=1800,
        )

    def test_cell_stone_furnace(self) -> None:
        """Test stone furnace cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="stone-furnace",
            recipe_name="copper-plate",
            input_feeds={"copper-ore": 5},
            expected_output_item="copper-plate",
            expected_output_count=4,
            demand_rate=15.0,
            ticks=2400,
        )

    def test_cell_chemical_plant(self) -> None:
        """Test chemical plant cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="chemical-plant",
            recipe_name="plastic-bar",
            input_feeds={"coal": 5, "petroleum-gas": 200},
            expected_output_item="plastic-bar",
            expected_output_count=5,
            demand_rate=60.0,
            ticks=1200,
        )

    def test_cell_oil_refinery(self) -> None:
        """Test oil refinery cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="oil-refinery",
            recipe_name="basic-oil-processing",
            input_feeds={"crude-oil": 200},
            expected_output_item="petroleum-gas",
            expected_output_count=45,
            demand_rate=60.0,
            ticks=1200,
        )

    def test_cell_centrifuge(self) -> None:
        """Test centrifuge cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="centrifuge",
            recipe_name="uranium-processing",
            input_feeds={"uranium-ore": 30},
            expected_output_item="uranium-238",
            expected_output_count=1,
            demand_rate=5.0,
            ticks=2200,
        )

    def test_cell_rocket_silo(self) -> None:
        """Test rocket silo cell end-to-end through OpenROAD and Factorio."""
        self.run_machine_cell_e2e(
            machine_name="rocket-silo",
            recipe_name="rocket-part",
            input_feeds={"processing-unit": 5, "low-density-structure": 5, "rocket-fuel": 5},
            expected_output_item="rocket-part",
            expected_output_count=1,
            demand_rate=20.0,
            ticks=1200,
        )

    def test_circuit_combinator_arithmetic(self) -> None:
        """Test arithmetic combinator cell with circuit connections."""
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="cell-combinator-") as tmp:
            work_dir = Path(tmp)

            # 1. Synthesize netlist with arithmetic combinator telemetry tap
            recipe_name = "iron-gear-wheel"
            allowed = {recipe_name: self.data.recipes[recipe_name]}
            nl = netlist.build(self.data, {recipe_name: 60.0}, allowed)
            plans = netlist.merge_plans_of(nl)
            split_plans = netlist.split_plans_of(nl)
            records = netlist.insert_telemetry(
                nl,
                tap_scope="iron-plate",
                enable_display=False,
                enable_circuit=True,
                merge_plans=plans,
            )

            (work_dir / "netlist.v").write_text(netlist.emit_verilog(nl, plans, split_plans))
            (work_dir / "cells.v").write_text(netlist.emit_cells_v(nl))

            cells = list(nl.cells.values())
            lef.write_lef(self.pdk, cells, str(work_dir / "tech.lef"))
            liberty.write_liberty(cells, str(work_dir / "tech.lib"))

            sidecar = {
                "demand": {recipe_name: 60.0},
                "fluids": [],
                "inputs": nl.inputs,
                "outputs": nl.outputs,
                "cells": dict(nl.cells),
                "instances": [
                    {"name": i.name, "cell": i.cell_key, "recipe": i.recipe} for i in nl.instances
                ],
                "telemetry": records,
            }
            (work_dir / "netlist.json").write_text(json.dumps(sidecar, indent=2))

            # 2. Run OpenROAD physical placement and routing with M1-M6 circuit layers
            tcl_script = "/repo/flow/factorilog.tcl"
            cmd_orfs = [
                "podman",
                "run",
                "--rm",
                "-v",
                f"{REPO_ROOT}:/repo",
                "-v",
                f"{work_dir}:/work",
                "-w",
                "/work",
                OPENROAD_IMAGE,
                "bash",
                "-lc",
                f"source /OpenROAD-flow-scripts/env.sh >/dev/null && openroad -no_init -exit {tcl_script}",
            ]
            podman_env = {**os.environ, "TMPDIR": "/tmp", "HOME": REAL_USER_HOME}
            res_orfs = subprocess.run(cmd_orfs, env=podman_env, capture_output=True, text=True)
            self.assertEqual(res_orfs.returncode, 0, f"OpenROAD failed:\n{res_orfs.stderr}")

            # Verify 0 DRC violations
            drc_rpt = work_dir / "route_drc.rpt"
            if drc_rpt.is_file():
                drc_text = drc_rpt.read_text().strip()
                self.assertEqual(drc_text, "", f"DRC violations in combinator cell: {drc_text}")

            def_path = work_dir / "factory.def"
            self.assertTrue(def_path.is_file(), "factory.def not generated by OpenROAD")

            # 3. Translate DEF to Factorio blueprint
            def_text = def_path.read_text()
            bp_string = def2bp.def_to_blueprint(def_text, sidecar, label="combinator_cell")

            # 4. Physical simulation in headless Factorio
            init_lua = f"""
            game.forces.player.research_all_technologies()
            local inv = game.create_inventory(1)
            local stack = inv[1]
            stack.set_stack{{name = "blueprint", count = 1}}
            stack.import_stack("{bp_string}")

            local built = stack.build_blueprint{{
                surface = surface,
                force = game.forces.player,
                position = {{0, 0}},
                force_build = true
            }}

            local ghosts = surface.find_entities_filtered{{name = "entity-ghost"}}
            local min_gx, min_gy = 1000000, 1000000
            for _, g in pairs(ghosts) do
                if g.position.x < min_gx then min_gx = g.position.x end
                if g.position.y < min_gy then min_gy = g.position.y end
            end
            local bp_entities = stack.get_blueprint_entities() or {{}}
            local min_bx, min_by = 1000000, 1000000
            for _, e in pairs(bp_entities) do
                if e.position.x < min_bx then min_bx = e.position.x end
                if e.position.y < min_by then min_by = e.position.y end
            end
            local shift_x = math.floor((min_gx - min_bx) + 0.5)
            local shift_y = math.floor((min_gy - min_by) + 0.5)

            local revived = 0
            for _, g in pairs(ghosts) do
                if g.valid then
                    local _, ent = g.revive()
                    if ent and ent.valid then revived = revived + 1 end
                end
            end
            storage.revived = revived

            local poles = surface.find_entities_filtered{{name = "medium-electric-pole"}}
            for _, p in ipairs(poles) do
                surface.create_entity{{
                    name = "electric-energy-interface",
                    position = {{p.position.x, p.position.y - 1}},
                    force = "player"
                }}
            end

            -- Identify input belt
            storage.in_belt = nil
            local def_data = {serialize_lua(def2bp.parse_def(def_text))}
            local in_pins = {serialize_lua(def2bp.extract_pins(def2bp.parse_def(def_text), sidecar).get("inputs", []))}
            for _, pin in ipairs(in_pins) do
                local px = pin.x + shift_x
                local py = pin.y + shift_y
                local ents = surface.find_entities_filtered{{position = {{px, py}}}}
                for _, ent in pairs(ents) do
                    if ent.type == "transport-belt" or ent.type == "underground-belt" then
                        storage.in_belt = ent
                        break
                    end
                end
            end
            storage.fed = 0
            """

            tick_lua = """
            if event.tick % 20 == 0 and storage.fed < 5 then
                local belt = storage.in_belt
                if belt and belt.valid then
                    local line = belt.get_transport_line(1)
                    if line.can_insert_at_back() then
                        line.insert_at_back({name = "iron-plate", count = 1})
                        storage.fed = storage.fed + 1
                    end
                end
            end

            if event.tick == 600 then
                local comb = game.surfaces[1].find_entities_filtered{name = "arithmetic-combinator"}[1]
                local sig = 0
                local en = 0
                if comb and comb.valid then
                    en = comb.energy
                    local net = comb.get_circuit_network(defines.wire_connector_id.combinator_output_green)
                    if net then
                        sig = net.get_signal({type = "item", name = "iron-plate"}) or 0
                    end
                end
                log(string.format("TEST_RESULT: COMBINATOR revived=%d en=%.0f sig=%d fed=%d",
                    storage.revived or 0, en, sig, storage.fed or 0))
            end
            """

            results = run_factorio_simulation(init_lua, tick_lua, ticks=610)
            target_line = None
            for line in results:
                if "TEST_RESULT: COMBINATOR" in line:
                    target_line = line
                    break

            self.assertIsNotNone(target_line, f"No combinator simulation result found: {results}")
            self.assertIn("sig=5", target_line)

    def test_cell_library_coverage(self) -> None:
        """Verify complete test coverage of all core machines in cell library."""
        all_machines = set(self.pdk.machines.keys())
        tested_machines = set(TESTED_MACHINES.keys())
        missing = all_machines - tested_machines
        if missing:
            raise AssertionError(
                f"Cell library coverage incomplete. Missing tests for machines: {sorted(missing)}"
            )

        unexpected = tested_machines - all_machines
        if unexpected:
            raise AssertionError(
                f"Tested machines contain unexpected non-library machines: {sorted(unexpected)}"
            )

        for machine, method_name in TESTED_MACHINES.items():
            if not hasattr(self, method_name) or not callable(getattr(self, method_name)):
                raise AssertionError(
                    f"Test method {method_name} for machine {machine} does not exist"
                )


if __name__ == "__main__":
    unittest.main()
