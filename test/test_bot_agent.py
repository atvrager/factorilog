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
    BootstrapController,
    BotAgent,
    CharacterNavigator,
    build_character_blocked_tiles,
    compute_8way_walk_vector,
    encode_assembler_batch_actions,
    encode_task_input_actions,
    extract_character_reach,
    find_character_walk_path,
    verify_placed_entities,
)
from foundry.campaign.cartographer import build_lua_cartography_scanner_snippet
from foundry.mp_proxy import (
    ACTION_BEGIN_MINING,
    ACTION_BUILD,
    ACTION_CLEAR_CURSOR,
    ACTION_CLOSE_GUI,
    ACTION_FAST_ENTITY_TRANSFER,
    ACTION_OPEN_GUI,
    ACTION_PIPETTE,
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
                self.assertEqual(
                    action_types,
                    [ACTION_PIPETTE, ACTION_BUILD, ACTION_CLEAR_CURSOR],
                )
            elif task.task_kind == TASK_CONFIGURE_RECIPE:
                self.assertEqual(
                    action_types,
                    [
                        ACTION_SELECTED_ENTITY_CHANGED,
                        ACTION_OPEN_GUI,
                        ACTION_SETUP_ASSEMBLING_MACHINE,
                        ACTION_CLOSE_GUI,
                    ],
                )
            elif task.task_kind == TASK_SEED_FUEL:
                self.assertEqual(
                    action_types,
                    [
                        ACTION_CLEAR_CURSOR,
                        ACTION_SELECTED_ENTITY_CHANGED,
                        ACTION_PIPETTE,
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

        # Verify inventory gating and bootstrap mall collection when an item is missing
        while True:
            cur_t = bot.current_task()
            assert cur_t is not None
            if cur_t.task_kind == TASK_PLACE_ENTITY:
                break
            bot.mark_task_complete(cur_t.task_index)

        place_task = bot.current_task()
        assert place_task is not None
        proxy._pending_injections.clear()
        empty_inv_state = PlayerWorldState(
            tick=50,
            player_x=place_task.stand_x,
            player_y=place_task.stand_y,
            walking=False,
            mining=False,
            selected_name=None,
            selected_x=None,
            selected_y=None,
            inventory={},
            nearest_resource=None,
        )
        status_wait = bot.step_lockstep_proxy(proxy, empty_inv_state, id_mappings)
        self.assertEqual(status_wait, f"waiting_for_inventory_{place_task.target_name}")
        self.assertNotIn(place_task.task_index, bot.completed_task_indices)

        # If a mall assembler producing transport-belt is already placed, missing transport-belt
        # triggers walking to and collecting from the mall assembler
        mall_asm_task = next(
            t
            for t in plan.tasks
            if t.task_kind == TASK_PLACE_ENTITY and t.recipe == "transport-belt"
        )
        bot.mark_task_complete(mall_asm_task.task_index)
        belt_place_task = next(
            t
            for t in plan.tasks
            if t.task_kind == TASK_PLACE_ENTITY
            and t.target_name == "transport-belt"
            and t.task_index not in bot.completed_task_indices
        )
        for t in plan.tasks:
            if t.task_index < belt_place_task.task_index:
                bot.mark_task_complete(t.task_index)

        proxy._pending_injections.clear()
        at_mall_state = PlayerWorldState(
            tick=60,
            player_x=mall_asm_task.stand_x,
            player_y=mall_asm_task.stand_y,
            walking=False,
            mining=False,
            selected_name=None,
            selected_x=None,
            selected_y=None,
            inventory={},
            nearest_resource=None,
        )
        status_collect = bot.step_lockstep_proxy(proxy, at_mall_state, id_mappings)
        self.assertEqual(status_collect, "collecting_mall_transport-belt")
        self.assertGreater(proxy.pending_action_count(), 0)

        # Verify 8-way walk vector helper
        self.assertIsNone(compute_8way_walk_vector(10.0, 10.0, 10.2, 10.2))
        self.assertEqual(compute_8way_walk_vector(10.0, 10.0, 15.0, 10.0), (1.0, 0.0))

        # Verify dynamic BOM fabrication plan calculation from prototype recipes
        fab_plan = bot.compute_bom_fabrication(
            starting_inventory={
                "offshore-pump": 1,
                "boiler": 1,
                "steam-engine": 1,
                "small-electric-pole": 1,
                "assembling-machine-1": 1,
                "stone-furnace": 3,
                "lab": 1,
            }
        )
        self.assertGreater(fab_plan.missing_bom.get("transport-belt", 0), 50)
        self.assertGreater(fab_plan.raw_materials_needed.get("iron-ore", 0), 100)
        self.assertGreater(fab_plan.raw_materials_needed.get("copper-ore", 0), 20)
        self.assertGreater(fab_plan.raw_materials_needed.get("wood", 0), 5)
        self.assertGreater(len(fab_plan.smelting_steps), 0)
        self.assertGreater(len(fab_plan.assembler_steps), 0)

        # Verify UDP InputAction batch encoding for lakeside assembler BOM fabrication
        batch_id_mappings = {
            "item": {"wood": 1, "copper-cable": 2, "iron-plate": 3, "copper-plate": 4},
            "recipe": {"small-electric-pole": 10},
        }
        batch_actions = encode_assembler_batch_actions(
            asm_x=12.0,
            asm_y=14.0,
            recipe_name="small-electric-pole",
            ingredients=("wood", "copper-cable"),
            id_mappings=batch_id_mappings,
        )
        batch_types = [act_type for act_type, _payload in batch_actions]
        self.assertIn(ACTION_SETUP_ASSEMBLING_MACHINE, batch_types)
        self.assertIn(ACTION_PIPETTE, batch_types)
        self.assertIn(ACTION_FAST_ENTITY_TRANSFER, batch_types)

        # Verify 100% entity placement verification helper against planned layout
        self.assertEqual(verify_placed_entities(plan.layout.entities, plan.layout.entities), [])
        missing_check = verify_placed_entities(plan.layout.entities, plan.layout.entities[:-1])
        self.assertEqual(len(missing_check), 1)
        self.assertIn("Missing", missing_check[0])

    def test_character_astar_pathfinding_and_navigator_physics(self) -> None:
        """Verify 8-way A* obstacle avoidance, no corner-cutting, speed scaling, and zero-inertia stops."""
        # 1. Straight line path compresses to a single goal waypoint
        open_path = find_character_walk_path(0.5, 0.5, 5.5, 0.5, blocked_tiles=frozenset())
        self.assertEqual(open_path, [(5.5, 0.5)])

        # 2. Vertical wall at x=2, y in {-1, 0, 1, 2} forces routing around y=3 or y=-2
        wall = {(2, y) for y in range(-1, 3)}
        detour = find_character_walk_path(0.5, 0.5, 4.5, 0.5, blocked_tiles=wall)
        self.assertGreater(len(detour), 1)
        self.assertEqual(detour[-1], (4.5, 0.5))
        for wx, wy in detour:
            self.assertNotIn((math.floor(wx), math.floor(wy)), wall)

        # 3. Block diagonal corner-cutting when an adjacent cardinal tile has an obstacle
        corner_block = {(1, 0)}
        no_cut = find_character_walk_path(0.5, 0.5, 1.5, -0.5, blocked_tiles=corner_block)
        self.assertEqual(no_cut, [(0.5, -0.5), (1.5, -0.5)])

        # 4. Approach speed scaling scales down vector magnitude near target
        far_vec = compute_8way_walk_vector(
            0.0, 0.0, 5.0, 0.0, stop_tolerance=0.5, scale_near_target=True
        )
        near_vec = compute_8way_walk_vector(
            0.0, 0.0, 0.8, 0.0, stop_tolerance=0.5, scale_near_target=True
        )
        assert far_vec is not None and near_vec is not None
        self.assertEqual(far_vec, (1.0, 0.0))
        self.assertLess(near_vec[0], 1.0)
        self.assertGreaterEqual(near_vec[0], 0.25)

        # 5. CharacterNavigator stops in one action without braking throttles and replans when stuck
        proxy = FactorioLockstepProxy("127.0.0.1", 0, "127.0.0.1", 34197)
        nav = CharacterNavigator()
        s0 = PlayerWorldState(
            tick=10,
            player_x=0.5,
            player_y=0.5,
            walking=False,
            mining=False,
            selected_name=None,
            selected_x=None,
            selected_y=None,
            inventory={},
            nearest_resource=None,
        )
        self.assertFalse(nav.step(proxy, s0, 5.5, 0.5, stop_tolerance=0.6))
        self.assertEqual(proxy.pending_action_count(), 1)
        proxy._pending_injections.clear()

        # Simulate stuck at (0.5, 0.5) for 24 ticks -> marks ahead tile blocked and replans
        s_stuck = PlayerWorldState(
            tick=34,
            player_x=0.5,
            player_y=0.5,
            walking=True,
            mining=False,
            selected_name=None,
            selected_x=None,
            selected_y=None,
            inventory={},
            nearest_resource=None,
        )
        self.assertFalse(nav.step(proxy, s_stuck, 5.5, 0.5, stop_tolerance=0.6))
        self.assertIn((1, 0), nav.dynamic_blocked_tiles)
        proxy._pending_injections.clear()

        # Simulate arrival at (5.3, 0.5) -> queues exactly 1 StopWalking action
        s_arrived = PlayerWorldState(
            tick=60,
            player_x=5.3,
            player_y=0.5,
            walking=True,
            mining=False,
            selected_name=None,
            selected_x=None,
            selected_y=None,
            inventory={},
            nearest_resource=None,
        )
        self.assertTrue(nav.step(proxy, s_arrived, 5.5, 0.5, stop_tolerance=0.6))
        self.assertEqual(proxy.pending_action_count(), 1)

        # Verify build_character_blocked_tiles excludes walkable transport-belt but blocks furnaces
        blocked_set = build_character_blocked_tiles(
            placed_entities=[
                {"name": "transport-belt", "x": 3.5, "y": 3.5, "direction": 0},
                {"name": "stone-furnace", "x": 6.0, "y": 6.0, "direction": 0},
            ],
            data=self.data,
        )
        self.assertNotIn((3, 3), blocked_set)
        self.assertIn((5, 5), blocked_set)
        self.assertIn((6, 6), blocked_set)

        # Verify BootstrapController initializes cleanly from a synthesized BotAgent
        bot = BotAgent(data=self.data, researched_techs={"automation", "logistics"})
        bot.ingest_cartography(_sample_cartography_payload())
        bot.synthesize_plan(
            reference_pos=(10.5, 10.5),
            bounds=(0, 64, 0, 52),
            base_origin=(28, 16),
            verify=False,
        )
        ctrl = BootstrapController(
            bot_agent=bot, id_mappings={"item": {}, "recipe": {}, "technology": {}}
        )
        self.assertEqual(ctrl.stage, "prime_latency")
        self.assertGreater(len(ctrl.phase1_target_task_indices), 10)

    def test_headless_factorio_cartographer_to_bot_starter_base_e2e(self) -> None:
        """Verify live Factorio Freeplay cartography scan, bot plan synthesis, and UDP action encoding."""
        from foundry.entity_helpers import (
            check_collisions,
            check_power,
            check_underground_pairing,
            get_pole_supply_radius,
        )

        try:
            factorio_bin = find_factorio_binary()
        except FileNotFoundError:
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

            map_gen = {"width": 160, "height": 160, "seed": 42}
            map_gen_file = tmp_path / "map-gen-settings.json"
            map_gen_file.write_text(json.dumps(map_gen, indent=2))

            base_save = tmp_path / "saves" / "scan_42.zip"
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

            scan_hook = (
                "\nlocal handler = require('event_handler')\n"
                "handler.add_lib({\n"
                "  on_nth_tick = {\n"
                "    [1] = function(e)\n"
                "      local p = nil\n"
                + scanner_lua
                + "      helpers.write_file('cartography_dump.json', helpers.table_to_json({cartography = storage.cartography}) .. '\\n', false)\n"
                "    end\n"
                "  }\n"
                "})\n"
            )
            self._append_lua_to_save(base_save, scan_hook)

            proc_scan = subprocess.run(
                [
                    str(factorio_bin),
                    "-c",
                    str(config_file),
                    "--mod-directory",
                    str(mods_dir),
                    "--benchmark",
                    str(base_save),
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

            dump_file = tmp_path / "script-output" / "cartography_dump.json"
            self.assertTrue(dump_file.is_file())

            bot = BotAgent(data=self.data, researched_techs={"automation", "logistics"})
            self.assertTrue(bot.ingest_cartography(dump_file))
            plan = bot.synthesize_plan(
                reference_pos=(0.0, 0.0),
                bounds=None,
                base_origin=None,
                verify=False,
            )
            self.assertEqual(check_collisions(plan.layout.entities), [])
            self.assertEqual(check_underground_pairing(plan.layout.entities), [])
            supply_rad = get_pole_supply_radius("small-electric-pole")
            self.assertEqual(check_power(plan.layout.entities, supply_rad), [])

            # Verify that output underground belts encode with reversed placement direction
            ug_output_tasks = [
                t
                for t in plan.tasks
                if t.task_kind == TASK_PLACE_ENTITY and t.entity_type == "output"
            ]
            self.assertGreater(len(ug_output_tasks), 0)
            id_mappings = {
                "item": {
                    name: idx + 1
                    for idx, name in enumerate(
                        sorted(set(plan.bill_of_materials.keys()) | {plan.fuel_commodity})
                    )
                },
                "recipe": {
                    name: idx + 1
                    for idx, name in enumerate(
                        sorted({t.recipe for t in plan.tasks if t.recipe is not None})
                    )
                },
            }
            for ug_t in ug_output_tasks:
                actions = encode_task_input_actions(ug_t, id_mappings)
                build_payload = actions[1][1]
                encoded_dir = build_payload[8]
                self.assertEqual(encoded_dir, (ug_t.direction + 8) % 16)

    def test_bot_synthesizes_and_builds_on_real_freeplay_map(self) -> None:
        """Verify Cartographer, automatic site selection, and BOM fabrication plan on the seed-42 Freeplay map."""
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

            bootstrap_kit = {
                "offshore-pump": 1,
                "boiler": 1,
                "steam-engine": 1,
                "small-electric-pole": 1,
                "assembling-machine-1": 1,
                "stone-furnace": 3,
                "lab": 1,
            }
            fab_plan = bot.compute_bom_fabrication(starting_inventory=bootstrap_kit)
            self.assertEqual(
                fab_plan.missing_bom["transport-belt"],
                plan.bill_of_materials["transport-belt"],
            )
            self.assertGreater(fab_plan.raw_materials_needed.get("iron-ore", 0), 500)
            self.assertGreater(fab_plan.raw_materials_needed.get("copper-ore", 0), 40)
            self.assertGreater(fab_plan.raw_materials_needed.get("stone", 0), 4)
            self.assertGreater(fab_plan.raw_materials_needed.get("wood", 0), 20)
            self.assertGreater(fab_plan.raw_materials_needed.get("coal", 0), 50)

            # Verify BootstrapController selects 3 planned iron-ore drills and valid temporary furnaces
            state_doc = json.loads(dump_file.read_text(encoding="utf-8"))
            ctrl = BootstrapController(
                bot_agent=bot,
                id_mappings={"item": {}, "recipe": {}, "technology": {}},
            )
            ctrl._select_iron_patch_drill_tasks()
            self.assertEqual(len(ctrl._iron_drill_tasks), 3)
            pole_tasks = [t for t in plan.tasks if t.phase == PHASE_POWER_NETWORK]
            self.assertLessEqual(len(pole_tasks), 60)
            bom_batch_map = {
                rec: crafts * batches for rec, _ings, crafts, batches in ctrl.bom_batches
            }
            self.assertGreaterEqual(bom_batch_map["splitter"], plan.bill_of_materials["splitter"])
            self.assertGreaterEqual(
                bom_batch_map["underground-belt"] * 2,
                plan.bill_of_materials["underground-belt"],
            )
            self.assertGreaterEqual(
                bom_batch_map["long-handed-inserter"],
                plan.bill_of_materials["long-handed-inserter"],
            )
            self.assertGreaterEqual(
                bom_batch_map["inserter"] - bom_batch_map["long-handed-inserter"],
                plan.bill_of_materials["inserter"],
            )
            self.assertGreaterEqual(
                bom_batch_map["electric-mining-drill"] + 3,
                plan.bill_of_materials["electric-mining-drill"],
            )
            self.assertGreaterEqual(
                bom_batch_map["assembling-machine-1"] + 1,
                plan.bill_of_materials["assembling-machine-1"],
            )
            del state_doc

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
        prefix: str = "starter_base_bot",
        target_frame_count: int = 1360,
    ) -> None:
        """Record live HUD, overview, and follow-cam MP4 videos of the bot starter base build."""
        import socket

        elf_bin = factorio_bin.resolve().parent.parent / "x64_" / "factorio"
        if not elf_bin.is_file():
            elf_bin = factorio_bin.resolve()

        srv_dir = tmp_path / f"vid_server_{prefix}"
        cli_dir = tmp_path / f"vid_client_{prefix}"
        srv_home = tmp_path / f"srv_home_{prefix}"
        cli_home = tmp_path / f"cli_home_{prefix}"
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

        live_mp4 = export_dir / f"{prefix}_live.mp4"
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

            target_frame = (
                cli_dir
                / "script-output"
                / "timelapse_overview"
                / f"frame_{target_frame_count:08d}.jpg"
            )
            wait_deadline = time.monotonic() + 240.0
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
                export_dir / f"{prefix}_overview.mp4",
                fps=20,
            )
            shutil.rmtree(overview_dir, ignore_errors=True)
        else:
            srv_txt = srv_proc.stdout.read() if srv_proc.stdout else ""
            cli_txt = cli_proc.stdout.read() if cli_proc and cli_proc.stdout else ""
            (export_dir / f"vid_debug_{prefix}.log").write_text(
                f"=== SRV ===\n{srv_txt}\n=== CLI ===\n{cli_txt}\n"
            )
        if follow_dir.is_dir() and list(follow_dir.glob("frame_*.jpg")):
            encode_timelapse_mp4(
                follow_dir,
                export_dir / f"{prefix}_follow.mp4",
                fps=20,
            )
            shutil.rmtree(follow_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
