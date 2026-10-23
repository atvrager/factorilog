"""Unit and end-to-end headless Factorio tests for the autonomous starter base bot agent."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from foundry import solve
from foundry.bp2save import find_factorio_binary
from foundry.campaign.bot_agent import (
    ALL_BUILD_PHASES,
    PHASE_CLEAR_OBSTACLES,
    PHASE_PATCH_MINERS,
    PHASE_POWER_NETWORK,
    PHASE_SHORELINE_POWER,
    PHASE_STARTER_BASE_CORE,
    PHASE_TRUNK_BELTS,
    TASK_CLEAR_OBSTACLE,
    TASK_CONFIGURE_RECIPE,
    TASK_PLACE_ENTITY,
    TASK_SEED_FUEL,
    BotAgent,
    compute_8way_walk_vector,
    encode_task_input_actions,
    extract_character_reach,
)
from foundry.campaign.cartographer import build_lua_cartography_scanner_snippet
from foundry.mp_proxy import (
    ACTION_BEGIN_MINING,
    ACTION_BUILD,
    ACTION_CLEAR_CURSOR,
    ACTION_FAST_ENTITY_TRANSFER,
    ACTION_PIPETTE,
    ACTION_ROTATE_ENTITY,
    ACTION_SELECTED_ENTITY_CHANGED,
    ACTION_SETUP_ASSEMBLING_MACHINE,
    FactorioLockstepProxy,
    PlayerWorldState,
    XvfbVideoRecorder,
    encode_timelapse_mp4,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "dump-fixture.json"


def _sample_cartography_payload() -> dict[str, object]:
    """Return a representative cartography telemetry payload with patches, water, cliff, and trees."""
    return {
        "tick": 60,
        "cartography": {
            "charted_chunks": [[0, 0], [1, 0], [0, 1], [1, 1]],
            "water_tiles": [[x, y] for x in range(8, 13) for y in range(2, 6)],
            "resource_patches": {
                "coal": [[x, y, 5000] for x in range(4, 10) for y in range(14, 20)],
                "iron-ore": [[x, y, 5000] for x in range(4, 10) for y in range(25, 31)],
                "copper-ore": [[x, y, 5000] for x in range(4, 10) for y in range(36, 42)],
            },
            "obstacles": [
                {
                    "name": "cliff",
                    "type": "cliff",
                    "position": {"x": 20.0, "y": 18.0},
                    "bounding_box": [[18.1, 16.1], [21.9, 19.9]],
                    "unit_number": 101,
                    "cliff_orientation": "north-to-south",
                },
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


class BotAgentTest(unittest.TestCase):
    """Tests for BotAgent starter base synthesis, reach validation, and action execution."""

    data: solve.Data

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = solve.Data(str(FIXTURE_PATH))

    def test_extract_character_reach(self) -> None:
        """Verify dynamic character reach extraction from prototype data."""
        reach = extract_character_reach(self.data)
        self.assertGreater(reach.build_reach, 0.0)
        self.assertGreater(reach.entity_reach, 0.0)
        self.assertGreater(reach.resource_reach, 0.0)
        self.assertGreater(reach.running_rate, 0.0)
        self.assertLessEqual(reach.resource_reach, reach.build_reach)

    def test_plan_starter_base_from_cartographer_and_task_ordering(self) -> None:
        """Verify cartographer-driven starter base planning, reach limits, and phase ordering."""
        bot = BotAgent(data=self.data, researched_techs={"automation", "logistics"})
        self.assertTrue(bot.ingest_cartography(_sample_cartography_payload()))

        plan = bot.synthesize_plan(
            reference_pos=(10.5, 10.5),
            bounds=(0, 64, 0, 52),
            base_origin=(28, 16),
            verify=False,
        )

        # 1. Verify bill of materials matches total planned entities
        self.assertEqual(sum(plan.bill_of_materials.values()), len(plan.layout.entities))
        self.assertIn("offshore-pump", plan.bill_of_materials)
        self.assertIn("boiler", plan.bill_of_materials)
        self.assertIn("steam-engine", plan.bill_of_materials)
        self.assertIn("electric-mining-drill", plan.bill_of_materials)
        self.assertIn("stone-furnace", plan.bill_of_materials)
        self.assertIn("assembling-machine-1", plan.bill_of_materials)
        self.assertIn("lab", plan.bill_of_materials)
        self.assertIn("splitter", plan.bill_of_materials)

        # 2. Verify the plan schedules pre-clearing only for colliding clearable obstacles
        clear_tasks = [t for t in plan.tasks if t.task_kind == TASK_CLEAR_OBSTACLE]
        self.assertEqual({t.unit_number for t in clear_tasks}, {202, 203})

        # 3. Verify phases appear in non-decreasing dependency order
        phase_rank = {name: idx for idx, name in enumerate(ALL_BUILD_PHASES)}
        observed_ranks = [phase_rank[t.phase] for t in plan.tasks]
        self.assertEqual(observed_ranks, sorted(observed_ranks))
        for required_phase in (
            PHASE_CLEAR_OBSTACLES,
            PHASE_SHORELINE_POWER,
            PHASE_PATCH_MINERS,
            PHASE_POWER_NETWORK,
            PHASE_TRUNK_BELTS,
            PHASE_STARTER_BASE_CORE,
        ):
            self.assertIn(required_phase, {t.phase for t in plan.tasks})

        # 4. Verify the plan schedules boiler fuel seeding right after boiler placement in Phase 1
        seed_tasks = [t for t in plan.tasks if t.task_kind == TASK_SEED_FUEL]
        self.assertEqual(len(seed_tasks), 1)
        self.assertEqual(seed_tasks[0].phase, PHASE_SHORELINE_POWER)
        self.assertEqual(seed_tasks[0].target_name, plan.fuel_commodity)

        # 5. Verify assembler recipe configuration tasks exist for all assemblers
        recipe_tasks = [t for t in plan.tasks if t.task_kind == TASK_CONFIGURE_RECIPE]
        configured_recipes = {t.recipe for t in recipe_tasks}
        self.assertIn("iron-gear-wheel", configured_recipes)
        self.assertIn("transport-belt", configured_recipes)
        self.assertIn("automation-science-pack", configured_recipes)

        # 6. Verify every task standing position is outside water/hard obstacles and within reach
        for task in plan.tasks:
            stand_tile = (math.floor(task.stand_x), math.floor(task.stand_y))
            self.assertNotIn(stand_tile, plan.surface.water_tiles)
            self.assertNotIn(stand_tile, plan.surface.obstacles)
            dist = math.hypot(task.target_x - task.stand_x, task.target_y - task.stand_y)
            if task.task_kind == TASK_CLEAR_OBSTACLE:
                self.assertLessEqual(dist, plan.reach_limits.resource_reach)
            else:
                self.assertLessEqual(dist, plan.reach_limits.build_reach)

    def test_encode_task_input_actions_and_lockstep_stepping(self) -> None:
        """Verify UDP InputAction encoding and step-by-step FactorioLockstepProxy control."""
        bot = BotAgent(data=self.data, researched_techs={"automation", "logistics"})
        bot.ingest_cartography(_sample_cartography_payload())
        plan = bot.synthesize_plan(
            reference_pos=(10.5, 10.5),
            bounds=(0, 64, 0, 52),
            base_origin=(28, 16),
            verify=False,
        )

        # Build synthetic ID mapping table for all items and recipes in the plan
        item_names = sorted(
            set(plan.bill_of_materials.keys()) | {plan.fuel_commodity, "underground-belt"}
        )
        recipe_names = sorted(
            {t.recipe for t in plan.tasks if t.recipe is not None} | {"automation-science-pack"}
        )
        id_mappings = {
            "item": {name: idx + 1 for idx, name in enumerate(item_names)},
            "recipe": {name: idx + 1 for idx, name in enumerate(recipe_names)},
        }

        for task in plan.tasks:
            encoded = encode_task_input_actions(task, id_mappings)
            self.assertGreaterEqual(len(encoded), 2)
            action_types = [act_type for act_type, _payload in encoded]
            if task.task_kind == TASK_CLEAR_OBSTACLE:
                self.assertEqual(
                    action_types,
                    [ACTION_SELECTED_ENTITY_CHANGED, ACTION_BEGIN_MINING],
                )
            elif task.task_kind == TASK_PLACE_ENTITY:
                self.assertEqual(action_types[0], ACTION_PIPETTE)
                self.assertEqual(action_types[1], ACTION_BUILD)
                self.assertEqual(action_types[-1], ACTION_CLEAR_CURSOR)
                if task.entity_type == "output":
                    self.assertIn(ACTION_ROTATE_ENTITY, action_types)
            elif task.task_kind == TASK_CONFIGURE_RECIPE:
                self.assertEqual(
                    action_types,
                    [ACTION_SELECTED_ENTITY_CHANGED, ACTION_SETUP_ASSEMBLING_MACHINE],
                )
            elif task.task_kind == TASK_SEED_FUEL:
                self.assertEqual(
                    action_types,
                    [
                        ACTION_PIPETTE,
                        ACTION_SELECTED_ENTITY_CHANGED,
                        ACTION_FAST_ENTITY_TRANSFER,
                        ACTION_CLEAR_CURSOR,
                    ],
                )

        # Test walking vector and lockstep proxy stepping on the first two tasks
        proxy = FactorioLockstepProxy("127.0.0.1", 0, "127.0.0.1", 34197)
        first_task = bot.current_task()
        assert first_task is not None

        far_state = PlayerWorldState(
            tick=10,
            player_x=first_task.stand_x - 5.0,
            player_y=first_task.stand_y - 5.0,
            walking=False,
            mining=False,
            selected_name=None,
            selected_x=None,
            selected_y=None,
            inventory=dict(plan.bill_of_materials),
            nearest_resource=None,
        )
        status1 = bot.step_lockstep_proxy(proxy, far_state, id_mappings)
        self.assertTrue(status1.startswith("walking_to_task_"))
        self.assertGreater(proxy.pending_action_count(), 0)

        # Clear pending queue to simulate heartbeat consumption, then arrive at stand_pos
        proxy._pending_injections.clear()
        arrived_state = PlayerWorldState(
            tick=20,
            player_x=first_task.stand_x,
            player_y=first_task.stand_y,
            walking=True,
            mining=False,
            selected_name=None,
            selected_x=None,
            selected_y=None,
            inventory=dict(plan.bill_of_materials),
            nearest_resource=None,
        )
        status2 = bot.step_lockstep_proxy(proxy, arrived_state, id_mappings)
        self.assertEqual(status2, f"mining_obstacle_{first_task.task_index}")

        proxy._pending_injections.clear()
        finished_mining_state = PlayerWorldState(
            tick=40,
            player_x=first_task.stand_x,
            player_y=first_task.stand_y,
            walking=False,
            mining=False,
            selected_name=None,
            selected_x=None,
            selected_y=None,
            inventory=dict(plan.bill_of_materials),
            nearest_resource=None,
        )
        status3 = bot.step_lockstep_proxy(proxy, finished_mining_state, id_mappings)
        self.assertEqual(status3, f"cleared_obstacle_{first_task.task_index}")
        self.assertIn(first_task.task_index, bot.completed_task_indices)

        # Verify 8-way walk vector helper
        self.assertIsNone(compute_8way_walk_vector(10.0, 10.0, 10.2, 10.2))
        self.assertEqual(compute_8way_walk_vector(10.0, 10.0, 15.0, 10.0), (1.0, 0.0))

    def test_headless_factorio_cartographer_to_bot_starter_base_e2e(self) -> None:
        """Verify live Factorio cartography scan, bot plan synthesis, obstacle clearing, and base production."""
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

            # Stage 1: Scan map surface with water, 3 patches, cliff, and colliding tree/rock
            scan_scenario = "bot_cartography_scan"
            scan_dir = tmp_path / "scenarios" / scan_scenario
            scan_dir.mkdir(parents=True, exist_ok=True)
            (scan_dir / "description.json").write_text(
                json.dumps({"order": "a", "multiplayer-compatible": True, "is-main-game": True})
            )

            surface_setup_lua = """  local surface = game.surfaces[1]
  for cx = -2, 3 do
    for cy = -2, 3 do
      surface.request_to_generate_chunks({cx * 32, cy * 32}, 1)
    end
  end
  surface.force_generate_chunk_requests()

  local land_tiles = {}
  for x = -48, 96 do
    for y = -48, 96 do
      land_tiles[#land_tiles + 1] = {name = 'grass-1', position = {x, y}}
    end
  end
  surface.set_tiles(land_tiles)

  local water_tiles = {}
  for x = 8, 12 do
    for y = 2, 5 do
      water_tiles[#water_tiles + 1] = {name = 'water', position = {x, y}}
    end
  end
  surface.set_tiles(water_tiles)

  for _, ent in pairs(surface.find_entities_filtered({area = {{-48, -48}, {96, 96}}})) do
    if ent.name ~= 'character' then
      ent.destroy()
    end
  end

  for x = 4, 9 do
    for y = 14, 19 do
      surface.create_entity({name = 'coal', position = {x + 0.5, y + 0.5}, amount = 4000})
    end
    for y = 25, 30 do
      surface.create_entity({name = 'iron-ore', position = {x + 0.5, y + 0.5}, amount = 4000})
    end
    for y = 36, 41 do
      surface.create_entity({name = 'copper-ore', position = {x + 0.5, y + 0.5}, amount = 4000})
    end
  end

  surface.create_entity({name = 'cliff', position = {20.0, 18.0}, cliff_orientation = 'north-to-south'})
  surface.create_entity({name = 'tree-01', position = {30.5, 17.5}})
  surface.create_entity({name = 'big-rock', position = {33.0, 20.0}})
"""

            scan_control_lua = (
                "script.on_init(function()\n"
                + surface_setup_lua
                + "  local p = {position = {x = 24, y = 24}, surface = surface}\n"
                + scanner_lua
                + "  helpers.write_file('cartography_dump.json', helpers.table_to_json({cartography = storage.cartography}) .. '\\n', false)\n"
                + "end)\n"
            )
            (scan_dir / "control.lua").write_text(scan_control_lua)
            proc_scan = subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--scenario2map",
                    scan_scenario,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc_scan.returncode,
                0,
                msg=f"STDOUT:\n{proc_scan.stdout}\nSTDERR:\n{proc_scan.stderr}",
            )

            dump_file = tmp_path / "script-output" / "cartography_dump.json"
            self.assertTrue(dump_file.is_file())

            # Stage 2: BotAgent ingests live cartography dump and synthesizes starter base plan
            bot = BotAgent(data=self.data, researched_techs={"automation", "logistics"})
            self.assertTrue(bot.ingest_cartography(dump_file))
            plan = bot.synthesize_plan(
                reference_pos=(10.5, 10.5),
                bounds=(0, 64, 0, 52),
                base_origin=(28, 16),
                verify=False,
            )
            self.assertGreaterEqual(len(plan.obstacles_to_clear), 2)
            progressive_lua = bot.build_lua_progressive_plan_executor_snippet(step_stride=0.15)

            # Stage 3: Execute the progressive bot plan across ticks and verify full production
            build_scenario = "bot_starter_base_exec"
            build_dir = tmp_path / "scenarios" / build_scenario
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "description.json").write_text(
                json.dumps({"order": "a", "multiplayer-compatible": True, "is-main-game": True})
            )

            build_control_lua = (
                progressive_lua
                + "\nlocal function ensure_world_initialized()\n"
                + "  if storage.world_initialized then return end\n"
                + "  storage.world_initialized = true\n"
                + surface_setup_lua
                + "  local force = game.forces['player']\n"
                + "  for _, t_name in ipairs({'steam-power', 'electronics', 'automation-science-pack', 'automation', 'logistics'}) do\n"
                + "    if force.technologies[t_name] then\n"
                + "      force.technologies[t_name].researched = true\n"
                + "    end\n"
                + "  end\n"
                + "  if force.technologies['steel-processing'] then\n"
                + "    force.add_research('steel-processing')\n"
                + "  end\n"
                + "  surface.daytime = 0.0\n"
                + "  surface.freeze_daytime = true\n"
                + "  if #surface.find_entities_filtered({name = 'character'}) == 0 then\n"
                + "    surface.create_entity({name = 'character', position = {24.5, 18.5}, force = force})\n"
                + "  end\n"
                + "end\n\n"
                + "script.on_init(ensure_world_initialized)\n\n"
                + "script.on_event(defines.events.on_tick, function(event)\n"
                + "  ensure_world_initialized()\n"
                + "  if game.is_multiplayer() and #game.connected_players == 0 then return end\n"
                + "  local surface = game.surfaces[1]\n"
                + "  local force = game.forces['player']\n"
                + "  local p = game.get_player(1)\n"
                + "  if p and p.valid then\n"
                + "    p.game_view_settings.show_entity_info = true\n"
                + "    if not storage.mp_speed_set then\n"
                + "      storage.mp_speed_set = true\n"
                + "      game.speed = 1\n"
                + "      for _, c in pairs(surface.find_entities_filtered({name = 'character'})) do\n"
                + "        if p.character and c.unit_number ~= p.character.unit_number then c.destroy() end\n"
                + "      end\n"
                + "      if p.character then p.character.teleport({24.5, 18.5}) end\n"
                + "    end\n"
                + "  end\n"
                + "  storage.active_ticks = (storage.active_ticks or 0) + 1\n"
                + "  if not game.is_multiplayer() or storage.active_ticks > 60 then\n"
                + "    step_bot_plan(surface, force)\n"
                + "  end\n"
                + "  local building = not (storage.bot_execution and storage.bot_execution.completed)\n"
                + "  if p and p.valid and storage.active_ticks % 3 == 0 and (storage.frame_seq or 0) < 1360 then\n"
                + "    storage.frame_seq = (storage.frame_seq or 0) + 1\n"
                + "    local char_ent = p.character or surface.find_entities_filtered({name = 'character'})[1]\n"
                + "    local tx = (char_ent and char_ent.valid and building) and char_ent.position.x or 31.5\n"
                + "    local ty = (char_ent and char_ent.valid and building) and char_ent.position.y or 22.5\n"
                + "    storage.cam_x = storage.cam_x and (storage.cam_x + (tx - storage.cam_x) * 0.12) or tx\n"
                + "    storage.cam_y = storage.cam_y and (storage.cam_y + (ty - storage.cam_y) * 0.12) or ty\n"
                + "    game.take_screenshot({\n"
                + "      player = p,\n"
                + "      by_player = p,\n"
                + "      position = {x = 24, y = 21},\n"
                + "      resolution = {1280, 800},\n"
                + "      zoom = 0.56,\n"
                + "      quality = 80,\n"
                + "      daytime = 0.0,\n"
                + "      show_gui = false,\n"
                + "      show_entity_info = true,\n"
                + "      path = string.format('timelapse_overview/frame_%08d.jpg', storage.frame_seq)\n"
                + "    })\n"
                + "    game.take_screenshot({\n"
                + "      player = p,\n"
                + "      by_player = p,\n"
                + "      position = {x = storage.cam_x, y = storage.cam_y},\n"
                + "      resolution = {1280, 800},\n"
                + "      zoom = 0.88,\n"
                + "      quality = 80,\n"
                + "      daytime = 0.0,\n"
                + "      show_gui = false,\n"
                + "      show_entity_info = true,\n"
                + "      path = string.format('timelapse_follow/frame_%08d.jpg', storage.frame_seq)\n"
                + "    })\n"
                + "  end\n"
                + "end)\n\n"
                + "script.on_nth_tick(500, function(event)\n"
                + "  if event.tick == 0 then return end\n"
                + "  local surface = game.surfaces[1]\n"
                + "  local drills_active = 0\n"
                + "  for _, d in pairs(surface.find_entities_filtered({name = 'electric-mining-drill'})) do\n"
                + "    if d.mining_progress > 0 or d.status == defines.entity_status.working then\n"
                + "      drills_active = drills_active + 1\n"
                + "    end\n"
                + "  end\n"
                + "  local plates_made = 0\n"
                + "  for _, f in pairs(surface.find_entities_filtered({name = 'stone-furnace'})) do\n"
                + "    plates_made = plates_made + (f.products_finished or 0)\n"
                + "  end\n"
                + "  local asm_made = {}\n"
                + "  for _, a in pairs(surface.find_entities_filtered({name = 'assembling-machine-1'})) do\n"
                + "    local rec = a.get_recipe()\n"
                + "    if rec then\n"
                + "      asm_made[rec.name] = (asm_made[rec.name] or 0) + (a.products_finished or 0)\n"
                + "    end\n"
                + "  end\n"
                + "  local out = {\n"
                + "    tick = event.tick,\n"
                + "    bot_execution = storage.bot_execution,\n"
                + "    drills_active = drills_active,\n"
                + "    plates_made = plates_made,\n"
                + "    asm_made = asm_made,\n"
                + "  }\n"
                + "  helpers.write_file('bot_sim_stats.json', helpers.table_to_json(out) .. '\\n', false)\n"
                + "end)\n"
            )
            (build_dir / "control.lua").write_text(build_control_lua)

            proc_map = subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--scenario2map",
                    build_scenario,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc_map.returncode,
                0,
                msg=f"STDOUT:\n{proc_map.stdout}\nSTDERR:\n{proc_map.stderr}",
            )

            save_zip = tmp_path / "saves" / f"{build_scenario}.zip"
            export_dir = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", ""))
            if export_dir.is_dir():
                shutil.copy2(save_zip, export_dir / "starter_base_bot_save.zip")

            proc_sim = subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--benchmark",
                    str(save_zip),
                    "--benchmark-ticks",
                    "10000",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc_sim.returncode,
                0,
                msg=f"STDOUT:\n{proc_sim.stdout}\nSTDERR:\n{proc_sim.stderr}",
            )

            stats_file = tmp_path / "script-output" / "bot_sim_stats.json"
            self.assertTrue(stats_file.is_file())
            sim_stats = json.loads(stats_file.read_text())

            exec_info = sim_stats["bot_execution"]
            self.assertTrue(exec_info["completed"], msg=f"Exec info: {exec_info}")
            self.assertGreaterEqual(exec_info["cleared_count"], 2)
            self.assertEqual(exec_info["placed_count"], len(plan.layout.entities))
            self.assertGreaterEqual(exec_info["seeded_count"], 1)

            self.assertGreater(sim_stats["drills_active"], 0, msg=f"Stats: {sim_stats}")
            self.assertGreater(sim_stats["plates_made"], 0, msg=f"Stats: {sim_stats}")
            self.assertGreater(
                sim_stats["asm_made"].get("iron-gear-wheel", 0), 0, msg=f"Stats: {sim_stats}"
            )
            self.assertGreater(
                sim_stats["asm_made"].get("transport-belt", 0), 0, msg=f"Stats: {sim_stats}"
            )
            self.assertGreater(
                sim_stats["asm_made"].get("automation-science-pack", 0),
                0,
                msg=f"Stats: {sim_stats}",
            )

            if (
                os.environ.get("RECORD_BOT_VIDEOS") == "1"
                and export_dir.is_dir()
                and shutil.which("Xvfb")
                and shutil.which("ffmpeg")
            ):
                self._record_bot_videos(
                    factorio_bin=Path(factorio_bin),
                    tmp_path=tmp_path,
                    mods_dir=mods_dir,
                    save_zip=save_zip,
                    export_dir=export_dir,
                )

    def test_bot_synthesizes_and_builds_on_real_freeplay_map(self) -> None:
        """Verify Cartographer and automatic site selection on the real seed-42 Freeplay map."""
        from foundry.entity_helpers import (
            check_collisions,
            check_power,
            check_underground_pairing,
            get_pole_supply_radius,
        )

        try:
            factorio_bin = find_factorio_binary()
        except FileNotFoundError:
            self.skipTest("Factorio binary not available for headless simulation")

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

            map_gen = {"width": 256, "height": 256, "seed": 42}
            map_gen_file = tmp_path / "map-gen-settings.json"
            map_gen_file.write_text(json.dumps(map_gen, indent=2))

            base_save = tmp_path / "saves" / "freeplay_42.zip"
            base_save.parent.mkdir(parents=True, exist_ok=True)
            proc_create = subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--create",
                    str(base_save),
                    "--map-gen-settings",
                    str(map_gen_file),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc_create.returncode,
                0,
                msg=f"STDOUT:\n{proc_create.stdout}\nSTDERR:\n{proc_create.stderr}",
            )

            scan_save = tmp_path / "saves" / "freeplay_42_scan.zip"
            shutil.copy2(base_save, scan_save)
            scanner_lua = build_lua_cartography_scanner_snippet(radius=120)
            scan_hook = (
                "\nlocal handler = require('event_handler')\n"
                "handler.add_lib({\n"
                "  on_nth_tick = {\n"
                "    [1] = function(e)\n"
                "      local p = nil\n"
                + scanner_lua
                + "      helpers.write_file('freeplay_cartography.json', helpers.table_to_json({cartography = storage.cartography}) .. '\\n', false)\n"
                "    end\n"
                "  }\n"
                "})\n"
            )
            self._append_lua_to_save(scan_save, scan_hook)

            proc_scan = subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--benchmark",
                    str(scan_save),
                    "--benchmark-ticks",
                    "5",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc_scan.returncode,
                0,
                msg=f"STDOUT:\n{proc_scan.stdout}\nSTDERR:\n{proc_scan.stderr}",
            )

            dump_file = tmp_path / "script-output" / "freeplay_cartography.json"
            self.assertTrue(dump_file.is_file())

            bot = BotAgent(data=self.data, researched_techs={"automation", "logistics"})
            self.assertTrue(bot.ingest_cartography(dump_file))
            self.assertGreater(len(bot.cartographer.water_tiles), 50)
            for comm in ("coal", "iron-ore", "copper-ore", "stone"):
                self.assertIn(comm, bot.cartographer.resource_patches)
                self.assertGreater(len(bot.cartographer.resource_patches[comm]), 20)

            # Synthesize plan with automatic site selection (base_origin=None, bounds=None)
            plan = bot.synthesize_plan(
                reference_pos=(0.0, 0.0),
                bounds=None,
                base_origin=None,
                verify=False,
            )
            self.assertEqual(len(plan.layout.trunk_nets), 4)
            for route in plan.layout.trunk_routes.values():
                self.assertTrue(route.is_success)
            self.assertEqual(check_collisions(plan.layout.entities), [])
            self.assertEqual(check_underground_pairing(plan.layout.entities), [])
            supply_rad = get_pole_supply_radius("small-electric-pole")
            self.assertEqual(check_power(plan.layout.entities, supply_rad), [])

            exec_save = tmp_path / "saves" / "freeplay_42_exec.zip"
            shutil.copy2(base_save, exec_save)
            executor_lua = bot.build_lua_plan_executor_snippet()
            ox, oy = plan.layout.base_core.bounds[0], plan.layout.base_core.bounds[1]
            exec_hook = (
                "\nlocal handler = require('event_handler')\n"
                "handler.add_lib({\n"
                "  on_nth_tick = {\n"
                "    [1] = function(e)\n"
                "      if storage.fp_exec_done then return end\n"
                "      storage.fp_exec_done = true\n"
                "      local surface = game.surfaces[1]\n"
                "      surface.request_to_generate_chunks({0, 0}, 4)\n"
                "      surface.force_generate_chunk_requests()\n"
                "      local force = game.forces['player']\n"
                "      for _, t_name in ipairs({'steam-power', 'electronics', 'automation-science-pack', 'automation', 'logistics'}) do\n"
                "        if force.technologies[t_name] then\n"
                "          force.technologies[t_name].researched = true\n"
                "        end\n"
                "      end\n"
                "      if force.technologies['steel-processing'] then\n"
                "        force.add_research('steel-processing')\n"
                "      end\n"
                f"      surface.create_entity({{name = 'character', position = {{{ox}, {oy}}}, force = force}})\n"
                + executor_lua
                + "    end,\n"
                "    [500] = function(e)\n"
                "      if e.tick == 0 then return end\n"
                "      local surface = game.surfaces[1]\n"
                "      local drills_active = 0\n"
                "      for _, d in pairs(surface.find_entities_filtered({name = 'electric-mining-drill'})) do\n"
                "        if d.mining_progress > 0 or d.status == defines.entity_status.working then\n"
                "          drills_active = drills_active + 1\n"
                "        end\n"
                "      end\n"
                "      local plates_made = 0\n"
                "      for _, f in pairs(surface.find_entities_filtered({name = 'stone-furnace'})) do\n"
                "        plates_made = plates_made + (f.products_finished or 0)\n"
                "      end\n"
                "      local asm_made = {}\n"
                "      for _, a in pairs(surface.find_entities_filtered({name = 'assembling-machine-1'})) do\n"
                "        local rec = a.get_recipe()\n"
                "        if rec then\n"
                "          asm_made[rec.name] = (asm_made[rec.name] or 0) + (a.products_finished or 0)\n"
                "        end\n"
                "      end\n"
                "      helpers.write_file('freeplay_sim_stats.json', helpers.table_to_json({\n"
                "        tick = e.tick,\n"
                "        bot_execution = storage.bot_execution,\n"
                "        drills_active = drills_active,\n"
                "        plates_made = plates_made,\n"
                "        asm_made = asm_made,\n"
                "      }) .. '\\n', false)\n"
                "    end\n"
                "  }\n"
                "})\n"
            )
            self._append_lua_to_save(exec_save, exec_hook)

            export_dir = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", ""))
            if export_dir.is_dir():
                shutil.copy2(exec_save, export_dir / "freeplay_42_bot_save.zip")

            proc_sim = subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--benchmark",
                    str(exec_save),
                    "--benchmark-ticks",
                    "7200",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc_sim.returncode,
                0,
                msg=f"STDOUT:\n{proc_sim.stdout}\nSTDERR:\n{proc_sim.stderr}",
            )

            stats_file = tmp_path / "script-output" / "freeplay_sim_stats.json"
            self.assertTrue(stats_file.is_file())
            sim_stats = json.loads(stats_file.read_text())
            exec_info = sim_stats["bot_execution"]
            self.assertEqual(exec_info["placed_count"], len(plan.layout.entities))
            self.assertGreater(sim_stats["drills_active"], 0, msg=f"Stats: {sim_stats}")
            self.assertGreater(sim_stats["plates_made"], 0, msg=f"Stats: {sim_stats}")
            self.assertGreater(
                sim_stats["asm_made"].get("iron-gear-wheel", 0), 0, msg=f"Stats: {sim_stats}"
            )
            self.assertGreater(
                sim_stats["asm_made"].get("transport-belt", 0), 0, msg=f"Stats: {sim_stats}"
            )
            self.assertGreater(
                sim_stats["asm_made"].get("automation-science-pack", 0),
                0,
                msg=f"Stats: {sim_stats}",
            )

    @staticmethod
    def _append_lua_to_save(save_zip_path: Path, lua_hook: str) -> None:
        """Append lua_hook to control.lua inside save_zip_path."""
        import zipfile

        temp_zip = save_zip_path.with_suffix(".tmp.zip")
        with (
            zipfile.ZipFile(save_zip_path, "r") as zin,
            zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zout,
        ):
            for item in zin.infolist():
                raw = zin.read(item.filename)
                if item.filename.endswith("/control.lua") or item.filename == "control.lua":
                    raw = raw + lua_hook.encode("utf-8")
                zout.writestr(item, raw)
        temp_zip.replace(save_zip_path)

    def _record_bot_videos(
        self,
        factorio_bin: Path,
        tmp_path: Path,
        mods_dir: Path,
        save_zip: Path,
        export_dir: Path,
    ) -> None:
        """Record live HUD, overview, and follow-cam MP4 videos of the bot starter base build."""
        import socket

        elf_bin = factorio_bin.resolve().parent.parent / "x64_" / "factorio"
        if not elf_bin.is_file():
            elf_bin = factorio_bin.resolve()

        srv_dir = tmp_path / "vid_server"
        cli_dir = tmp_path / "vid_client"
        srv_home = tmp_path / "srv_home"
        cli_home = tmp_path / "cli_home"
        for d in (srv_dir, cli_dir, srv_home, cli_home):
            d.mkdir(parents=True, exist_ok=True)

        srv_cfg = srv_dir / "config.ini"
        srv_cfg.write_text(
            f"[path]\nread-data=__PATH__executable__/../../data\nwrite-data={srv_dir}\n"
        )
        cli_cfg = cli_dir / "config.ini"
        cli_cfg.write_text(
            f"[path]\nread-data=__PATH__executable__/../../data\nwrite-data={cli_dir}\n"
            f"[other]\ncheck_updates=false\n"
            f"[graphics]\nfull-screen=false\npreferred-screen-index=0\n"
        )
        (cli_dir / "player-data.json").write_text(
            json.dumps({"service-username": "factorilog-bot"}, indent=2)
        )

        vid_save = srv_dir / "saves" / "vid_sim.zip"
        vid_save.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(save_zip, vid_save)

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        srv_settings = srv_dir / "server-settings.json"
        srv_settings.write_text(
            json.dumps(
                {
                    "name": "bot-vid-srv",
                    "description": "Autonomous starter base bot video server",
                    "max_players": 2,
                    "visibility": {"public": False, "lan": False},
                    "require_user_verification": False,
                    "auto_pause": True,
                }
            )
        )

        live_mp4 = export_dir / "starter_base_bot_live.mp4"
        recorder = XvfbVideoRecorder(live_mp4, width=1280, height=800, fps=30)
        recorder.start_display()
        srv_proc: subprocess.Popen[str] | None = None
        cli_proc: subprocess.Popen[str] | None = None
        try:
            srv_proc = subprocess.Popen(
                [
                    str(elf_bin),
                    "-c",
                    str(srv_cfg),
                    "--mod-directory",
                    str(mods_dir),
                    "--bind",
                    f"127.0.0.1:{port}",
                    "--start-server",
                    str(vid_save),
                    "--server-settings",
                    str(srv_settings),
                ],
                env={
                    **os.environ,
                    "HOME": str(srv_home),
                    "SteamAppId": "427520",
                    "SteamGameId": "427520",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            srv_log = srv_dir / "factorio-current.log"
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if (
                    srv_log.is_file()
                    and "changing state from(CreatingGame) to(InGame)"
                    in srv_log.read_text(errors="replace")
                ):
                    break
                time.sleep(0.1)

            cli_proc = subprocess.Popen(
                [
                    str(elf_bin),
                    "-c",
                    str(cli_cfg),
                    "--mod-directory",
                    str(mods_dir),
                    "--disable-audio",
                    "--force-graphics-preset",
                    "very-low",
                    "--video-memory-usage",
                    "low",
                    "--low-vram",
                    "--window-size",
                    "1280x800",
                    "--mp-connect",
                    f"127.0.0.1:{port}",
                ],
                env={
                    **os.environ,
                    "HOME": str(cli_home),
                    "SteamAppId": "427520",
                    "SteamGameId": "427520",
                    "DISPLAY": recorder.display,
                    "WAYLAND_DISPLAY": "",
                    "SDL_VIDEODRIVER": "x11",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            cli_log = cli_dir / "factorio-current.log"
            join_deadline = time.monotonic() + 30.0
            while time.monotonic() < join_deadline:
                if cli_log.is_file() and "to(InGame)" in cli_log.read_text(errors="replace"):
                    break
                if cli_proc.poll() is not None or srv_proc.poll() is not None:
                    break
                time.sleep(0.1)

            recorder.start_recording()

            target_frame = cli_dir / "script-output" / "timelapse_overview" / "frame_00001360.jpg"
            wait_deadline = time.monotonic() + 160.0
            while time.monotonic() < wait_deadline:
                if target_frame.is_file():
                    break
                if cli_proc.poll() is not None or srv_proc.poll() is not None:
                    break
                time.sleep(0.25)
        finally:
            for p in (cli_proc, srv_proc):
                if p is not None and p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        p.kill()
            recorder.stop()

        overview_dir = cli_dir / "script-output" / "timelapse_overview"
        follow_dir = cli_dir / "script-output" / "timelapse_follow"
        if overview_dir.is_dir() and list(overview_dir.glob("frame_*.jpg")):
            encode_timelapse_mp4(
                overview_dir,
                export_dir / "starter_base_bot_overview.mp4",
                fps=20,
            )
        else:
            srv_txt = srv_proc.stdout.read() if srv_proc.stdout else ""
            cli_txt = cli_proc.stdout.read() if cli_proc and cli_proc.stdout else ""
            (export_dir / "vid_debug.log").write_text(
                f"=== SRV ===\n{srv_txt}\n=== CLI ===\n{cli_txt}\n"
            )
        if follow_dir.is_dir() and list(follow_dir.glob("frame_*.jpg")):
            encode_timelapse_mp4(
                follow_dir,
                export_dir / "starter_base_bot_follow.mp4",
                fps=20,
            )


if __name__ == "__main__":
    unittest.main()
