"""Unit tests for physical routing primitives in small Factorio worlds (up to 4x4).

Tests physical rules of the Factorio engine:
1. Belts are one-way and can only rotate 90 degrees at a time.
2. Underground belts require two cells on M1 (entrance and exit),
   and 0-4 cells between them on M3/M4 (not at the same coordinates).
3. Pipe-to-ground fluid transfer across underground spans.
4. Obstacle bypass with underground routing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def find_factorio_binary() -> Path:
    """Find Factorio executable binary."""
    from flow.check_blueprint import find_factorio_binary as find_bin

    return find_bin()


def run_factorio_simulation(init_lua: str, tick_lua: str, ticks: int = 180) -> list[str]:
    """Execute a headless Factorio scenario with custom init and tick scripts."""
    game_bin = find_factorio_binary()
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="factorilog-test-") as tmp:
        work_dir = Path(tmp)
        mods_dir = work_dir / "mods"
        mod_dir = mods_dir / "prim_test"
        data_dir = work_dir / "data"

        mod_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        config_file = work_dir / "config.ini"
        config_file.write_text(
            f"[path]\nread-data=__PATH__executable__/../../data\nwrite-data={data_dir}\n"
        )

        info_file = mod_dir / "info.json"
        info_file.write_text(
            """{
  "name": "prim_test",
  "version": "0.1.0",
  "title": "Primitive Test Mod",
  "author": "factorilog",
  "factorio_version": "2.1",
  "dependencies": ["base >= 2.1"]
}
"""
        )

        full_control_lua = f"""
