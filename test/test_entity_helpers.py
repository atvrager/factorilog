"""Unit tests for entity geometry, power checks, collisions, and belt pruning."""

from __future__ import annotations

import unittest

from foundry import entity_helpers as eh


class TestEntityHelpers(unittest.TestCase):
    """Test suite for entity helper functions."""

    def test_entity_helpers_directions_and_consumers(self) -> None:
        self.assertEqual(eh._segment_direction(5, 1, 5, 4), eh.DIRECTION_SOUTH)
        self.assertEqual(eh._segment_direction(5, 4, 5, 1), eh.DIRECTION_NORTH)
        self.assertEqual(eh._segment_direction(1, 5, 4, 5), eh.DIRECTION_EAST)
        self.assertEqual(eh._segment_direction(4, 5, 1, 5), eh.DIRECTION_WEST)

        self.assertFalse(eh.is_electric_consumer(""))
        self.assertFalse(eh.is_electric_consumer("burner-inserter"))
        self.assertFalse(eh.is_electric_consumer("constant-combinator"))
        self.assertFalse(eh.is_electric_consumer("display-panel"))
        self.assertFalse(eh.is_electric_consumer("transport-belt"))
        self.assertTrue(eh.is_electric_consumer("fast-inserter"))
        self.assertTrue(eh.is_electric_consumer("assembling-machine-3"))
        self.assertTrue(eh.is_electric_pole("medium-electric-pole"))
        self.assertTrue(eh.is_electric_pole("substation"))
        self.assertFalse(eh.is_electric_pole("transport-belt"))

        self.assertEqual(eh.get_pole_wire_reach("medium-electric-pole"), 9.0)
        self.assertEqual(eh.get_pole_wire_reach("unknown-pole", default=12.5), 12.5)
        self.assertEqual(
            eh.get_compatible_underground_belt(4, "underground-belt"), "underground-belt"
        )
        self.assertEqual(
            eh.get_compatible_underground_belt(6, "underground-belt"), "fast-underground-belt"
        )
        self.assertEqual(eh.get_compatible_underground_belt(8), "express-underground-belt")
        self.assertEqual(eh.get_compatible_underground_belt(99, "custom-ug"), "custom-ug")

    def test_entity_helpers_prototypes_and_sizes(self) -> None:
        protos = {
            "custom-ug-belt": {
                "collision_box": [[-0.4, -0.4], [0.4, 0.4]],
                "max_distance": 11,
            },
            "custom-pipe-ground": {
                "fluid_box": {
                    "pipe_connections": [
                        {"connection_type": "normal", "max_underground_distance": 99},
                        {"connection_type": "underground", "max_underground_distance": 14},
                    ]
                }
            },
            "custom-pole": {
                "collision_box": [[-0.65, -0.65], [0.65, 0.65]],
                "supply_area_distance": 4.5,
                "maximum_wire_distance": 11.5,
            },
            "nested-table": {
                "nested-machine": {
                    "collision_box": [[-1.4, -2.4], [1.4, 2.4]],
                    "max_distance": 6,
                    "supply_area_distance": 5.5,
                    "maximum_wire_distance": 13.5,
                }
            },
        }
        eh.register_prototype_boxes(protos)
        self.assertEqual(eh.get_underground_span("custom-ug-belt"), 11)
        self.assertEqual(eh.get_underground_span("custom-pipe-ground"), 14)
        self.assertEqual(eh.get_pole_supply_radius("custom-pole"), 4.5)
        self.assertEqual(eh.get_pole_wire_reach("custom-pole"), 11.5)
        self.assertEqual(eh.entity_size("custom-pole"), (2.0, 2.0))
        self.assertEqual(eh.entity_collision_half_size("custom-pole"), (0.65, 0.65))
        self.assertEqual(eh.entity_size("nested-machine", eh.DIRECTION_NORTH), (3.0, 5.0))
        self.assertEqual(eh.entity_size("nested-machine", eh.DIRECTION_EAST), (5.0, 3.0))
        self.assertEqual(
            eh.entity_collision_half_size("nested-machine", eh.DIRECTION_NORTH), (1.4, 2.4)
        )
        self.assertEqual(
            eh.entity_collision_half_size("nested-machine", eh.DIRECTION_WEST), (2.4, 1.4)
        )

        # Fallback entity_size and entity_collision_half_size checks
        self.assertEqual(eh.entity_size("pump", eh.DIRECTION_NORTH), (1.0, 2.0))
        self.assertEqual(eh.entity_size("pump", eh.DIRECTION_EAST), (2.0, 1.0))
        self.assertEqual(eh.entity_size("custom-pump", eh.DIRECTION_NORTH), (1.0, 2.0))
        self.assertEqual(eh.entity_size("custom-pump", eh.DIRECTION_WEST), (2.0, 1.0))
        self.assertEqual(eh.entity_size("rocket-silo"), (9.0, 9.0))
        self.assertEqual(eh.entity_size("oil-refinery"), (5.0, 5.0))
        self.assertEqual(eh.entity_size("steam-engine", eh.DIRECTION_NORTH), (3.0, 5.0))
        self.assertEqual(eh.entity_size("steam-engine", eh.DIRECTION_EAST), (5.0, 3.0))
        self.assertEqual(eh.entity_size("boiler", eh.DIRECTION_NORTH), (3.0, 2.0))
        self.assertEqual(eh.entity_size("boiler", eh.DIRECTION_EAST), (2.0, 3.0))
        self.assertEqual(eh.entity_size("assembling-machine-3"), (3.0, 3.0))
        self.assertEqual(eh.entity_size("steel-furnace"), (2.0, 2.0))
        self.assertEqual(eh.entity_size("transport-belt"), (1.0, 1.0))

        self.assertEqual(eh.entity_collision_half_size("pump", eh.DIRECTION_NORTH), (0.29, 0.9))
        self.assertEqual(eh.entity_collision_half_size("pump", eh.DIRECTION_EAST), (0.9, 0.29))
        self.assertEqual(
            eh.entity_collision_half_size("custom-pump", eh.DIRECTION_NORTH), (0.29, 0.9)
        )
        self.assertEqual(
            eh.entity_collision_half_size("custom-pump", eh.DIRECTION_EAST), (0.9, 0.29)
        )
        self.assertEqual(eh.entity_collision_half_size("rocket-silo"), (4.2, 4.2))
        self.assertEqual(eh.entity_collision_half_size("oil-refinery"), (2.2, 2.2))
        self.assertEqual(
            eh.entity_collision_half_size("steam-engine", eh.DIRECTION_NORTH), (1.25, 2.35)
        )
        self.assertEqual(
            eh.entity_collision_half_size("steam-engine", eh.DIRECTION_EAST), (2.35, 1.25)
        )
        self.assertEqual(eh.entity_collision_half_size("boiler", eh.DIRECTION_NORTH), (1.29, 0.79))
        self.assertEqual(eh.entity_collision_half_size("boiler", eh.DIRECTION_EAST), (0.79, 1.29))
        self.assertEqual(eh.entity_collision_half_size("assembling-machine-3"), (1.2, 1.2))
        self.assertEqual(eh.entity_collision_half_size("steel-furnace"), (0.7, 0.7))
        self.assertEqual(eh.entity_collision_half_size("transport-belt"), (0.15, 0.15))

    def test_entity_helpers_power_collisions_and_underground(self) -> None:
        # check_power across bucket boundaries (bucket size 16)
        consumer_near = {
            "name": "assembling-machine-3",
            "position": {"x": 12.0, "y": 12.0},
            "direction": eh.DIRECTION_NORTH,
        }
        consumer_far = {
            "name": "assembling-machine-3",
            "position": {"x": 50.0, "y": 50.0},
            "direction": eh.DIRECTION_NORTH,
        }
        # Place pole at (16.5, 16.5) in the next bucket so box edge distance is 3.3 <= 3.5.
        pole = {
            "name": "medium-electric-pole",
            "position": {"x": 16.5, "y": 16.5},
        }
        self.assertEqual(
            eh.check_power([consumer_near, consumer_far]), [consumer_near, consumer_far]
        )
        self.assertEqual(eh.check_power([pole, consumer_near, consumer_far]), [consumer_far])

        # check_collisions for splitters and pumps in N/S and E/W orientations
        ents = [
            {"name": "splitter", "position": {"x": 5.0, "y": 5.5}, "direction": eh.DIRECTION_NORTH},
            {"name": "transport-belt", "position": {"x": 4.5, "y": 5.5}},
            {"name": "transport-belt", "position": {"x": 5.5, "y": 5.5}},
            {
                "name": "splitter",
                "position": {"x": 10.5, "y": 10.0},
                "direction": eh.DIRECTION_EAST,
            },
            {"name": "transport-belt", "position": {"x": 10.5, "y": 9.5}},
            {"name": "transport-belt", "position": {"x": 10.5, "y": 10.5}},
            {"name": "pump", "position": {"x": 20.5, "y": 20.0}, "direction": eh.DIRECTION_NORTH},
            {"name": "pipe", "position": {"x": 20.5, "y": 19.5}},
            {"name": "pipe", "position": {"x": 20.5, "y": 20.5}},
            {"name": "pump", "position": {"x": 30.0, "y": 30.5}, "direction": eh.DIRECTION_EAST},
            {"name": "pipe", "position": {"x": 29.5, "y": 30.5}},
            {"name": "pipe", "position": {"x": 30.5, "y": 30.5}},
        ]
        cols = eh.check_collisions(ents)
        self.assertEqual(len(cols), 8)
        self.assertEqual(
            eh.check_collisions(
                [
                    {
                        "name": "splitter",
                        "position": {"x": 5.0, "y": 5.5},
                        "direction": eh.DIRECTION_NORTH,
                    }
                ]
            ),
            [],
        )
        self.assertEqual(
            eh.check_collisions(
                [
                    {
                        "name": "pump",
                        "position": {"x": 20.5, "y": 20.0},
                        "direction": eh.DIRECTION_NORTH,
                    }
                ]
            ),
            [],
        )

        # check_underground_pairing for all 4 directions (E, W, S, N) and pipe-to-ground
        ug_ents = [
            # E pair
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_EAST,
                "position": {"x": 1.5, "y": 1.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_EAST,
                "position": {"x": 4.5, "y": 1.5},
            },
            # W pair
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_WEST,
                "position": {"x": 10.5, "y": 2.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_WEST,
                "position": {"x": 7.5, "y": 2.5},
            },
            # S pair
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_SOUTH,
                "position": {"x": 3.5, "y": 1.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_SOUTH,
                "position": {"x": 3.5, "y": 4.5},
            },
            # N pair
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_NORTH,
                "position": {"x": 5.5, "y": 10.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_NORTH,
                "position": {"x": 5.5, "y": 7.5},
            },
            # Pipe-to-ground pair at max span 10
            {
                "name": "pipe-to-ground",
                "direction": eh.DIRECTION_WEST,
                "position": {"x": 20.5, "y": 1.5},
            },
            {
                "name": "pipe-to-ground",
                "direction": eh.DIRECTION_EAST,
                "position": {"x": 30.5, "y": 1.5},
            },
        ]
        self.assertEqual(eh.check_underground_pairing(ug_ents), [])
        # Break W pair by moving output to wrong direction or span
        bad_w = [
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_WEST,
                "position": {"x": 10.5, "y": 2.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_EAST,
                "position": {"x": 7.5, "y": 2.5},
            },
        ]
        self.assertEqual(len(eh.check_underground_pairing(bad_w)), 2)

    def test_prune_dead_end_belts_all_sinks(self) -> None:
        # Test output pins, splitters in E/W/N/S, underground input, inserter pickup, and opposing belt head-on
        entities = [
            # Belt on output pin (0.5, 2.5) -> kept
            {
                "name": "transport-belt",
                "position": {"x": 0.5, "y": 2.5},
                "direction": eh.DIRECTION_EAST,
            },
            # Splitters in E, W, N, S and belts feeding both halves of each splitter -> kept
            {"name": "splitter", "position": {"x": 10.5, "y": 5.0}, "direction": eh.DIRECTION_EAST},
            {
                "name": "transport-belt",
                "position": {"x": 9.5, "y": 4.5},
                "direction": eh.DIRECTION_EAST,
            },
            {
                "name": "transport-belt",
                "position": {"x": 9.5, "y": 5.5},
                "direction": eh.DIRECTION_EAST,
            },
            {"name": "splitter", "position": {"x": 20.5, "y": 5.0}, "direction": eh.DIRECTION_WEST},
            {
                "name": "transport-belt",
                "position": {"x": 21.5, "y": 4.5},
                "direction": eh.DIRECTION_WEST,
            },
            {
                "name": "transport-belt",
                "position": {"x": 21.5, "y": 5.5},
                "direction": eh.DIRECTION_WEST,
            },
            {
                "name": "splitter",
                "position": {"x": 30.0, "y": 5.5},
                "direction": eh.DIRECTION_NORTH,
            },
            {
                "name": "transport-belt",
                "position": {"x": 29.5, "y": 6.5},
                "direction": eh.DIRECTION_NORTH,
            },
            {
                "name": "transport-belt",
                "position": {"x": 30.5, "y": 6.5},
                "direction": eh.DIRECTION_NORTH,
            },
            {
                "name": "splitter",
                "position": {"x": 40.0, "y": 5.5},
                "direction": eh.DIRECTION_SOUTH,
            },
            {
                "name": "transport-belt",
                "position": {"x": 39.5, "y": 4.5},
                "direction": eh.DIRECTION_SOUTH,
            },
            {
                "name": "transport-belt",
                "position": {"x": 40.5, "y": 4.5},
                "direction": eh.DIRECTION_SOUTH,
            },
            # Inserter at (50.5, 5.5) facing EAST -> pickup at (51.5, 5.5) -> belt at (51.5, 5.5) kept
            {"name": "inserter", "position": {"x": 50.5, "y": 5.5}, "direction": eh.DIRECTION_EAST},
            {
                "name": "transport-belt",
                "position": {"x": 51.5, "y": 5.5},
                "direction": eh.DIRECTION_NORTH,
            },
            # Belt feeding into belt at (51.5, 5.5) with same/valid direction vs opposing direction
            {
                "name": "transport-belt",
                "position": {"x": 51.5, "y": 6.5},
                "direction": eh.DIRECTION_NORTH,
            },
            # Put belt at (60.5, 5.5) facing EAST into (61.5, 5.5) facing WEST so prune_dead_end_belts removes both.
            {
                "name": "transport-belt",
                "position": {"x": 60.5, "y": 5.5},
                "direction": eh.DIRECTION_EAST,
            },
            {
                "name": "transport-belt",
                "position": {"x": 61.5, "y": 5.5},
                "direction": eh.DIRECTION_WEST,
            },
            # Belts at macro input (69.5, 1.5) and output (73.5, 1.5) for component at (70, 0) with w=3, h=3
            {
                "name": "transport-belt",
                "position": {"x": 69.5, "y": 1.5},
                "direction": eh.DIRECTION_EAST,
            },
            {
                "name": "transport-belt",
                "position": {"x": 73.5, "y": 1.5},
                "direction": eh.DIRECTION_EAST,
            },
        ]
        def_data = {
            "units": 1000,
            "pins": [{"direction": "OUTPUT", "x": 500, "y": 2500}],
            "components": [{"name": "c1", "macro": "custom_mac", "x": 70000, "y": 0}],
        }
        netlist_data = {
            "cells": {
                "custom_mac": {
                    "width": 3,
                    "height": 3,
                    "inputs": ["in1"],
                    "outputs": ["out1"],
                    "in_slots": {},
                    "out_slots": {},
                }
            }
        }
        pruned = eh.prune_dead_end_belts(entities, def_data, netlist_data)
        pruned_belt_positions = {
            (round(e["position"]["x"], 1), round(e["position"]["y"], 1))
            for e in pruned
            if e["name"] == "transport-belt"
        }
        self.assertIn((0.5, 2.5), pruned_belt_positions)
        self.assertIn((9.5, 4.5), pruned_belt_positions)
        self.assertIn((9.5, 5.5), pruned_belt_positions)
        self.assertIn((21.5, 4.5), pruned_belt_positions)
        self.assertIn((21.5, 5.5), pruned_belt_positions)
        self.assertIn((29.5, 6.5), pruned_belt_positions)
        self.assertIn((30.5, 6.5), pruned_belt_positions)
        self.assertIn((39.5, 4.5), pruned_belt_positions)
        self.assertIn((40.5, 4.5), pruned_belt_positions)
        self.assertIn((51.5, 5.5), pruned_belt_positions)
        self.assertIn((51.5, 6.5), pruned_belt_positions)
        self.assertIn((69.5, 1.5), pruned_belt_positions)
        self.assertIn((73.5, 1.5), pruned_belt_positions)
        self.assertNotIn((60.5, 5.5), pruned_belt_positions)
        self.assertNotIn((61.5, 5.5), pruned_belt_positions)


if __name__ == "__main__":
    unittest.main()
