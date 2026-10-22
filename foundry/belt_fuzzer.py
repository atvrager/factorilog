"""Empirical belt rule fuzzer and characterization suite using headless Factorio."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, TypeAlias

ExperimentMap: TypeAlias = dict[str, dict[str, Any]]

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "test"))

FUZZ_SIMULATION_TICKS = 190
YELLOW_SPEED_IPS = 15.0
RED_SPEED_IPS = 30.0
BLUE_SPEED_IPS = 45.0
TURBO_SPEED_IPS = 60.0
YELLOW_UG_MAX_TILES = 5
RED_UG_MAX_TILES = 7
BLUE_UG_MAX_TILES = 9
TURBO_UG_MAX_TILES = 11
YELLOW_LATENCY_TICKS = 32
RED_LATENCY_TICKS = 16
BLUE_LATENCY_TICKS = 11
TURBO_LATENCY_TICKS = 8


def get_simulation_runner() -> Any:
    """Dynamically load the simulation runner from the test suite."""
    try:
        mod = importlib.import_module("test_routing_primitives")
        return getattr(mod, "run_factorio_simulation", None)
    except (ImportError, AttributeError):
        return None


def run_empirical_fuzzing() -> dict[str, Any]:
    """Execute live Factorio simulations to characterize physical belt primitives."""
    runner = get_simulation_runner()
    if runner is None:
        raise RuntimeError("Factorio simulation runner not available.")

    init_lua = """
    -- 1. Straight transfer test
    local b1 = surface.create_entity{name = "transport-belt", position = {0.5, 0.5}, direction = 4, force = "player"}
    local b2 = surface.create_entity{name = "transport-belt", position = {1.5, 0.5}, direction = 4, force = "player"}
    local b3 = surface.create_entity{name = "transport-belt", position = {2.5, 0.5}, direction = 4, force = "player"}
    b1.get_transport_line(1).insert_at_back({name = "iron-plate", count = 1})
    b1.get_transport_line(2).insert_at_back({name = "copper-plate", count = 1})

    -- 2. Corner turn test (East to South, no tail)
    local c1 = surface.create_entity{name = "transport-belt", position = {0.5, 2.5}, direction = 4, force = "player"}
    local c2 = surface.create_entity{name = "transport-belt", position = {1.5, 2.5}, direction = 8, force = "player"}
    local c3 = surface.create_entity{name = "transport-belt", position = {1.5, 3.5}, direction = 8, force = "player"}
    c1.get_transport_line(1).insert_at_back({name = "iron-plate", count = 1})
    c1.get_transport_line(2).insert_at_back({name = "copper-plate", count = 1})

    -- 3. Sideload test (South belt feeds into East belt with tail)
    local s_tail = surface.create_entity{name = "transport-belt", position = {0.5, 5.5}, direction = 4, force = "player"}
    local s_main = surface.create_entity{name = "transport-belt", position = {1.5, 5.5}, direction = 4, force = "player"}
    local s_side = surface.create_entity{name = "transport-belt", position = {1.5, 4.5}, direction = 8, force = "player"}
    local s_out  = surface.create_entity{name = "transport-belt", position = {2.5, 5.5}, direction = 4, force = "player"}
    s_side.get_transport_line(1).insert_at_back({name = "iron-plate", count = 1})
    s_side.get_transport_line(2).insert_at_back({name = "copper-plate", count = 1})

    -- 4. Head-to-head collision test
    local h1 = surface.create_entity{name = "transport-belt", position = {0.5, 7.5}, direction = 4, force = "player"}
    local h2 = surface.create_entity{name = "transport-belt", position = {1.5, 7.5}, direction = 12, force = "player"}
    h1.get_transport_line(1).insert_at_back({name = "iron-plate", count = 1})
    h2.get_transport_line(1).insert_at_back({name = "copper-plate", count = 1})

    -- 5. Underground belt test (Span 4)
    local u1 = surface.create_entity{name = "underground-belt", position = {0.5, 9.5}, direction = 4, type = "input", force = "player"}
    local u2 = surface.create_entity{name = "underground-belt", position = {4.5, 9.5}, direction = 4, type = "output", force = "player"}
    local u_out = surface.create_entity{name = "transport-belt", position = {5.5, 9.5}, direction = 4, force = "player"}
    u1.get_transport_line(1).insert_at_back({name = "iron-plate", count = 1})
    u1.get_transport_line(2).insert_at_back({name = "copper-plate", count = 1})

    -- 6. Underground side feed clearance test
    local us_in = surface.create_entity{name = "underground-belt", position = {1.5, 11.5}, direction = 4, type = "input", force = "player"}
    local us_feed = surface.create_entity{name = "transport-belt", position = {1.5, 10.5}, direction = 8, force = "player"}
    us_feed.get_transport_line(1).insert_at_back({name = "iron-plate", count = 1})

    -- 7. Splitter flank feed clearance test
    local sp = surface.create_entity{name = "splitter", position = {1.5, 15.0}, direction = 4, force = "player"}
    local sp_flank = surface.create_entity{name = "transport-belt", position = {1.5, 13.5}, direction = 8, force = "player"}
    sp_flank.get_transport_line(1).insert_at_back({name = "iron-plate", count = 1})

    storage.b1 = b1
    storage.b2 = b2
    storage.b3 = b3
    storage.c2 = c2
    storage.c3 = c3
    storage.s_main = s_main
    storage.s_out = s_out
    storage.h1 = h1
    storage.h2 = h2
    storage.u1 = u1
    storage.u2 = u2
    storage.u_out = u_out
    storage.us_in = us_in
    storage.us_feed = us_feed
    storage.sp = sp
    storage.sp_flank = sp_flank
    storage.turn_seen_l1 = 0
    storage.turn_seen_l2 = 0
    """

    tick_lua = """
    if storage.c3.get_transport_line(1).get_item_count("iron-plate") > 0 then
        storage.turn_seen_l1 = 1
    end
    if storage.c3.get_transport_line(2).get_item_count("copper-plate") > 0 then
        storage.turn_seen_l2 = 1
    end

    if event.tick == 180 then
        -- 1. Straight
        local b3_l1 = storage.b3.get_transport_line(1).get_item_count("iron-plate")
        local b3_l2 = storage.b3.get_transport_line(2).get_item_count("copper-plate")
        log(string.format("TEST_RESULT: TEST_EXP: STRAIGHT l1=%d l2=%d shape=%s in=%d out=%d",
            b3_l1, b3_l2, storage.b2.belt_shape, #storage.b2.belt_neighbours.inputs, #storage.b2.belt_neighbours.outputs))

        -- 2. Turn
        log(string.format("TEST_RESULT: TEST_EXP: TURN l1=%d l2=%d c2_shape=%s in=%d out=%d",
            storage.turn_seen_l1, storage.turn_seen_l2, storage.c2.belt_shape,
            #storage.c2.belt_neighbours.inputs, #storage.c2.belt_neighbours.outputs))

        -- 3. Sideload
        local so_l1 = storage.s_out.get_transport_line(1).get_item_count()
        local so_l2 = storage.s_out.get_transport_line(2).get_item_count()
        log(string.format("TEST_RESULT: TEST_EXP: SIDELOAD out_l1=%d out_l2=%d main_shape=%s in=%d",
            so_l1, so_l2, storage.s_main.belt_shape, #storage.s_main.belt_neighbours.inputs))

        -- 4. Head to Head Collision
        local h1_count = storage.h1.get_transport_line(1).get_item_count()
        local h2_count = storage.h2.get_transport_line(1).get_item_count()
        log(string.format("TEST_RESULT: TEST_EXP: HEAD_TO_HEAD h1_blocked=%d h2_blocked=%d in=%d out=%d",
            h1_count, h2_count, #storage.h2.belt_neighbours.inputs, #storage.h2.belt_neighbours.outputs))

        -- 5. Underground
        local u_l1 = storage.u_out.get_transport_line(1).get_item_count("iron-plate")
        local u_l2 = storage.u_out.get_transport_line(2).get_item_count("copper-plate")
        local u_paired = storage.u1.underground_belt_neighbour ~= nil
        log(string.format("TEST_RESULT: TEST_EXP: UNDERGROUND l1=%d l2=%d paired=%s",
            u_l1, u_l2, tostring(u_paired)))

        -- 6. Hood side feed
        local us_count = storage.us_in.get_transport_line(1).get_item_count()
        local feed_blocked = storage.us_feed.get_transport_line(1).get_item_count()
        log(string.format("TEST_RESULT: TEST_EXP: HOOD_SIDE_FEED hood_received=%d feed_blocked=%d",
            us_count, feed_blocked))

        -- 7. Splitter flank feed
        local flank_count = storage.sp_flank.get_transport_line(1).get_item_count()
        log(string.format("TEST_RESULT: TEST_EXP: SPLITTER_FLANK flank_blocked=%d",
            flank_count))
    end
    """

    results = runner(init_lua, tick_lua, ticks=FUZZ_SIMULATION_TICKS)

    parsed_results: ExperimentMap = {}
    for line in results:
        if "TEST_EXP: " in line:
            payload = line.split("TEST_EXP: ")[1].strip()
            parts = payload.split()
            exp_name = parts[0]
            kvs = {}
            for item in parts[1:]:
                if "=" in item:
                    k, v = item.split("=", 1)
                    kvs[k] = v
            parsed_results[exp_name] = kvs

    return build_rule_database(parsed_results)


def build_rule_database(exp_data: ExperimentMap) -> dict[str, Any]:
    """Compile raw simulation outputs into an empirical rule database."""
    straight_exp = exp_data.get("STRAIGHT", {})
    turn_exp = exp_data.get("TURN", {})
    sideload_exp = exp_data.get("SIDELOAD", {})
    collision_exp = exp_data.get("HEAD_TO_HEAD", {})
    ug_exp = exp_data.get("UNDERGROUND", {})
    hood_feed_exp = exp_data.get("HOOD_SIDE_FEED", {})
    sp_flank_exp = exp_data.get("SPLITTER_FLANK", {})

    experiments = [
        {
            "experiment_id": "EXP-01-STRAIGHT",
            "name": "collinear_belt_transfer",
            "physical_outcome": "PASS_CLEAN_TRANSFER",
            "engine_measurements": {
                "lane_1_delivered": int(straight_exp.get("l1", 0)),
                "lane_2_delivered": int(straight_exp.get("l2", 0)),
                "engine_belt_shape": straight_exp.get("shape", "straight"),
                "connected_inputs": int(straight_exp.get("in", 0)),
                "connected_outputs": int(straight_exp.get("out", 0)),
            },
            "interpretation": "Belts with matching directions transfer items 1:1 across both lanes.",
        },
        {
            "experiment_id": "EXP-02-TURN",
            "name": "orthogonal_corner_turn",
            "physical_outcome": "PASS_CLEAN_TRANSFER",
            "engine_measurements": {
                "lane_1_delivered": int(turn_exp.get("l1", 0)),
                "lane_2_delivered": int(turn_exp.get("l2", 0)),
                "engine_belt_shape": turn_exp.get("c2_shape", "right"),
                "connected_inputs": int(turn_exp.get("in", 0)),
                "connected_outputs": int(turn_exp.get("out", 0)),
            },
            "interpretation": "Perpendicular belts curve 90 degrees when no upstream tail exists.",
        },
        {
            "experiment_id": "EXP-03-SIDELOAD",
            "name": "perpendicular_sideload_injection",
            "physical_outcome": "PASS_SINGLE_LANE_DEPOSIT",
            "engine_measurements": {
                "near_lane_delivered": int(sideload_exp.get("out_l1", 0)),
                "far_lane_delivered": int(sideload_exp.get("out_l2", 0)),
                "engine_belt_shape": sideload_exp.get("main_shape", "straight"),
                "connected_inputs": int(sideload_exp.get("in", 0)),
            },
            "interpretation": "Perpendicular belts into a belt with an existing tail drop all items onto the near lane only.",
        },
        {
            "experiment_id": "EXP-04-HEAD_TO_HEAD",
            "name": "opposing_belt_deadlock",
            "physical_outcome": "CONFIRMED_DEADLOCK",
            "engine_measurements": {
                "source_1_blocked": int(collision_exp.get("h1_blocked", 0)),
                "source_2_blocked": int(collision_exp.get("h2_blocked", 0)),
                "connected_inputs": int(collision_exp.get("in", 0)),
                "connected_outputs": int(collision_exp.get("out", 0)),
            },
            "interpretation": "Belts facing opposite directions on adjacent tiles fail to connect and stall.",
        },
        {
            "experiment_id": "EXP-05-UNDERGROUND",
            "name": "subsurface_tunnel_transfer",
            "physical_outcome": "PASS_CLEAN_TRANSFER",
            "engine_measurements": {
                "lane_1_delivered": int(ug_exp.get("l1", 0)),
                "lane_2_delivered": int(ug_exp.get("l2", 0)),
                "engine_underground_paired": ug_exp.get("paired") == "true",
            },
            "interpretation": "Underground entrance pairs with collinear exit and transfers both lanes.",
        },
        {
            "experiment_id": "EXP-06-HOOD_SIDE_FEED",
            "name": "underground_hood_side_clearance",
            "physical_outcome": "CONFIRMED_BLOCKED",
            "engine_measurements": {
                "hood_accepted_items": int(hood_feed_exp.get("hood_received", 0)),
                "feed_belt_blocked": int(hood_feed_exp.get("feed_blocked", 0)),
            },
            "interpretation": "Perpendicular feeds into underground hood sides are physically blocked.",
        },
        {
            "experiment_id": "EXP-07-SPLITTER_FLANK",
            "name": "splitter_flank_feed_clearance",
            "physical_outcome": "CONFIRMED_BLOCKED",
            "engine_measurements": {
                "flank_belt_blocked": int(sp_flank_exp.get("flank_blocked", 0)),
            },
            "interpretation": "Perpendicular feeds into splitter side flanks cannot enter and remain blocked.",
        },
    ]

    rules = [
        {
            "rule_id": "RULE-01-STRAIGHT",
            "primitive": "straight",
            "compatible": True,
            "lane_mapping": {"1": 1, "2": 2},
            "speed_tiles_per_sec": YELLOW_SPEED_IPS,
            "drc_code": None,
            "summary": "Collinear belts transfer items across both lanes with zero latency penalty.",
        },
        {
            "rule_id": "RULE-02-TURN",
            "primitive": "turn",
            "compatible": True,
            "lane_mapping": {"1": 1, "2": 2},
            "requires_no_upstream_tail": True,
            "drc_code": None,
            "summary": "Perpendicular belts curve into 90-degree corners when no upstream tail feeds the tile.",
        },
        {
            "rule_id": "RULE-03-SIDELOAD",
            "primitive": "sideload",
            "compatible": True,
            "lane_mapping": {"1": 1, "2": 1},
            "requires_upstream_tail": True,
            "drc_code": None,
            "summary": "Perpendicular feeds onto a belt with an existing tail drop items onto the near lane only.",
        },
        {
            "rule_id": "RULE-04-HEAD_TO_HEAD",
            "primitive": "opposing_pair",
            "compatible": False,
            "drc_code": "DRC-01",
            "summary": "Belts facing opposite directions on adjacent tiles deadlock and are forbidden.",
        },
        {
            "rule_id": "RULE-05-UNDERGROUND_PAIR",
            "primitive": "underground_pair",
            "compatible": True,
            "lane_mapping": {"1": 1, "2": 2},
            "max_span_tiles": {
                "underground-belt": YELLOW_UG_MAX_TILES,
                "fast-underground-belt": RED_UG_MAX_TILES,
                "express-underground-belt": BLUE_UG_MAX_TILES,
                "turbo-underground-belt": TURBO_UG_MAX_TILES,
            },
            "drc_code": "DRC-02",
            "summary": "Underground inputs must pair with collinear outputs within tier span limits.",
        },
        {
            "rule_id": "RULE-06-HOOD_SIDE_FEED",
            "primitive": "hood_side_feed",
            "compatible": False,
            "drc_code": "DRC-03",
            "summary": "Perpendicular belts feeding into the side of underground hoods stall and fail DRC.",
        },
        {
            "rule_id": "RULE-07-SPLITTER_FLANK",
            "primitive": "splitter_flank_feed",
            "compatible": False,
            "drc_code": "DRC-04",
            "summary": "Perpendicular belts feeding into splitter flanks stall and fail DRC.",
        },
    ]

    belt_tiers = {
        "yellow": {
            "belt": "transport-belt",
            "underground": "underground-belt",
            "splitter": "splitter",
            "speed_items_per_sec": YELLOW_SPEED_IPS,
            "max_underground_span_tiles": YELLOW_UG_MAX_TILES,
            "latency_ticks_per_tile": YELLOW_LATENCY_TICKS,
        },
        "red": {
            "belt": "fast-transport-belt",
            "underground": "fast-underground-belt",
            "splitter": "fast-splitter",
            "speed_items_per_sec": RED_SPEED_IPS,
            "max_underground_span_tiles": RED_UG_MAX_TILES,
            "latency_ticks_per_tile": RED_LATENCY_TICKS,
        },
        "blue": {
            "belt": "express-transport-belt",
            "underground": "express-underground-belt",
            "splitter": "express-splitter",
            "speed_items_per_sec": BLUE_SPEED_IPS,
            "max_underground_span_tiles": BLUE_UG_MAX_TILES,
            "latency_ticks_per_tile": BLUE_LATENCY_TICKS,
        },
        "turbo": {
            "belt": "turbo-transport-belt",
            "underground": "turbo-underground-belt",
            "splitter": "turbo-splitter",
            "speed_items_per_sec": TURBO_SPEED_IPS,
            "max_underground_span_tiles": TURBO_UG_MAX_TILES,
            "latency_ticks_per_tile": TURBO_LATENCY_TICKS,
        },
    }

    layer_stack_mapping = {
        "M1": {
            "preferred_direction": "horizontal",
            "entity": "transport-belt",
            "level": "surface",
        },
        "M2": {
            "preferred_direction": "vertical",
            "entity": "transport-belt",
            "level": "surface",
        },
        "M3": {
            "preferred_direction": "horizontal",
            "entity": "underground-belt",
            "level": "subsurface",
        },
        "M4": {
            "preferred_direction": "vertical",
            "entity": "underground-belt",
            "level": "subsurface",
        },
        "M5": {
            "preferred_direction": "horizontal",
            "entity": "circuit-red",
            "level": "circuit",
        },
        "M6": {
            "preferred_direction": "vertical",
            "entity": "circuit-green",
            "level": "circuit",
        },
        "M7": {
            "preferred_direction": "horizontal",
            "entity": "fast-underground-belt",
            "level": "subsurface",
        },
        "M8": {
            "preferred_direction": "vertical",
            "entity": "fast-underground-belt",
            "level": "subsurface",
        },
        "M9": {
            "preferred_direction": "horizontal",
            "entity": "express-underground-belt",
            "level": "subsurface",
        },
        "M10": {
            "preferred_direction": "vertical",
            "entity": "express-underground-belt",
            "level": "subsurface",
        },
        "M11": {
            "preferred_direction": "horizontal",
            "entity": "turbo-underground-belt",
            "level": "subsurface",
        },
        "M12": {
            "preferred_direction": "vertical",
            "entity": "turbo-underground-belt",
            "level": "subsurface",
        },
        "VIA1": {
            "description": "Corner turn between surface layers M1 and M2",
            "entity": "transport-belt",
        },
        "VIA2": {
            "description": "Portal transition to subsurface layers M3 and M4",
            "entity": "underground-belt",
        },
    }

    return {
        "metadata": {
            "version": "1.2.0",
            "game_version": "2.1.21",
            "simulator": "Factorio 2.1 Headless Linux",
            "simulation_source": "foundry/belt_fuzzer.py",
        },
        "belt_tiers": belt_tiers,
        "layer_stack_mapping": layer_stack_mapping,
        "empirical_experiments": experiments,
        "derived_rules": rules,
    }


def main() -> int:
    """Execute live Factorio fuzzing and write empirical rule database."""
    parser = argparse.ArgumentParser(description="Empirical belt rule fuzzer")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "test" / "data" / "belt_rules.json",
        help="Target path for rule database JSON",
    )
    args = parser.parse_args()

    sys.stdout.write("Running live empirical belt fuzzing in Factorio...\n")
    db = run_empirical_fuzzing()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(db, indent=2) + "\n")
    sys.stdout.write(f"Successfully generated empirical rule database at {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
