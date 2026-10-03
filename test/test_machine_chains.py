"""Run end-to-end tests for two-stage PDK machine chains through OpenROAD and Factorio.

Each test executes these steps:
1. Synthesize a two-machine netlist with connected material nets.
2. Run OpenROAD physical placement and detailed routing.
3. Verify zero DRC violations in TritonRoute.
4. Translate the routed DEF into a Factorio blueprint.
5. Simulate the blueprint in headless Factorio to verify inter-cell handoffs.
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
from test_machine_cells import serialize_lua
from test_routing_primitives import run_factorio_simulation

OPENROAD_IMAGE = "docker.io/openroad/orfs:latest"
REAL_USER_HOME = pwd.getpwuid(os.getuid()).pw_dir


class TestMachineChains(unittest.TestCase):
    """Verify physical execution of PDK machine chains from OpenROAD to Factorio."""

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

    def run_chain_e2e(
        self,
        chain_name: str,
        demand: dict[str, float],
        allowed_recipes: list[str],
        input_feeds: dict[str, int],
        expected_output_item: str,
        expected_output_count: int,
        machine_override: dict[str, str] | None = None,
        prefill_fluids: dict[str, tuple[str, float]] | None = None,
        block_draining_ticks: int = 0,
        ticks: int = 2400,
    ) -> None:
        """Execute a two-stage machine chain end-to-end through OpenROAD and Factorio."""
        with tempfile.TemporaryDirectory(dir="/tmp", prefix=f"chain-{chain_name}-") as tmp:
            work_dir = Path(tmp)

            # 1. Synthesize two-machine netlist
            allowed = {r: self.data.recipes[r] for r in allowed_recipes}
            nl = netlist.build(
                self.data,
                demand,
                allowed,
                machine_override=machine_override,
            )
            plans = netlist.merge_plans_of(nl)
            split_plans = netlist.split_plans_of(nl)

            (work_dir / "netlist.v").write_text(netlist.emit_verilog(nl, plans, split_plans))
            (work_dir / "cells.v").write_text(netlist.emit_cells_v(nl))

            cells = list(nl.cells.values())
            lef.write_lef(self.pdk, cells, str(work_dir / "tech.lef"))
            liberty.write_liberty(cells, str(work_dir / "tech.lib"))

            fluids = [
                m
                for m in (nl.inputs + nl.outputs + list(nl.nets.keys()))
                if m in self.data.prototypes.get("fluid", {})
            ]
            sidecar = {
                "demand": demand,
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

            # Verify zero DRC violations
            drc_rpt = work_dir / "route_drc.rpt"
            if drc_rpt.is_file():
                drc_text = drc_rpt.read_text().strip()
                self.assertEqual(
                    drc_text, "", f"DRC violations detected in {chain_name}: {drc_text}"
                )

            def_path = work_dir / "factory.def"
            self.assertTrue(def_path.is_file(), "factory.def not generated by OpenROAD")

            # 3. Translate DEF to Factorio blueprint
            def_text = def_path.read_text()
            bp_string = def2bp.def_to_blueprint(def_text, sidecar, label=f"chain_{chain_name}")

            # Verify blueprint pin structure
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
                for machine_type, (fluid_name, amount) in prefill_fluids.items():
                    prefill_lua_lines.append(f"""
                    for _, ent in pairs(surface.find_entities()) do
                        if ent.name == "{machine_type}" then
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

            local poles = surface.find_entities_filtered{{name = "medium-electric-pole"}}
            for _, p in ipairs(poles) do
                surface.create_entity{{
                    name = "electric-energy-interface",
                    position = {{p.position.x, p.position.y - 1}},
                    force = "player"
                }}
            end

            {chr(10).join(prefill_lua_lines)}

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

            storage.feed_targets = {{}}
            storage.fed_counts = {{}}
            {chr(10).join(feed_lua_lines)}
            storage.drained_count = 0
            """

            is_fluid = expected_output_item in self.data.prototypes.get("fluid", {})
            drain_condition = (
                f"event.tick >= {block_draining_ticks}" if block_draining_ticks > 0 else "true"
            )

            belt_drain_lua = ""
            if not is_fluid:
                belt_drain_lua = f"""
                if {drain_condition} then
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
                end
                """

            tick_lua = f"""
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

            {belt_drain_lua}

            if event.tick == {ticks} then
                local extra_count = 0
                local surface = game.surfaces[1]
                for _, ent in ipairs(surface.find_entities()) do
                    if ent.type == "assembling-machine" or ent.type == "furnace" then
                        local inv = ent.get_output_inventory()
                        if inv then
                            extra_count = extra_count + inv.get_item_count("{expected_output_item}")
                        end
                    end
                end
                local total = (storage.drained_count or 0) + extra_count
                log(string.format("TEST_RESULT: {chain_name} total=%d drained=%d extra=%d revived=%d",
                    total, storage.drained_count or 0, extra_count, storage.revived or 0))
            end
            """

            results = run_factorio_simulation(init_lua, tick_lua, ticks=ticks + 10)
            target_line = None
            for line in results:
                if f"TEST_RESULT: {chain_name}" in line:
                    target_line = line
                    break

            self.assertIsNotNone(
                target_line,
                f"No simulation results returned for {chain_name}. Log lines: {results}",
            )

            m_total = re.search(r"total=(\d+)", target_line)
            m_revived = re.search(r"revived=(\d+)", target_line)
            self.assertIsNotNone(m_total, f"total count missing in {target_line}")
            self.assertIsNotNone(m_revived, f"revived count missing in {target_line}")
            total_count = int(m_total.group(1))
            revived_count = int(m_revived.group(1))
            self.assertGreater(revived_count, 0, f"No entities revived for {chain_name}")
            self.assertGreaterEqual(
                total_count,
                expected_output_count,
                f"Expected at least {expected_output_count} {expected_output_item}, got {total_count}",
            )

    def test_solid_chain_crafting_to_crafting(self) -> None:
        """Test crafting to crafting chain from iron gear wheels to science packs."""
        self.run_chain_e2e(
            chain_name="crafting_to_crafting",
            demand={"automation-science-pack": 15.0},
            allowed_recipes=["iron-gear-wheel", "automation-science-pack"],
            input_feeds={"iron-plate": 10, "copper-plate": 5},
            expected_output_item="automation-science-pack",
            expected_output_count=3,
            machine_override={
                "iron-gear-wheel": "assembling-machine-1",
                "automation-science-pack": "assembling-machine-3",
            },
            ticks=2400,
        )

    def test_solid_chain_smelting_to_crafting(self) -> None:
        """Test smelting to crafting chain from copper ore to copper cable."""
        self.run_chain_e2e(
            chain_name="smelting_to_crafting",
            demand={"copper-cable": 30.0},
            allowed_recipes=["copper-plate", "copper-cable"],
            input_feeds={"copper-ore": 10},
            expected_output_item="copper-cable",
            expected_output_count=10,
            ticks=2400,
        )

    def test_fluid_chain_refinery_to_chemical_plant(self) -> None:
        """Test fluid chain from oil refinery to chemical plant for solid fuel."""
        self.run_chain_e2e(
            chain_name="refinery_to_chemical_plant",
            demand={"solid-fuel": 10.0},
            allowed_recipes=["basic-oil-processing", "solid-fuel-from-petroleum-gas"],
            input_feeds={},
            expected_output_item="solid-fuel",
            expected_output_count=3,
            prefill_fluids={"oil-refinery": ("crude-oil", 500.0)},
            ticks=1500,
        )

    def test_backpressure_stability(self) -> None:
        """Test belt backpressure stability when downstream output pauses."""
        self.run_chain_e2e(
            chain_name="backpressure_stability",
            demand={"automation-science-pack": 15.0},
            allowed_recipes=["iron-gear-wheel", "automation-science-pack"],
            input_feeds={"iron-plate": 20, "copper-plate": 10},
            expected_output_item="automation-science-pack",
            expected_output_count=5,
            machine_override={
                "iron-gear-wheel": "assembling-machine-1",
                "automation-science-pack": "assembling-machine-3",
            },
            block_draining_ticks=1800,
            ticks=3600,
        )


if __name__ == "__main__":
    unittest.main()
