"""End-to-end tests for single PDK machine cells through OpenROAD and Factorio.

Tests individual machine cells through the full electronic design automation flow:
1. Synthesizes a single-cell netlist with dedicated input and output pins.
2. Runs OpenROAD floorplanning, pin placement, and detailed routing.
3. Verifies zero DRC violations in TritonRoute.
4. Compiles the routed DEF into a Factorio blueprint.
5. Simulates the blueprint in headless Factorio to verify physical crafting and item conservation.
"""

from __future__ import annotations

import json
import os
import pwd
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
        recipe_name: str,
        input_feeds: dict[str, int],
        expected_output_item: str,
        expected_output_count: int,
        demand_rate: float = 60.0,
        ticks: int = 1200,
    ) -> None:
        """Execute single machine cell end-to-end through OpenROAD and Factorio."""
        with tempfile.TemporaryDirectory(dir="/tmp", prefix=f"cell-{recipe_name}-") as tmp:
            work_dir = Path(tmp)

            # 1. Synthesize single-cell netlist
            allowed = {recipe_name: self.data.recipes[recipe_name]}
            nl = netlist.build(self.data, {recipe_name: demand_rate}, allowed)
            plans = netlist.merge_plans_of(nl)
            split_plans = netlist.split_plans_of(nl)

            (work_dir / "netlist.v").write_text(netlist.emit_verilog(nl, plans, split_plans))
            (work_dir / "cells.v").write_text(netlist.emit_cells_v(nl))

            cells = list(nl.cells.values())
            lef_text = lef.write_lef(self.pdk, cells, str(work_dir / "tech.lef"))
            lib_text = liberty.write_liberty(cells, str(work_dir / "tech.lib"))

            sidecar = {
                "demand": {recipe_name: demand_rate},
                "fluids": [],
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
                    drc_text, "", f"DRC violations detected in {recipe_name}: {drc_text}"
                )

            def_path = work_dir / "factory.def"
            self.assertTrue(def_path.is_file(), "factory.def not generated by OpenROAD")

            # 3. Translate DEF to Factorio blueprint
            def_text = def_path.read_text()
            bp_string = def2bp.def_to_blueprint(def_text, sidecar, label=f"cell_{recipe_name}")

            # Verify blueprint structure
            def_data = def2bp.parse_def(def_text)
            pins_data = def2bp.extract_pins(def_data, sidecar)
            in_pins = pins_data.get("inputs", [])
            out_pins = pins_data.get("outputs", [])
            self.assertGreater(len(in_pins), 0, "No input pins found in DEF")
            self.assertGreater(len(out_pins), 0, "No output pins found in DEF")

            # 4. Physical simulation in headless Factorio
            feed_lua_lines = []
            for item_name, count in input_feeds.items():
                feed_lua_lines.append(f'storage.feed_targets["{item_name}"] = {count}')
                feed_lua_lines.append(f'storage.fed_counts["{item_name}"] = 0')

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
            local min_gx, min_gy = 1e9, 1e9
            for _, g in pairs(ghosts) do
                if g.position.x < min_gx then min_gx = g.position.x end
                if g.position.y < min_gy then min_gy = g.position.y end
            end
            local bp_entities = stack.get_blueprint_entities() or {{}}
            local min_bx, min_by = 1e9, 1e9
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

            -- Supply electric power through adjacent EEI
            local poles = surface.find_entities_filtered{{name = "medium-electric-pole"}}
            if #poles > 0 then
                local ppos = poles[1].position
                surface.create_entity{{
                    name = "electric-energy-interface",
                    position = {{ppos.x, ppos.y - 1}},
                    force = "player"
                }}
            end

            -- Map input pin belts
            storage.in_belts = {{}}
            local input_pins_data = {serialize_lua(in_pins)}
            for _, pin in ipairs(input_pins_data) do
                local px = pin.x + shift_x
                local py = pin.y + shift_y
                local ents = surface.find_entities_filtered{{position = {{px, py}}}}
                for _, ent in pairs(ents) do
                    if ent.type == "transport-belt" or ent.type == "underground-belt" then
                        storage.in_belts[pin.name] = ent
                        break
                    end
                end
            end

            -- Map output pin belts
            storage.out_belts = {{}}
            local output_pins_data = {serialize_lua(out_pins)}
            for _, pin in ipairs(output_pins_data) do
                local px = pin.x + shift_x
                local py = pin.y + shift_y
                local ents = surface.find_entities_filtered{{position = {{px, py}}}}
                for _, ent in pairs(ents) do
                    if ent.type == "transport-belt" or ent.type == "underground-belt" then
                        storage.out_belts[pin.name] = ent
                        break
                    end
                end
            end
            for k, b in pairs(storage.in_belts) do
                log(string.format("TEST_RESULT: IN_PIN_FOUND: %s pos=%.1f,%.1f", k, b.position.x, b.position.y))
            end
            for k, b in pairs(storage.out_belts) do
                log(string.format("TEST_RESULT: OUT_PIN_FOUND: %s pos=%.1f,%.1f", k, b.position.x, b.position.y))
            end

            storage.feed_targets = {{}}
            storage.fed_counts = {{}}
            {chr(10).join(feed_lua_lines)}
            storage.drained_count = 0
            """

            tick_lua = f"""
            if event.tick == 1 then
                for k, b in pairs(storage.in_belts) do
                    log(string.format("TEST_RESULT: IN_PIN: %s pos=%.1f,%.1f", k, b.position.x, b.position.y))
                end
                for k, b in pairs(storage.out_belts) do
                    log(string.format("TEST_RESULT: OUT_PIN: %s pos=%.1f,%.1f", k, b.position.x, b.position.y))
                end
                local surface = game.surfaces[1]
                local machines = surface.find_entities_filtered{{type = "assembling-machine"}}
                for _, m in ipairs(machines) do
                    log(string.format("TEST_RESULT: M: recipe=%s en=%.0f status=%s",
                        tostring(m.get_recipe() and m.get_recipe().name), m.energy, tostring(m.status)))
                end
                local poles = surface.find_entities_filtered{{name = "medium-electric-pole"}}
                for _, p in ipairs(poles) do
                    log(string.format("TEST_RESULT: POLE: pos=%.1f,%.1f", p.position.x, p.position.y))
                end
                local eeis = surface.find_entities_filtered{{name = "electric-energy-interface"}}
                for _, e in ipairs(eeis) do
                    log(string.format("TEST_RESULT: EEI: pos=%.1f,%.1f en=%.0f", e.position.x, e.position.y, e.energy))
                end
                local inserters = surface.find_entities_filtered{{type = "inserter"}}
                for _, ins in ipairs(inserters) do
                    log(string.format("TEST_RESULT: INS: pos=%.1f,%.1f en=%.0f status=%s",
                        ins.position.x, ins.position.y, ins.energy, tostring(ins.status)))
                end
            end

            -- Feed items onto input belts
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
                        end
                    end
                end
            end

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

            if event.tick == {ticks} then
                log(string.format("TEST_RESULT: {recipe_name} revived=%d drained=%d",
                    storage.revived or 0, storage.drained_count or 0))
            end
            """

            results = run_factorio_simulation(init_lua, tick_lua, ticks=ticks + 10)
            target_line = None
            for line in results:
                if f"TEST_RESULT: {recipe_name}" in line:
                    target_line = line
                    break

            self.assertIsNotNone(
                target_line,
                f"No simulation results returned for {recipe_name}. Log lines: {results}",
            )

            # Check revived count > 0 and drained count >= expected
            self.assertIn(f"drained={expected_output_count}", target_line)

    def test_single_input_assembler_iron_gear_wheel(self) -> None:
        """An assembling machine crafts iron gear wheels from iron plates."""
        self.run_machine_cell_e2e(
            recipe_name="iron-gear-wheel",
            input_feeds={"iron-plate": 10},
            expected_output_item="iron-gear-wheel",
            expected_output_count=5,
            demand_rate=60.0,
            ticks=1200,
        )

    def test_two_input_assembler_automation_science_pack(self) -> None:
        """An assembling machine crafts automation science packs from copper plates and gears."""
        self.run_machine_cell_e2e(
            recipe_name="automation-science-pack",
            input_feeds={"copper-plate": 5, "iron-gear-wheel": 5},
            expected_output_item="automation-science-pack",
            expected_output_count=5,
            demand_rate=15.0,
            ticks=2400,
        )

    def test_smelting_furnace_iron_plate(self) -> None:
        """An electric furnace smelts iron ore into iron plates."""
        self.run_machine_cell_e2e(
            recipe_name="iron-plate",
            input_feeds={"iron-ore": 5},
            expected_output_item="iron-plate",
            expected_output_count=5,
            demand_rate=30.0,
            ticks=1800,
        )

    def test_circuit_combinator_arithmetic(self) -> None:
        """An arithmetic combinator accumulates circuit pulses from a belt telemetry tap."""
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
            local min_gx, min_gy = 1e9, 1e9
            for _, g in pairs(ghosts) do
                if g.position.x < min_gx then min_gx = g.position.x end
                if g.position.y < min_gy then min_gy = g.position.y end
            end
            local bp_entities = stack.get_blueprint_entities() or {{}}
            local min_bx, min_by = 1e9, 1e9
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
            if #poles > 0 then
                surface.create_entity{{
                    name = "electric-energy-interface",
                    position = {{poles[1].position.x, poles[1].position.y - 1}},
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


if __name__ == "__main__":
    unittest.main()