script.on_init(function()
    local surface = game.surfaces[1]
    for cx = -2, 2 do
        for cy = -2, 2 do
            surface.request_to_generate_chunks({{cx * 32, cy * 32}}, 1)
        end
    end
    surface.force_generate_chunk_requests()

    local tiles = {{}}
    for x = -20, 20 do
        for y = -20, 20 do
            tiles[#tiles + 1] = {{name = "lab-dark-1", position = {{x, y}}}}
        end
    end
    surface.set_tiles(tiles)
    for _, entity in pairs(surface.find_entities_filtered{{area = {{{{-20, -20}}, {{20, 20}}}}}}) do
        if entity.name ~= "character" then
            entity.destroy()
        end
    end

{init_lua}
end)

script.on_event(defines.events.on_tick, function(event)
{tick_lua}
end)
"""
        (mod_dir / "control.lua").write_text(full_control_lua)

        save_path = work_dir / "test.zip"
        cmd_create = [
            str(game_bin),
            "-c",
            str(config_file),
            "--mod-directory",
            str(mods_dir),
            "--create",
            str(save_path),
        ]
        res_create = subprocess.run(cmd_create, capture_output=True, text=True)
        if res_create.returncode != 0:
            log_file = data_dir / "factorio-current.log"
            log_text = log_file.read_text() if log_file.is_file() else "No log"
            raise RuntimeError(
                f"Factorio create failed:\nSTDOUT:\n{res_create.stdout}\nSTDERR:\n{res_create.stderr}\nLOG:\n{log_text}"
            )

        cmd_bench = [
            str(game_bin),
            "-c",
            str(config_file),
            "--mod-directory",
            str(mods_dir),
            "--benchmark",
            str(save_path),
            "--benchmark-ticks",
            str(ticks),
            "--benchmark-sanitize",
        ]
        res_bench = subprocess.run(cmd_bench, capture_output=True, text=True)
        if res_bench.returncode != 0:
            log_file = data_dir / "factorio-current.log"
            log_text = log_file.read_text() if log_file.is_file() else "No log"
            raise RuntimeError(
                f"Factorio benchmark failed:\nSTDOUT:\n{res_bench.stdout}\nSTDERR:\n{res_bench.stderr}\nLOG:\n{log_text}"
            )

        log_file = data_dir / "factorio-current.log"
        if not log_file.is_file():
            return []
        return [
            line for line in log_file.read_text().splitlines() if "TEST_RESULT:" in line
        ]


class TestRoutingPrimitives(unittest.TestCase):
    """Test fundamental routing primitives in 4x4 Factorio grid."""

    def test_belt_90_degree_turn(self) -> None:
        """Belts are one-way and can only rotate 90 degrees at a time."""
        init_lua = """
        local b1 = surface.create_entity{name = "transport-belt", position = {0.5, 0.5}, direction = 4, force = "player"}
        local b2 = surface.create_entity{name = "transport-belt", position = {1.5, 0.5}, direction = 8, force = "player"}
        local b3 = surface.create_entity{name = "transport-belt", position = {1.5, 1.5}, direction = 8, force = "player"}
        b1.get_transport_line(1).insert_at_back({name = "iron-plate", count = 1})
        storage.b3 = b3
        """
        tick_lua = """
        if event.tick == 120 then
            local count = storage.b3.get_transport_line(1).get_item_count()
            log("TEST_RESULT: b3_iron=" .. count)
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=130)
        self.assertTrue(any("b3_iron=1" in r for r in results), f"Expected b3_iron=1, got {results}")

    def test_underground_belt_span_and_clearance(self) -> None:
        """Underground belt requires 2 surface cells on M1 and 0-4 cells between on M3."""
        init_lua = """
        local u_in = surface.create_entity{name = "underground-belt", position = {0.5, 0.5}, direction = 4, type = "input", force = "player"}
        local cross_belt = surface.create_entity{name = "transport-belt", position = {1.5, 0.5}, direction = 8, force = "player"}
        local u_out = surface.create_entity{name = "underground-belt", position = {3.5, 0.5}, direction = 4, type = "output", force = "player"}

        u_in.get_transport_line(1).insert_at_back({name = "copper-plate", count = 1})
        cross_belt.get_transport_line(1).insert_at_back({name = "coal", count = 1})

        storage.u_out = u_out
        storage.cross_belt = cross_belt
        """
        tick_lua = """
        if event.tick == 150 then
            local u_count = storage.u_out.get_transport_line(1).get_item_count()
            local c_count = storage.cross_belt.get_transport_line(1).get_item_count()
            log(string.format("TEST_RESULT: u_out_copper=%d cross_coal=%d", u_count, c_count))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=160)
        self.assertTrue(
            any("u_out_copper=1 cross_coal=1" in r for r in results),
            f"Expected u_out_copper=1 cross_coal=1, got {results}",
        )

    def test_pipe_to_ground_fluid_transfer(self) -> None:
        """Pipe-to-ground fluid transfer across underground span between 2 surface cells."""
        init_lua = """
        local p_in = surface.create_entity{name = "pipe-to-ground", position = {0.5, 0.5}, direction = 12, force = "player"}
        local p_out = surface.create_entity{name = "pipe-to-ground", position = {3.5, 0.5}, direction = 4, force = "player"}
        p_in.insert_fluid({name = "water", amount = 100})
        storage.p_out = p_out
        """
        tick_lua = """
        if event.tick == 30 then
            local water = storage.p_out.get_fluid_count("water")
            log(string.format("TEST_RESULT: p_out_water=%.1f", water))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=40)
        self.assertTrue(any("p_out_water=50.0" in r for r in results), f"Expected p_out_water=50.0, got {results}")

    def test_obstacle_bypass_underground(self) -> None:
        """Bypass a 2x2 obstacle using underground belt on M3 with entrance/exit outside."""
        init_lua = """
        local furnace = surface.create_entity{name = "electric-furnace", position = {2.5, 2.5}, force = "player"}
        local u_in = surface.create_entity{name = "underground-belt", position = {0.5, 2.5}, direction = 4, type = "input", force = "player"}
        local u_out = surface.create_entity{name = "underground-belt", position = {4.5, 2.5}, direction = 4, type = "output", force = "player"}

        u_in.get_transport_line(1).insert_at_back({name = "iron-ore", count = 1})
        storage.u_out = u_out
        """
        tick_lua = """
        if event.tick == 150 then
            local count = storage.u_out.get_transport_line(1).get_item_count()
            log("TEST_RESULT: bypass_iron_ore=" .. count)
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=160)
        self.assertTrue(
            any("bypass_iron_ore=1" in r for r in results),
            f"Expected bypass_iron_ore=1, got {results}",
        )

    def test_pump_unidirectional_transfer(self) -> None:
        """Powered pump actively transfers fluid from input pipe to output pipe."""
        init_lua = """
        local p1 = surface.create_entity{name = "pipe", position = {0.5, 2.5}, force = "player"}
        local pump = surface.create_entity{name = "pump", position = {0.5, 1.0}, direction = 0, force = "player"}
        local p2 = surface.create_entity{name = "pipe", position = {0.5, -0.5}, force = "player"}
        p1.insert_fluid({name = "water", amount = 100})
        storage.p1 = p1
        storage.pump = pump
        storage.p2 = p2
        """
        tick_lua = """
        storage.pump.energy = 1e9
        if event.tick == 30 then
            local w1 = storage.p1.get_fluid_count("water")
            local w2 = storage.p2.get_fluid_count("water")
            log(string.format("TEST_RESULT: pump_forward w1=%.1f w2=%.1f", w1, w2))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=40)
        self.assertTrue(
            any("pump_forward w1=0.0 w2=99.9" in r for r in results),
            f"Expected w1=0.0 w2=99.9, got {results}",
        )

    def test_pump_backflow_isolation(self) -> None:
        """Pump blocks backflow when output pipe contains high pressure fluid."""
        init_lua = """
        local p1 = surface.create_entity{name = "pipe", position = {0.5, 2.5}, force = "player"}
        local pump = surface.create_entity{name = "pump", position = {0.5, 1.0}, direction = 0, force = "player"}
        local p2 = surface.create_entity{name = "pipe", position = {0.5, -0.5}, force = "player"}
        p2.insert_fluid({name = "water", amount = 100})
        storage.p1 = p1
        storage.pump = pump
        storage.p2 = p2
        """
        tick_lua = """
        storage.pump.energy = 1e9
        if event.tick == 60 then
            local w1 = storage.p1.get_fluid_count("water")
            local w2 = storage.p2.get_fluid_count("water")
            log(string.format("TEST_RESULT: pump_reverse w1=%.1f w2=%.1f", w1, w2))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=70)
        self.assertTrue(
            any("pump_reverse w1=0.0 w2=100.0" in r for r in results),
            f"Expected w1=0.0 w2=100.0, got {results}",
        )

    def test_pump_blueprint_placement_and_footprint(self) -> None:
        """Pump blueprint builds cleanly with 1x2 footprint in Factorio."""
        import blueprint

        entities = [
            {"name": "pipe", "position": {"x": 0.5, "y": 2.5}, "entity_number": 1},
            {"name": "pump", "position": {"x": 0.5, "y": 1.0}, "direction": 0, "entity_number": 2},
            {"name": "pipe", "position": {"x": 0.5, "y": -0.5}, "entity_number": 3},
        ]
        bp_str = blueprint.encode(entities, "pump_blueprint_test")
        init_lua = f"""
        local inv = game.create_inventory(1)
        local stack = inv[1]
        stack.set_stack{{name = "blueprint", count = 1}}
        stack.import_stack('{bp_str}')
        local built = stack.build_blueprint{{
            surface = surface,
            force = game.forces.player,
            position = {{0, 0}},
            force_build = true
        }}
        local ghosts = surface.find_entities_filtered{{name = "entity-ghost"}}
        local revived = 0
        for _, g in pairs(ghosts) do
            local colliders, ent = g.revive()
            if ent and ent.valid then revived = revived + 1 end
        end
        storage.revived = revived
        """
        tick_lua = """
        if event.tick == 1 then
            local surface = game.surfaces[1]
            local placed_pumps = surface.find_entities_filtered{name = "pump"}
            log(string.format("TEST_RESULT: bp_pumps=%d revived=%d", #placed_pumps, storage.revived or 0))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=5)
        self.assertTrue(
            any("bp_pumps=1 revived=3" in r for r in results),
            f"Expected bp_pumps=1 revived=3, got {results}",
        )

    def test_belt_intersection_first_unit_turns(self) -> None:
        """An intersecting belt entering the first unit curves that unit with 1:1 lane mapping."""
        init_lua = """
        local u1 = surface.create_entity{name = "transport-belt", position = {0.5, 1.5}, direction = 4, force = "player"}
        local u2 = surface.create_entity{name = "transport-belt", position = {1.5, 1.5}, direction = 4, force = "player"}
        local u3 = surface.create_entity{name = "transport-belt", position = {2.5, 1.5}, direction = 4, force = "player"}
        local bi = surface.create_entity{name = "transport-belt", position = {0.5, 0.5}, direction = 8, force = "player"}

        bi.get_transport_line(1).insert_at_back({name = "copper-plate", count = 1})
        bi.get_transport_line(2).insert_at_back({name = "copper-ore", count = 1})

        storage.u1 = u1
        storage.u3 = u3
        """
        tick_lua = """
        if event.tick == 1 then
            log("TEST_RESULT: u1_shape=" .. storage.u1.belt_shape)
        elseif event.tick == 120 then
            local l1 = storage.u3.get_transport_line(1).get_item_count("copper-plate")
            local l2 = storage.u3.get_transport_line(2).get_item_count("copper-ore")
            log(string.format("TEST_RESULT: u3_end l1_plate=%d l2_ore=%d", l1, l2))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=130)
        self.assertTrue(any("u1_shape=left" in r for r in results), f"Expected u1_shape=left, got {results}")
        self.assertTrue(any("u3_end l1_plate=1 l2_ore=1" in r for r in results), f"Expected 1:1 lane flow, got {results}")

    def test_belt_intersection_middle_unit_sideloads(self) -> None:
        """An intersecting belt entering the middle unit sideloads onto a single lane."""
        init_lua = """
        local u1 = surface.create_entity{name = "transport-belt", position = {0.5, 1.5}, direction = 4, force = "player"}
        local u2 = surface.create_entity{name = "transport-belt", position = {1.5, 1.5}, direction = 4, force = "player"}
        local u3 = surface.create_entity{name = "transport-belt", position = {2.5, 1.5}, direction = 4, force = "player"}
        local bi = surface.create_entity{name = "transport-belt", position = {1.5, 0.5}, direction = 8, force = "player"}

        u1.get_transport_line(2).insert_at_back({name = "iron-ore", count = 1})
        bi.get_transport_line(1).insert_at_back({name = "copper-plate", count = 1})
        bi.get_transport_line(2).insert_at_back({name = "copper-ore", count = 1})

        storage.u2 = u2
        storage.u3 = u3
        """
        tick_lua = """
        if event.tick == 1 then
            log(string.format("TEST_RESULT: u2_shape=%s in_count=%d", storage.u2.belt_shape, #storage.u2.belt_neighbours.inputs))
        elseif event.tick == 120 then
            local l1_count = storage.u3.get_transport_line(1).get_item_count()
            local l2_count = storage.u3.get_transport_line(2).get_item_count()
            log(string.format("TEST_RESULT: u3_lanes l1_count=%d l2_count=%d", l1_count, l2_count))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=130)
        self.assertTrue(any("u2_shape=straight in_count=2" in r for r in results), f"Expected straight with 2 inputs, got {results}")
        self.assertTrue(any("u3_lanes l1_count=2 l2_count=1" in r for r in results), f"Expected sideloaded 2 on L1 and 1 on L2, got {results}")

    def test_belt_intersection_rotated_third_unit(self) -> None:
        """Rotated third unit straightens when fed from behind, making prior unit a sideload."""
        init_lua = """
        local u1 = surface.create_entity{name = "transport-belt", position = {0.5, 1.5}, direction = 4, force = "player"}
        local u2 = surface.create_entity{name = "transport-belt", position = {1.5, 1.5}, direction = 4, force = "player"}
        local u3 = surface.create_entity{name = "transport-belt", position = {2.5, 1.5}, direction = 8, force = "player"}
        local bi = surface.create_entity{name = "transport-belt", position = {2.5, 0.5}, direction = 8, force = "player"}

        storage.u3 = u3
        """
        tick_lua = """
        if event.tick == 1 then
            log(string.format("TEST_RESULT: u3_rot_shape=%s in_count=%d", storage.u3.belt_shape, #storage.u3.belt_neighbours.inputs))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=5)
        self.assertTrue(any("u3_rot_shape=straight in_count=2" in r for r in results), f"Expected straight with 2 inputs, got {results}")

    def test_belt_3x3_crossing_requires_underground(self) -> None:
        """Crossing two 3-unit belt lines in 3x3 requires an underground belt for isolation."""
        init_lua = """
        local uh1 = surface.create_entity{name = "transport-belt", position = {0.5, 1.5}, direction = 4, force = "player"}
        local uh2 = surface.create_entity{name = "transport-belt", position = {1.5, 1.5}, direction = 4, force = "player"}
        local uh3 = surface.create_entity{name = "transport-belt", position = {2.5, 1.5}, direction = 4, force = "player"}

        local uv1 = surface.create_entity{name = "underground-belt", position = {1.5, 0.5}, direction = 8, type = "input", force = "player"}
        local uv3 = surface.create_entity{name = "underground-belt", position = {1.5, 2.5}, direction = 8, type = "output", force = "player"}

        uh1.get_transport_line(1).insert_at_back({name = "iron-plate", count = 3})
        uv1.get_transport_line(1).insert_at_back({name = "copper-plate", count = 3})

        storage.uh3 = uh3
        storage.uv3 = uv3
        """
        tick_lua = """
        if event.tick == 120 then
            local h_count = storage.uh3.get_transport_line(1).get_item_count()
            local v_count = storage.uv3.get_transport_line(1).get_item_count()
            log(string.format("TEST_RESULT: 3x3_ug_cross h_count=%d v_count=%d", h_count, v_count))
        end
        """
        results = run_factorio_simulation(init_lua, tick_lua, ticks=130)
        self.assertTrue(any("3x3_ug_cross h_count=1 v_count=1" in r for r in results), f"Expected h_count=1 v_count=1, got {results}")


if __name__ == "__main__":
    unittest.main()
