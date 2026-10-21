"""Unit tests for street channel routing and router intersections."""

from __future__ import annotations

import unittest

from foundry.channel_router import ChannelLane, RouterIntersection, StreetChannel
from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    check_collisions,
    check_underground_pairing,
)


class TestChannelRouter(unittest.TestCase):
    """Test channel lane allocation, straight routing, and router intersection."""

    def test_channel_lane_validation(self) -> None:
        """Verify ChannelLane bounds checking and properties."""
        lane = ChannelLane(0, "H", DIRECTION_EAST)
        self.assertEqual(lane.lane_index, 0)
        self.assertEqual(lane.orientation, "H")
        self.assertEqual(lane.direction, DIRECTION_EAST)
        self.assertFalse(lane.is_allocated)
        self.assertFalse(lane.is_fluid)

        # Invalid lane index
        with self.assertRaises(ValueError):
            ChannelLane(-1, "H", DIRECTION_EAST)
        with self.assertRaises(ValueError):
            ChannelLane(6, "H", DIRECTION_EAST)

        # Invalid orientation
        with self.assertRaises(ValueError):
            ChannelLane(2, "X", DIRECTION_EAST)

        # Fluid lane property
        fluid_lane = ChannelLane(1, "V", DIRECTION_SOUTH, tier="pipe-to-ground")
        self.assertTrue(fluid_lane.is_fluid)

    def test_street_channel_allocation(self) -> None:
        """Verify allocation and deallocation across 6-tile street corridor."""
        ch = StreetChannel("h_street_0", "H", base_coord=97.0, span_min=3.0, span_max=97.0)
        self.assertEqual(len(ch.lanes), 6)
        self.assertEqual(len(ch.allocated_lanes), 0)
        self.assertEqual(len(ch.unallocated_lanes), 6)

        # Lane coordinates across 6 tiles (base 97.0)
        for i in range(6):
            self.assertEqual(ch.get_lane_coord(i), 97.0 + i + 0.5)

        # Allocate forward lane (East)
        l0 = ch.allocate_lane("iron_bus", DIRECTION_EAST, item_type="iron-plate")
        self.assertEqual(l0.lane_index, 0)
        self.assertEqual(l0.net_name, "iron_bus")
        self.assertEqual(l0.direction, DIRECTION_EAST)
        self.assertTrue(l0.is_allocated)

        # Allocate reverse lane (West) -> defaults to lanes 3-5
        l3 = ch.allocate_lane("circuit_bus", DIRECTION_WEST, item_type="electronic-circuit")
        self.assertEqual(l3.lane_index, 3)
        self.assertEqual(l3.direction, DIRECTION_WEST)

        # Allocate preferred lane
        l5 = ch.allocate_lane("copper_bus", DIRECTION_WEST, preferred_lane=5)
        self.assertEqual(l5.lane_index, 5)

        # Deallocate lane
        ch.free_lane(0)
        self.assertFalse(ch.get_lane(0).is_allocated)

        # Reallocate lane 0
        l0_new = ch.allocate_lane("steel_bus", DIRECTION_EAST)
        self.assertEqual(l0_new.lane_index, 0)
        self.assertEqual(l0_new.net_name, "steel_bus")

        # Fill remaining lanes
        ch.allocate_lane("net_1", DIRECTION_EAST)  # lane 1
        ch.allocate_lane("net_2", DIRECTION_EAST)  # lane 2
        ch.allocate_lane("net_4", DIRECTION_WEST)  # lane 4
        self.assertEqual(len(ch.allocated_lanes), 6)

        # Overflow error when all six lanes have nets
        with self.assertRaises(RuntimeError):
            ch.allocate_lane("overflow_net", DIRECTION_EAST)

    def test_emit_straight_belts(self) -> None:
        """Verify emission of continuous surface belts along street channel."""
        ch = StreetChannel("v_street_0", "V", base_coord=97.0, span_min=10.0, span_max=15.0)
        ch.allocate_lane("iron", DIRECTION_SOUTH, preferred_lane=0)
        ch.allocate_lane("water", DIRECTION_SOUTH, preferred_lane=1, tier="pipe")

        entities = ch.emit_straight_belts()
        # 5 tiles long x 2 allocated lanes = 10 entities
        self.assertEqual(len(entities), 10)

        belts = [e for e in entities if e["name"] == "fast-transport-belt"]
        pipes = [e for e in entities if e["name"] == "pipe"]
        self.assertEqual(len(belts), 5)
        self.assertEqual(len(pipes), 5)

        # Check belt coordinates (lane 0: x = 97.5, y in [10.5..14.5])
        for idx, b in enumerate(belts):
            self.assertEqual(b["position"]["x"], 97.5)
            self.assertEqual(b["position"]["y"], 10.5 + idx)
            self.assertEqual(b["direction"], DIRECTION_SOUTH)

        # Check pipe coordinates (lane 1: x = 98.5, y in [10.5..14.5])
        for idx, p in enumerate(pipes):
            self.assertEqual(p["position"]["x"], 98.5)
            self.assertEqual(p["position"]["y"], 10.5 + idx)

    def test_router_intersection_straight_crossing(self) -> None:
        """Verify straight-through orthogonal crossing without collisions."""
        h_ch = StreetChannel("h_ch", "H", base_coord=97.0, span_min=3.0, span_max=197.0)
        v_ch = StreetChannel("v_ch", "V", base_coord=97.0, span_min=3.0, span_max=197.0)

        # Allocate 3 forward and 3 reverse lanes in both channels
        for i in range(3):
            h_ch.allocate_lane(f"h_fwd_{i}", DIRECTION_EAST, preferred_lane=i)
            v_ch.allocate_lane(f"v_fwd_{i}", DIRECTION_SOUTH, preferred_lane=i)
        for i in range(3, 6):
            h_ch.allocate_lane(f"h_rev_{i}", DIRECTION_WEST, preferred_lane=i)
            v_ch.allocate_lane(f"v_rev_{i}", DIRECTION_NORTH, preferred_lane=i)

        router = RouterIntersection(
            center_x=100.0,
            center_y=100.0,
            h_channel=h_ch,
            v_channel=v_ch,
        )

        ents = router.emit_intersection_entities()

        # Check vertical traffic: 6 lanes x 6 surface belt tiles = 36 surface belts
        surface_belts = [e for e in ents if e["name"] == "fast-transport-belt"]
        self.assertEqual(len(surface_belts), 36)

        # Check horizontal traffic: 6 lanes x 2 underground belt hoods = 12 underground hoods
        ug_belts = [e for e in ents if e["name"] == "fast-underground-belt"]
        self.assertEqual(len(ug_belts), 12)

        # Verify zero surface collisions
        collisions = check_collisions(ents)
        self.assertEqual(collisions, [])

        # Verify underground belt pairing
        unpaired = check_underground_pairing(ents)
        self.assertEqual(unpaired, [])

    def test_router_intersection_pipes(self) -> None:
        """Verify pipe-to-ground crossing pairs in router intersection."""
        h_ch = StreetChannel("h_ch", "H", base_coord=97.0, span_min=3.0, span_max=197.0)
        v_ch = StreetChannel("v_ch", "V", base_coord=97.0, span_min=3.0, span_max=197.0)

        h_ch.allocate_lane("crude_oil", DIRECTION_EAST, preferred_lane=0, tier="pipe")
        v_ch.allocate_lane("water", DIRECTION_SOUTH, preferred_lane=0, tier="pipe")

        router = RouterIntersection(
            center_x=100.0,
            center_y=100.0,
            h_channel=h_ch,
            v_channel=v_ch,
        )

        ents = router.emit_intersection_entities()
        pipes_to_ground = [e for e in ents if e["name"] == "pipe-to-ground"]
        self.assertEqual(len(pipes_to_ground), 4)

        # Verify pipe-to-ground pairing
        unpaired = check_underground_pairing(ents)
        self.assertEqual(unpaired, [])

    def test_router_intersection_turns(self) -> None:
        """Verify 90-degree turning connections peel off cleanly."""
        h_ch = StreetChannel("h_ch", "H", base_coord=97.0, span_min=3.0, span_max=197.0)
        v_ch = StreetChannel("v_ch", "V", base_coord=97.0, span_min=3.0, span_max=197.0)

        # Allocate lanes for turns
        h_ch.allocate_lane("east_to_south", DIRECTION_EAST, preferred_lane=0)
        v_ch.allocate_lane("east_to_south", DIRECTION_SOUTH, preferred_lane=0)

        router = RouterIntersection(
            center_x=100.0,
            center_y=100.0,
            h_channel=h_ch,
            v_channel=v_ch,
        )
        router.add_turn("H", 0, "V", 0)

        ents = router.emit_intersection_entities()
        # Verify no collisions in turn
        collisions = check_collisions(ents)
        self.assertEqual(collisions, [])

        # Corner entity is at (97.5, 97.5) with direction South
        corner = next(e for e in ents if e["position"]["x"] == 97.5 and e["position"]["y"] == 97.5)
        self.assertEqual(corner["direction"], DIRECTION_SOUTH)

    def test_router_intersection_switches(self) -> None:
        """Verify 2x2 approach splitters bridge adjacent lanes."""
        h_ch = StreetChannel("h_ch", "H", base_coord=97.0, span_min=3.0, span_max=197.0)
        v_ch = StreetChannel("v_ch", "V", base_coord=97.0, span_min=3.0, span_max=197.0)

        h_ch.allocate_lane("iron_bus_0", DIRECTION_EAST, preferred_lane=0)
        h_ch.allocate_lane("iron_bus_1", DIRECTION_EAST, preferred_lane=1)

        router = RouterIntersection(
            center_x=100.0,
            center_y=100.0,
            h_channel=h_ch,
            v_channel=v_ch,
        )
        router.add_switch("H", 0, 1, filter_item="iron-plate")

        ents = router.emit_intersection_entities()
        splitters = [e for e in ents if e["name"] == "fast-splitter"]
        self.assertEqual(len(splitters), 1)

        spl = splitters[0]
        self.assertEqual(spl["direction"], DIRECTION_EAST)
        self.assertEqual(spl["filter"], "iron-plate")
        # Center y is halfway between lane 0 (97.5) and lane 1 (98.5) -> 98.0
        self.assertEqual(spl["position"]["y"], 98.0)
        # Approach offset 1 before x_min (97.0 - 0.5 - 1.0 = 95.5)
        self.assertEqual(spl["position"]["x"], 95.5)

        # Check collision checker considers splitter valid
        collisions = check_collisions(ents)
        self.assertEqual(collisions, [])

    def test_cityblock_chassis_integration(self) -> None:
        """Verify CityBlockChassis helper methods for channels and intersections."""
        from foundry.chassis import CityBlockChassis

        chassis = CityBlockChassis(pitch=100, core_width=94, core_height=94, channel_width=6)

        # Horizontal channel north of block (0, 0)
        h_ch = chassis.get_horizontal_channel(0, 0)
        self.assertEqual(h_ch.orientation, "H")
        self.assertEqual(h_ch.base_coord, 97.0)
        self.assertEqual(h_ch.span_min, 3.0)
        self.assertEqual(h_ch.span_max, 97.0)

        # Vertical channel east of block (0, 0)
        v_ch = chassis.get_vertical_channel(0, 0)
        self.assertEqual(v_ch.orientation, "V")
        self.assertEqual(v_ch.base_coord, 97.0)
        self.assertEqual(v_ch.span_min, 3.0)
        self.assertEqual(v_ch.span_max, 97.0)

        # Router intersection at grid corner (1, 1) -> center (100.0, 100.0)
        router = chassis.get_intersection(1, 1, h_channel=h_ch, v_channel=v_ch)
        self.assertEqual(router.center_x, 100.0)
        self.assertEqual(router.center_y, 100.0)

    def test_dynamic_underground_span_resolution(self) -> None:
        """Verify data-driven prototype span lookups for base, Space Age, and modded belts."""
        from foundry.entity_helpers import (
            get_compatible_underground_belt,
            get_underground_span,
            register_underground_span,
        )

        # Base spans
        self.assertEqual(get_underground_span("underground-belt"), 5)
        self.assertEqual(get_underground_span("fast-underground-belt"), 7)
        self.assertEqual(get_underground_span("express-underground-belt"), 9)

        # Dynamic registration for modded tier
        register_underground_span("mod-hyper-underground-belt", 15)
        self.assertEqual(get_underground_span("mod-hyper-underground-belt"), 15)

        # Compatible tier selection based on physical required span
        # 6-tile gap requires span >= 6
        self.assertEqual(
            get_compatible_underground_belt(6, preferred_name="underground-belt"),
            "fast-underground-belt",
        )
        # Fast belt already meets 6-tile gap
        self.assertEqual(
            get_compatible_underground_belt(6, preferred_name="fast-underground-belt"),
            "fast-underground-belt",
        )
        # 14-tile gap selects kr-advanced-underground-belt (span 14 >= 14)
        register_underground_span("kr-advanced-underground-belt", 14)
        self.assertEqual(
            get_compatible_underground_belt(14),
            "kr-advanced-underground-belt",
        )
        # 15-tile gap selects registered modded belt (span 15 >= 15)
        self.assertEqual(
            get_compatible_underground_belt(15),
            "mod-hyper-underground-belt",
        )

    def test_check_underground_pairing_mods(self) -> None:
        """Verify that check_underground_pairing dynamically supports registered modded spans."""
        from foundry.entity_helpers import register_underground_span

        # Modded belt with span 14
        register_underground_span("kr-advanced-underground-belt", 14)
        mod_entities = [
            {
                "name": "kr-advanced-underground-belt",
                "type": "input",
                "position": {"x": 0.5, "y": 2.5},
                "direction": DIRECTION_EAST,
            },
            {
                "name": "kr-advanced-underground-belt",
                "type": "output",
                "position": {"x": 14.5, "y": 2.5},
                "direction": DIRECTION_EAST,
            },
        ]
        self.assertEqual(check_underground_pairing(mod_entities), [])

        # Exceeding modded span (15 > 14) fails pairing
        invalid_mod_entities = [
            {
                "name": "kr-advanced-underground-belt",
                "type": "input",
                "position": {"x": 0.5, "y": 2.5},
                "direction": DIRECTION_EAST,
            },
            {
                "name": "kr-advanced-underground-belt",
                "type": "output",
                "position": {"x": 15.5, "y": 2.5},
                "direction": DIRECTION_EAST,
            },
        ]
        self.assertEqual(len(check_underground_pairing(invalid_mod_entities)), 2)


if __name__ == "__main__":
    unittest.main()
