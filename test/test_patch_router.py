"""Unit and end-to-end tests for the patch-to-base router and starter base synthesizer."""

from __future__ import annotations

import collections
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from foundry import blueprint, solve
from foundry.bp2save import find_factorio_binary
from foundry.render_bp import render_blueprint
from foundry.entity_helpers import (
    check_collisions,
    check_power,
    check_underground_pairing,
    get_pole_supply_radius,
    get_pole_wire_reach,
)
from foundry.patch_router import (
    MapSurfaceSpec,
    resolve_tech_tier,
    route_patch_to_base,
    synthesize_patch_miner_array,
    synthesize_shoreline_power_array,
    synthesize_starter_base_core,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "dump-fixture.json"


def _rect_tiles(x0: int, y0: int, w: int, h: int) -> set[tuple[int, int]]:
    """Return a rectangular set of integer (x, y) tile coordinates."""
    return {(x, y) for x in range(x0, x0 + w) for y in range(y0, y0 + h)}


class PatchRouterTest(unittest.TestCase):
    """Tests for tech-tier resolution, miner arrays, shoreline power, and trunk routing."""

    data: solve.Data

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = solve.Data(str(FIXTURE_PATH))

    def test_resolve_logistics_tech_tier(self) -> None:
        tier = resolve_tech_tier(self.data, unlocked_by="logistics")
        self.assertEqual(tier.drill_name, "electric-mining-drill")
        self.assertEqual((tier.drill_width, tier.drill_height), (3, 3))
        self.assertEqual(tier.belt_name, "transport-belt")
        self.assertEqual(tier.underground_belt_name, "underground-belt")
        self.assertEqual(tier.splitter_name, "splitter")
        self.assertEqual(tier.pole_name, "small-electric-pole")
        self.assertEqual(tier.inserter_name, "inserter")
        self.assertEqual(tier.long_inserter_name, "long-handed-inserter")
        self.assertEqual(tier.furnace_name, "stone-furnace")
        self.assertEqual(tier.assembler_name, "assembling-machine-1")
        self.assertEqual(tier.lab_name, "lab")
        self.assertEqual(tier.fuel_commodity, "coal")
        self.assertEqual(
            set(tier.filtered_prototypes["underground-belt"].keys()),
            {"underground-belt"},
        )
        self.assertEqual(
            set(tier.filtered_prototypes["splitter"].keys()),
            {"splitter"},
        )

    def test_synthesize_patch_miner_array_single_and_split_fanout(self) -> None:
        tier = resolve_tech_tier(self.data, unlocked_by="logistics")
        patch_tiles = _rect_tiles(4, 10, 10, 8)

        single_res = synthesize_patch_miner_array(
            commodity="iron-ore",
            patch_tiles=patch_tiles,
            target_rate_per_min=45.0,
            target_anchor=(30, 14),
            obstacles=set(),
            data=self.data,
            tier=tier,
            output_fanout=1,
        )
        self.assertGreaterEqual(single_res.drill_count, 2)
        self.assertEqual(len(single_res.output_terminals), 1)
        self.assertEqual(check_collisions(single_res.entities), [])

        split_res = synthesize_patch_miner_array(
            commodity="coal",
            patch_tiles=patch_tiles,
            target_rate_per_min=30.0,
            target_anchor=(30, 14),
            obstacles=set(),
            data=self.data,
            tier=tier,
            output_fanout=2,
        )
        self.assertGreaterEqual(split_res.drill_count, 1)
        self.assertEqual(len(split_res.output_terminals), 2)
        splitter_ents = [e for e in split_res.entities if e["name"] == "splitter"]
        self.assertEqual(len(splitter_ents), 1)
        self.assertEqual(check_collisions(split_res.entities), [])

    def test_synthesize_shoreline_power_array(self) -> None:
        tier = resolve_tech_tier(self.data, unlocked_by="logistics")
        water_tiles = _rect_tiles(0, 0, 12, 4)
        power_res = synthesize_shoreline_power_array(
            water_tiles=water_tiles,
            required_power_watts=500000.0,
            reference_pos=(6, 12),
            obstacles=set(),
            data=self.data,
            tier=tier,
        )
        names = {e["name"] for e in power_res.entities}
        self.assertIn("offshore-pump", names)
        self.assertIn("boiler", names)
        self.assertIn("steam-engine", names)
        self.assertIn("inserter", names)
        self.assertIn("transport-belt", names)
        self.assertEqual(check_collisions(power_res.entities), [])

    def test_synthesize_starter_base_core(self) -> None:
        tier = resolve_tech_tier(self.data, unlocked_by="logistics")
        base_res = synthesize_starter_base_core(
            origin=(25, 10),
            data=self.data,
            tier=tier,
            science_recipe="automation-science-pack",
            mall_recipes=("transport-belt",),
        )
        self.assertIn("coal", base_res.input_terminals)
        self.assertIn("iron-ore", base_res.input_terminals)
        self.assertIn("copper-ore", base_res.input_terminals)
        self.assertEqual(check_collisions(base_res.entities), [])
        recipes_placed = {e.get("recipe") for e in base_res.entities if "recipe" in e}
        self.assertIn("iron-gear-wheel", recipes_placed)
        self.assertIn("transport-belt", recipes_placed)
        self.assertIn("automation-science-pack", recipes_placed)

    def test_route_patch_to_base_full_layout_and_pdn(self) -> None:
        surface = MapSurfaceSpec(
            min_x=0,
            max_x=70,
            min_y=0,
            max_y=50,
            water_tiles=_rect_tiles(2, 2, 12, 4),
            resource_patches={
                "coal": _rect_tiles(4, 14, 9, 7),
                "iron-ore": _rect_tiles(4, 25, 9, 7),
                "copper-ore": _rect_tiles(4, 36, 9, 7),
            },
        )
        res = route_patch_to_base(
            surface=surface,
            data=self.data,
            science_recipe="automation-science-pack",
            mall_recipes=("transport-belt",),
            unlocked_by="logistics",
            base_origin=(28, 16),
            engine=None,
            verify=True,
        )
        self.assertTrue(res.blueprint_string.startswith("0"))
        self.assertEqual(len(res.trunk_nets), 4)
        for route in res.trunk_routes.values():
            self.assertTrue(route.is_success)
        assert res.verification is not None
        self.assertTrue(res.verification.is_valid)

        # Verify zero collisions, valid underground pairs, and full small-electric-pole coverage
        self.assertEqual(check_collisions(res.entities), [])
        self.assertEqual(check_underground_pairing(res.entities), [])
        supply_rad = get_pole_supply_radius("small-electric-pole")
        self.assertEqual(check_power(res.entities, supply_radius=supply_rad), [])

        # Verify that all small-electric-pole entities form a single connected copper wire graph
        poles = [e for e in res.entities if e["name"] == "small-electric-pole"]
        self.assertGreater(len(poles), 4)
        pole_ids = {int(e["entity_number"]) for e in poles}
        wire_reach = get_pole_wire_reach("small-electric-pole")
        by_id = {int(e["entity_number"]): e for e in poles}
        adj: dict[int, set[int]] = collections.defaultdict(set)
        for u, c1, v, c2 in res.wires:
            self.assertEqual(c1, 5)
            self.assertEqual(c2, 5)
            p_u = by_id[u]["position"]
            p_v = by_id[v]["position"]
            dist = ((p_u["x"] - p_v["x"]) ** 2 + (p_u["y"] - p_v["y"]) ** 2) ** 0.5
            self.assertLessEqual(dist, wire_reach + 1e-6)
            adj[u].add(v)
            adj[v].add(u)

        start_pole = next(iter(pole_ids))
        visited = {start_pole}
        queue = collections.deque([start_pole])
        while queue:
            cur = queue.popleft()
            for nb in adj[cur]:
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        self.assertEqual(visited, pole_ids)

    def test_headless_factorio_real_patches_zero_infinity_chests(self) -> None:
        try:
            factorio_bin = find_factorio_binary()
        except FileNotFoundError:
            self.skipTest("Factorio binary not available for headless simulation")

        surface = MapSurfaceSpec(
            min_x=0,
            max_x=70,
            min_y=0,
            max_y=50,
            water_tiles=_rect_tiles(2, 2, 12, 4),
            resource_patches={
                "coal": _rect_tiles(4, 14, 9, 7),
                "iron-ore": _rect_tiles(4, 25, 9, 7),
                "copper-ore": _rect_tiles(4, 36, 9, 7),
            },
        )
        res = route_patch_to_base(
            surface=surface,
            data=self.data,
            science_recipe="automation-science-pack",
            mall_recipes=("transport-belt",),
            unlocked_by="logistics",
            base_origin=(28, 16),
            engine=None,
            verify=True,
        )

        # Ensure zero cheat entities exist in the blueprint
        bp_doc = blueprint.decode(res.blueprint_string)["blueprint"]
        for ent in bp_doc["entities"]:
            self.assertNotIn("infinity", ent["name"])
            self.assertNotIn("electric-energy-interface", ent["name"])

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            mods_dir = tmp_path / "mods"
            mods_dir.mkdir(parents=True, exist_ok=True)
            import json

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
            scenario_name = "patch_to_base_sim"
            scenario_dir = tmp_path / "scenarios" / scenario_name
            scenario_dir.mkdir(parents=True, exist_ok=True)
            (scenario_dir / "description.json").write_text(
                json.dumps({"order": "a", "multiplayer-compatible": True, "is-main-game": True})
            )

            water_lua = ", ".join(f"{{{x}, {y}}}" for x, y in sorted(surface.water_tiles))
            patch_lua_blocks: list[str] = []
            for comm, pts in sorted(surface.resource_patches.items()):
                coords_lua = ", ".join(f"{{{x}, {y}}}" for x, y in sorted(pts))
                patch_lua_blocks.append(
                    f"  for _, pt in ipairs({{{coords_lua}}}) do\n"
                    f"    surface.create_entity({{name = '{comm}', position = {{pt[1] + 0.5, pt[2] + 0.5}}, amount = 5000}})\n"
                    f"  end"
                )
            patches_lua = "\n".join(patch_lua_blocks)

            e1_x = float(res.entities[0]["position"]["x"])
            e1_y = float(res.entities[0]["position"]["y"])

            ents_json = json.dumps(res.entities)
            control_lua = f"""local ENTS_JSON = [==[{ents_json}]==]
local function setup_world()
  if storage.setup_done then return end
  storage.setup_done = true
  local surface = game.surfaces[1]
  local force = game.forces.player
  for _, t_name in ipairs({{'steam-power', 'electronics', 'automation-science-pack', 'automation', 'logistics'}}) do
    if force.technologies[t_name] then
      force.technologies[t_name].researched = true
    end
  end
  force.add_research('steel-processing')
  for cx = -1, 3 do
    for cy = -1, 3 do
      surface.request_to_generate_chunks({{cx * 32, cy * 32}}, 1)
    end
  end
  surface.force_generate_chunk_requests()

  local land_tiles = {{}}
  for x = -16, 96 do
    for y = -16, 80 do
      land_tiles[#land_tiles + 1] = {{name = 'grass-1', position = {{x, y}}}}
    end
  end
  surface.set_tiles(land_tiles)

  local water_tiles = {{}}
  for _, pt in ipairs({{{water_lua}}}) do
    water_tiles[#water_tiles + 1] = {{name = 'water', position = {{pt[1], pt[2]}}}}
  end
  surface.set_tiles(water_tiles)

  for _, ent in pairs(surface.find_entities_filtered({{area = {{{{-16, -16}}, {{96, 80}}}}}})) do
    if ent.name ~= 'character' then
      ent.destroy()
    end
  end

{patches_lua}

  local ents = helpers.json_to_table(ENTS_JSON)
  for _, e in ipairs(ents) do
    local params = {{
      name = e.name,
      position = e.position,
      direction = e.direction or 0,
      force = force,
      raise_built = true
    }}
    if e.recipe then
      params.recipe = e.recipe
    end
    if e.type then
      params.type = e.type
    end
    surface.create_entity(params)
  end

  -- Seed 5 coal into the boiler to bootstrap the steam engine so electric drills start mining coal
  for _, b in pairs(surface.find_entities_filtered({{name = 'boiler'}})) do
    b.insert({{name = 'coal', count = 5}})
  end
end

script.on_init(setup_world)

script.on_nth_tick(500, function(e)
  setup_world()
  if e.tick == 0 then return end
  local surface = game.surfaces[1]
  local drills_active = 0
  for _, d in pairs(surface.find_entities_filtered({{name = 'electric-mining-drill'}})) do
    if d.mining_progress > 0 or d.status == defines.entity_status.working then
      drills_active = drills_active + 1
    end
  end
  local plates_made = 0
  for _, f in pairs(surface.find_entities_filtered({{name = 'stone-furnace'}})) do
    plates_made = plates_made + (f.products_finished or 0)
  end
  local asm_made = {{}}
  local asm_details = {{}}
  for _, a in pairs(surface.find_entities_filtered({{name = 'assembling-machine-1'}})) do
    local rec = a.get_recipe()
    if rec then
      asm_made[rec.name] = (asm_made[rec.name] or 0) + (a.products_finished or 0)
      local in_inv = a.get_inventory(defines.inventory.crafter_input)
      local out_inv = a.get_inventory(defines.inventory.crafter_output)
      asm_details[#asm_details + 1] = {{
        rec = rec.name,
        pos = a.position,
        status = a.status,
        made = a.products_finished,
        in_contents = in_inv and in_inv.get_contents() or {{}},
        out_contents = out_inv and out_inv.get_contents() or {{}}
      }}
    end
  end
  local furnace_details = {{}}
  for _, f in pairs(surface.find_entities_filtered({{name = 'stone-furnace'}})) do
    local src_inv = f.get_inventory(defines.inventory.crafter_input)
    local dst_inv = f.get_inventory(defines.inventory.crafter_output)
    local fuel_inv = f.get_inventory(defines.inventory.fuel)
    furnace_details[#furnace_details + 1] = {{
      pos = f.position,
      status = f.status,
      made = f.products_finished,
      src = src_inv and src_inv.get_contents() or {{}},
      dst = dst_inv and dst_inv.get_contents() or {{}},
      fuel = fuel_inv and fuel_inv.get_contents() or {{}}
    }}
  end
  local ghosts_left = #surface.find_entities_filtered({{name = 'entity-ghost'}})
  local pumps = #surface.find_entities_filtered({{name = 'offshore-pump'}})
  local boilers = #surface.find_entities_filtered({{name = 'boiler'}})
  local engines = #surface.find_entities_filtered({{name = 'steam-engine'}})
  local drills = surface.find_entities_filtered({{name = 'electric-mining-drill'}})
  local drill_statuses = {{}}
  for _, d in pairs(drills) do
    drill_statuses[#drill_statuses + 1] = {{pos = d.position, status = d.status, energy = d.energy, progress = d.mining_progress}}
  end
  local boiler_info = {{}}
  for _, b in pairs(surface.find_entities_filtered({{name = 'boiler'}})) do
    boiler_info[#boiler_info + 1] = {{pos = b.position, status = b.status, energy = b.energy}}
  end
  local belt_items = {{}}
  for _, b in pairs(surface.find_entities_filtered({{name = {{'transport-belt', 'splitter', 'underground-belt'}}}})) do
    local total = 0
    for l = 1, b.get_max_transport_line_index() do
      total = total + #b.get_transport_line(l)
    end
    if total > 0 then
      belt_items[#belt_items + 1] = {{name = b.name, pos = b.position, dir = b.direction, items = total}}
    end
  end
  helpers.write_file('patch_to_base_report.json', helpers.table_to_json({{
    tick = e.tick,
    ghosts_left = ghosts_left,
    pumps = pumps,
    boilers = boilers,
    engines = engines,
    drills_total = #drills,
    drills_active = drills_active,
    drill_statuses = drill_statuses,
    boiler_info = boiler_info,
    plates_made = plates_made,
    asm_made = asm_made,
    asm_details = asm_details,
    furnace_details = furnace_details,
    belt_items = belt_items
  }}) .. '\\n', false)
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
            save_zip = tmp_path / "saves" / f"{scenario_name}.zip"
            png_out = render_blueprint(res.blueprint_string, tmp_path / "starter_base.png")
            self.assertTrue(png_out.is_file())
            self.assertGreater(png_out.stat().st_size, 0)
            export_dir = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", ""))
            if export_dir.is_dir():
                shutil.copy2(save_zip, export_dir / "starter_base_sim.zip")
                shutil.copy2(png_out, export_dir / "starter_base.png")
                (export_dir / "starter_base.blueprint").write_text(
                    res.blueprint_string + "\n", encoding="utf-8"
                )
            subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--benchmark",
                    str(save_zip),
                    "--benchmark-ticks",
                    "8000",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            report_file = tmp_path / "script-output" / "patch_to_base_report.json"
            self.assertTrue(report_file.is_file())

            report = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertGreater(report["drills_active"], 0, msg=f"Report: {report}")
            self.assertGreater(report["plates_made"], 0, msg=f"Report: {report}")
            self.assertGreater(
                report["asm_made"].get("iron-gear-wheel", 0), 0, msg=f"Report: {report}"
            )
            self.assertGreater(
                report["asm_made"].get("transport-belt", 0), 0, msg=f"Report: {report}"
            )
            self.assertGreater(
                report["asm_made"].get("automation-science-pack", 0), 0, msg=f"Report: {report}"
            )


if __name__ == "__main__":
    unittest.main()
