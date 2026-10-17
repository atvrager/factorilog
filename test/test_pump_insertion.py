"""Unit tests for inline pipeline pump insertion pass."""

from __future__ import annotations

import unittest

from foundry.belt_drc import BeltDrcChecker
from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
)
from foundry.jumper import RouteResult
from foundry.placement import PlacementResult
from foundry.pump_insertion import (
    build_conduit_graph,
    component_extent,
    find_fluid_components,
    insert_pipeline_pumps,
)


class TestPumpInsertion(unittest.TestCase):
    """Test suite for physical pipeline pump insertion."""

    def test_short_pipeline_no_pumps(self) -> None:
        """Pipelines within extent limits do not require any pumps."""
        entities = [
            {"name": "pipe", "position": {"x": float(i) + 0.5, "y": 0.5}}
            for i in range(50)
        ]
        placement = PlacementResult(
            entities=entities,
            occupied_positions={(round(float(i) + 0.5, 2), 0.5) for i in range(50)},
            grid_entities={(round(float(i) + 0.5, 2), 0.5): e for i, e in enumerate(entities)},
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"water": {(float(i) + 0.5, 0.5): DIRECTION_EAST for i in range(50)}},
            tile_to_nets={(float(i) + 0.5, 0.5): {"water"} for i in range(50)},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {"units": 1000, "pins": [{"name": "water", "direction": "INPUT", "x": 500, "y": 500}]}
        netlist_data = {"fluids": ["water"]}

        count = insert_pipeline_pumps(placement, routes, def_data, netlist_data, max_segment_extent=250)
        self.assertEqual(count, 0)
        self.assertFalse(any(e.get("name") == "pump" for e in placement.entities))

    def test_vertical_pipeline_pump_insertion(self) -> None:
        """Overextended vertical pipeline gets inline pump inserted and passes DRC-16."""
        # 400 vertical pipes going North from y=400 down to y=1
        entities = [
            {"name": "pipe", "position": {"x": 10.5, "y": float(y) + 0.5}}
            for y in range(400)
        ]
        placement = PlacementResult(
            entities=entities,
            occupied_positions={(10.5, round(float(y) + 0.5, 2)) for y in range(400)},
            grid_entities={(10.5, round(float(y) + 0.5, 2)): e for y, e in enumerate(entities)},
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"water": {(10.5, float(y) + 0.5): DIRECTION_NORTH for y in range(400)}},
            tile_to_nets={(10.5, float(y) + 0.5): {"water"} for y in range(400)},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {
            "units": 1000,
            "pins": [{"name": "water", "direction": "INPUT", "x": 10500, "y": 399500}],
        }
        netlist_data = {"fluids": ["water"]}

        checker = BeltDrcChecker()
        # Before insertion: DRC-16 must fail
        violations_before = checker.check_entities(placement.entities)
        self.assertTrue(any(v.code == "DRC-16" for v in violations_before))

        # Insert pumps
        count = insert_pipeline_pumps(placement, routes, def_data, netlist_data, max_segment_extent=250)
        self.assertGreaterEqual(count, 1)

        # After insertion: DRC-16 must pass
        violations_after = checker.check_entities(placement.entities)
        drc16 = [v for v in violations_after if v.code == "DRC-16"]
        self.assertEqual(drc16, [])

        # Verify pump entity exists
        pumps = [e for e in placement.entities if e.get("name") == "pump"]
        self.assertGreaterEqual(len(pumps), 1)

    def test_horizontal_pipeline_pump_insertion(self) -> None:
        """Overextended horizontal pipeline gets inline pump inserted and passes DRC-16."""
        entities = [
            {"name": "pipe", "position": {"x": float(x) + 0.5, "y": 20.5}}
            for x in range(380)
        ]
        placement = PlacementResult(
            entities=entities,
            occupied_positions={(round(float(x) + 0.5, 2), 20.5) for x in range(380)},
            grid_entities={(round(float(x) + 0.5, 2), 20.5): e for x, e in enumerate(entities)},
            inst_to_entity={},
            fluids={"crude-oil"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"crude-oil": {(float(x) + 0.5, 20.5): DIRECTION_EAST for x in range(380)}},
            tile_to_nets={(float(x) + 0.5, 20.5): {"crude-oil"} for x in range(380)},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {
            "units": 1000,
            "pins": [{"name": "crude-oil", "direction": "INPUT", "x": 500, "y": 20500}],
        }
        netlist_data = {"fluids": ["crude-oil"]}

        count = insert_pipeline_pumps(placement, routes, def_data, netlist_data, max_segment_extent=250)
        self.assertGreaterEqual(count, 1)

        checker = BeltDrcChecker()
        violations = checker.check_entities(placement.entities)
        self.assertEqual([v for v in violations if v.code == "DRC-16"], [])

    def test_multi_segment_pipeline_pumps(self) -> None:
        """Very long pipeline (800 tiles) receives multiple pumps to keep each segment <= 250 tiles."""
        entities = [
            {"name": "pipe", "position": {"x": 5.5, "y": float(y) + 0.5}}
            for y in range(800)
        ]
        placement = PlacementResult(
            entities=entities,
            occupied_positions={(5.5, round(float(y) + 0.5, 2)) for y in range(800)},
            grid_entities={(5.5, round(float(y) + 0.5, 2)): e for y, e in enumerate(entities)},
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"water": {(5.5, float(y) + 0.5): DIRECTION_SOUTH for y in range(800)}},
            tile_to_nets={(5.5, float(y) + 0.5): {"water"} for y in range(800)},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {
            "units": 1000,
            "pins": [{"name": "water", "direction": "INPUT", "x": 5500, "y": 500}],
        }
        netlist_data = {"fluids": ["water"]}

        count = insert_pipeline_pumps(placement, routes, def_data, netlist_data, max_segment_extent=250)
        self.assertGreaterEqual(count, 3)

        checker = BeltDrcChecker()
        violations = checker.check_entities(placement.entities)
        self.assertEqual([v for v in violations if v.code == "DRC-16"], [])

    def test_mixed_sources_and_sinks_pump_direction(self) -> None:
        """Pump points from net-surplus half to net-deficit half even when a minority source is closer to the cut."""
        entities = [
            {"name": "pipe", "position": {"x": 10.5, "y": float(y) + 0.5}}
            for y in range(400)
        ]
        placement = PlacementResult(
            entities=entities,
            occupied_positions={(10.5, round(float(y) + 0.5, 2)) for y in range(400)},
            grid_entities={(10.5, round(float(y) + 0.5, 2)): e for y, e in enumerate(entities)},
            inst_to_entity={},
            fluids={"light-oil"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"light-oil": {(10.5, float(y) + 0.5): DIRECTION_SOUTH for y in range(400)}},
            tile_to_nets={(10.5, float(y) + 0.5): {"light-oil"} for y in range(400)},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        # North half (y < 200): 3 sources at y=0..2, 1 sink at y=10
        # South half (y >= 200): 1 source at y=220 (very close to middle cut!), 4 sinks at y=380..395
        def_data = {
            "units": 1000,
            "pins": [
                {"name": "light-oil", "direction": "INPUT", "x": 10500, "y": 500},
                {"name": "light-oil", "direction": "INPUT", "x": 10500, "y": 1500},
                {"name": "light-oil", "direction": "INPUT", "x": 10500, "y": 2500},
                {"name": "light-oil", "direction": "OUTPUT", "x": 10500, "y": 10500},
                {"name": "light-oil", "direction": "INPUT", "x": 10500, "y": 220500},
                {"name": "light-oil", "direction": "OUTPUT", "x": 10500, "y": 380500},
                {"name": "light-oil", "direction": "OUTPUT", "x": 10500, "y": 385500},
                {"name": "light-oil", "direction": "OUTPUT", "x": 10500, "y": 390500},
                {"name": "light-oil", "direction": "OUTPUT", "x": 10500, "y": 395500},
            ],
        }
        netlist_data = {"fluids": ["light-oil"]}

        count = insert_pipeline_pumps(placement, routes, def_data, netlist_data, max_segment_extent=250)
        self.assertEqual(count, 1)
        pumps = [e for e in placement.entities if e.get("name") == "pump"]
        self.assertEqual(len(pumps), 1)
        self.assertEqual(pumps[0]["direction"], DIRECTION_SOUTH)


if __name__ == "__main__":
    unittest.main()

