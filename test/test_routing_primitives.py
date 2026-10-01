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
            raise RuntimeError(f"Factorio create failed:\n{res_create.stderr}")

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
            raise RuntimeError(f"Factorio benchmark failed:\n{res_bench.stderr}")

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


if __name__ == "__main__":
    unittest.main()
