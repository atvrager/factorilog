"""Unit and end-to-end tests for the map cartographer and obstacle classifier."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from foundry import solve
from foundry.bp2save import find_factorio_binary
from foundry.campaign.cartographer import (
    MapCartographer,
    build_lua_cartography_scanner_snippet,
    discover_water_tile_names,
    is_cliff_demolition_unlocked,
    is_landfill_unlocked,
)
from foundry.patch_router import route_patch_to_base

FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "dump-fixture.json"


class CartographerTest(unittest.TestCase):
    """Tests for MapCartographer, technology-dependent obstacle classification, and routing."""

    data: solve.Data

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = solve.Data(str(FIXTURE_PATH))

    def test_discover_water_and_tech_unlocks(self) -> None:
        """Verify dynamic water tile discovery and cliff/landfill technology unlock checks."""
        water_names = discover_water_tile_names(self.data)
        self.assertIn("water", water_names)
        self.assertIn("deepwater", water_names)

        # At game start or with only logistics researched, the tech tree locks cliff demolition and landfill
        self.assertFalse(is_cliff_demolition_unlocked(self.data, {"logistics", "automation"}))
        self.assertFalse(is_landfill_unlocked(self.data, {"logistics", "automation"}))

        # Researching cliff-explosives unlocks cliff demolition
        self.assertTrue(is_cliff_demolition_unlocked(self.data, {"cliff-explosives"}))

        # Researching landfill unlocks landfill placement
        self.assertTrue(is_landfill_unlocked(self.data, {"landfill"}))

    def test_technology_dependent_obstacle_classification(self) -> None:
        """Verify that trees and rocks are always clearable while cliffs depend on cliff-explosives."""
        cart = MapCartographer(data=self.data)
        tree_obs = cart.add_obstacle("tree-01", "tree", 10.5, 12.5)
        rock_obs = cart.add_obstacle("big-rock", "simple-entity", 14.0, 16.0)
        cliff_obs = cart.add_obstacle(
            "cliff",
            "cliff",
            20.0,
            22.0,
            cliff_orientation="north-to-south",
        )

        self.assertIn((10, 12), tree_obs.occupied_tiles)
        self.assertTrue(len(rock_obs.occupied_tiles) >= 1)
        self.assertTrue(len(cliff_obs.occupied_tiles) >= 4)

        # Early game (logistics tier): trees and rocks are clearable, cliffs are hard obstacles
        early_cls = cart.classify_obstacles({"automation", "logistics"})
        self.assertFalse(early_cls.cliff_demolition_unlocked)
        self.assertEqual(len(early_cls.hard_obstacles), 1)
        self.assertEqual(early_cls.hard_obstacles[0].entity_type, "cliff")
        self.assertEqual(len(early_cls.clearable_obstacles), 2)
        self.assertEqual(early_cls.hard_tiles, set(cliff_obs.occupied_tiles))
        self.assertTrue(tree_obs.occupied_tiles.issubset(early_cls.clearable_tiles))
        self.assertTrue(rock_obs.occupied_tiles.issubset(early_cls.clearable_tiles))

        # MapSurfaceSpec at early game includes only cliff tiles in obstacles (not trees/rocks)
        early_spec = cart.to_map_surface_spec({"automation", "logistics"})
        self.assertEqual(early_spec.obstacles, set(cliff_obs.occupied_tiles))
        self.assertFalse(early_spec.obstacles & set(tree_obs.occupied_tiles))

        # When the force researches cliff-explosives, cliffs also become clearable
        late_cls = cart.classify_obstacles({"cliff-explosives"})
        self.assertTrue(late_cls.cliff_demolition_unlocked)
        self.assertEqual(len(late_cls.hard_obstacles), 0)
        self.assertEqual(len(late_cls.hard_tiles), 0)
        self.assertEqual(len(late_cls.clearable_obstacles), 3)

        late_spec = cart.to_map_surface_spec({"cliff-explosives"})
        self.assertEqual(len(late_spec.obstacles), 0)

    def test_ingest_telemetry_and_compute_pre_clearing_plan(self) -> None:
        """Verify JSON telemetry ingestion, routing around cliffs, and tree/rock pre-clearing."""
        cart = MapCartographer(data=self.data)
        telemetry_payload = {
            "tick": 120,
            "cartography": {
                "charted_chunks": [[0, 0], [1, 0], [0, 1], [1, 1]],
                "water_tiles": [[x, y] for x in range(8, 13) for y in range(2, 6)],
                "resource_patches": {
                    "coal": [[x, y, 5000] for x in range(4, 10) for y in range(14, 20)],
                    "iron-ore": [[x, y, 5000] for x in range(4, 10) for y in range(25, 31)],
                    "copper-ore": [[x, y, 5000] for x in range(4, 10) for y in range(36, 42)],
                },
                "obstacles": [
                    # Put a cliff wall in the middle of the map that the router must avoid
                    {
                        "name": "cliff",
                        "type": "cliff",
                        "position": {"x": 20.0, "y": 18.0},
                        "bounding_box": [[18.1, 16.1], [21.9, 19.9]],
                        "unit_number": 101,
                        "cliff_orientation": "north-to-south",
                    },
                    # Put trees directly on top of the base core and belt corridor
                    {
                        "name": "tree-01",
                        "type": "tree",
                        "position": {"x": 30.5, "y": 17.5},
                        "bounding_box": [[30.1, 17.1], [30.9, 17.9]],
                        "unit_number": 202,
                    },
                    {
                        "name": "big-rock",
                        "type": "simple-entity",
                        "position": {"x": 33.0, "y": 20.0},
                        "bounding_box": [[32.1, 19.1], [33.9, 20.9]],
                        "unit_number": 203,
                    },
                    # Distant tree that does not intersect the base or belts
                    {
                        "name": "tree-02",
                        "type": "tree",
                        "position": {"x": 60.5, "y": 48.5},
                        "bounding_box": [[60.1, 48.1], [60.9, 48.9]],
                        "unit_number": 204,
                    },
                ],
            },
        }
        cart.ingest_telemetry_dict(telemetry_payload)
        self.assertEqual(len(cart.water_tiles), 20)
        self.assertEqual(len(cart.resource_patches["coal"]), 36)
        self.assertEqual(len(cart.obstacles), 4)

        surface = cart.to_map_surface_spec(
            researched_techs={"automation", "logistics"},
            bounds=(0, 64, 0, 52),
        )
        # Cliff tiles must be in surface.obstacles, while tree and rock tiles do not block routing
        cliff_tiles = cart.obstacles[0].occupied_tiles
        self.assertEqual(surface.obstacles, set(cliff_tiles))

        res = route_patch_to_base(
            surface=surface,
            data=self.data,
            base_origin=(28, 16),
            verify=False,
        )
        # Verify zero placed entities overlap the cliff keepout tiles
        for ent in res.entities:
            ex = int(float(ent["position"]["x"]))
            ey = int(float(ent["position"]["y"]))
            self.assertNotIn((ex, ey), cliff_tiles)

        # Verify obstacles_to_clear_for_entities identifies the intersecting tree and rock
        to_clear = cart.obstacles_to_clear_for_entities(
            res.entities,
            researched_techs={"automation", "logistics"},
            reference_pos=(5.0, 15.0),
        )
        cleared_units = {obs.unit_number for obs in to_clear}
        self.assertIn(202, cleared_units)
        self.assertIn(203, cleared_units)
        self.assertNotIn(204, cleared_units)
        self.assertNotIn(101, cleared_units)

        # Test remove_obstacle_at after clearing
        removed = cart.remove_obstacle_at(30.5, 17.5)
        self.assertEqual(removed, 1)

    def test_headless_factorio_cartographer_extraction_and_routing(self) -> None:
        """Verify live Factorio surface scanning via Lua snippet, cartographer ingestion, and routing."""
        factorio_bin = find_factorio_binary()
        if not factorio_bin:
            self.skipTest("Factorio binary not available")

        scanner_lua = build_lua_cartography_scanner_snippet(radius=64)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            mods_dir = tmp_path / "mods"
            mods_dir.mkdir(parents=True, exist_ok=True)
            mod_list = {
                "mods": [
                    {"name": "base", "enabled": True},
                    {"name": "elevated-rails", "enabled": False},
                    {"name": "quality", "enabled": False},
                    {"name": "recycler", "enabled": False},
                    {"name": "space-age", "enabled": False},
                ]
            }
            (mods_dir / "mod-list.json").write_text(json.dumps(mod_list, indent=2))
            config_file = tmp_path / "config.ini"
            config_file.write_text(
                f"[path]\nread-data=__PATH__executable__/../../data\nwrite-data={tmp_path}\n"
            )
            scenario_name = "cartographer_scan_sim"
            scenario_dir = tmp_path / "scenarios" / scenario_name
            scenario_dir.mkdir(parents=True, exist_ok=True)
            (scenario_dir / "description.json").write_text(
                json.dumps({"order": "a", "multiplayer-compatible": True, "is-main-game": True})
            )

            control_lua = f"""script.on_init(function()
  local surface = game.surfaces[1]
  for cx = -2, 3 do
    for cy = -2, 3 do
      surface.request_to_generate_chunks({{cx * 32, cy * 32}}, 1)
    end
  end
  surface.force_generate_chunk_requests()

  local land_tiles = {{}}
  for x = -48, 96 do
    for y = -48, 96 do
      land_tiles[#land_tiles + 1] = {{name = 'grass-1', position = {{x, y}}}}
    end
  end
  surface.set_tiles(land_tiles)

  local water_tiles = {{}}
  for x = 8, 12 do
    for y = 2, 5 do
      water_tiles[#water_tiles + 1] = {{name = 'water', position = {{x, y}}}}
    end
  end
  surface.set_tiles(water_tiles)

  for _, ent in pairs(surface.find_entities_filtered({{area = {{{{-48, -48}}, {{96, 96}}}}}})) do
    if ent.name ~= 'character' then
      ent.destroy()
    end
  end

  for x = 4, 9 do
    for y = 14, 19 do
      surface.create_entity({{name = 'coal', position = {{x + 0.5, y + 0.5}}, amount = 4000}})
    end
    for y = 25, 30 do
      surface.create_entity({{name = 'iron-ore', position = {{x + 0.5, y + 0.5}}, amount = 4000}})
    end
    for y = 36, 41 do
      surface.create_entity({{name = 'copper-ore', position = {{x + 0.5, y + 0.5}}, amount = 4000}})
    end
  end

  surface.create_entity({{name = 'cliff', position = {{20.0, 18.0}}, cliff_orientation = 'north-to-south'}})
  surface.create_entity({{name = 'tree-01', position = {{30.5, 17.5}}}})
  surface.create_entity({{name = 'big-rock', position = {{33.0, 20.0}}}})

  local p = {{position = {{x = 24, y = 24}}, surface = surface}}
{scanner_lua}
  helpers.write_file('cartography_dump.json', helpers.table_to_json({{cartography = storage.cartography}}) .. '\\n', false)
end)
"""
            (scenario_dir / "control.lua").write_text(control_lua)
            proc = subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--scenario2map",
                    scenario_name,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc.returncode, 0, msg=f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )

            dump_file = tmp_path / "script-output" / "cartography_dump.json"
            self.assertTrue(dump_file.is_file())

            cart = MapCartographer(data=self.data)
            self.assertTrue(cart.ingest_telemetry_file(dump_file))
            self.assertEqual(len(cart.water_tiles), 20)
            self.assertEqual(len(cart.resource_patches["coal"]), 36)
            self.assertEqual(len(cart.resource_patches["iron-ore"]), 36)
            self.assertEqual(len(cart.resource_patches["copper-ore"]), 36)
            self.assertEqual(len(cart.obstacles), 3)

            surface = cart.to_map_surface_spec(
                researched_techs={"automation", "logistics"},
                bounds=(0, 64, 0, 52),
            )
            res = route_patch_to_base(
                surface=surface,
                data=self.data,
                base_origin=(28, 16),
                verify=False,
            )
            self.assertGreater(len(res.entities), 50)
            to_clear = cart.obstacles_to_clear_for_entities(
                res.entities,
                researched_techs={"automation", "logistics"},
            )
            cleared_types = {obs.entity_type for obs in to_clear}
            self.assertIn("tree", cleared_types)
            self.assertIn("simple-entity", cleared_types)
            self.assertNotIn("cliff", cleared_types)


if __name__ == "__main__":
    unittest.main()
