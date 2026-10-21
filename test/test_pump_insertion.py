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
from foundry.placement import PlacementResult, RouteResult
from foundry.pump_insertion import (
    PumpInsertionConfig,
    _compress_straight_pipes,
    _get_pump_tiles,
    _shrink_underground_pipe_spans,
    build_conduit_graph,
    component_extent,
    find_fluid_components,
    insert_pipeline_pumps,
)


class TestPumpInsertion(unittest.TestCase):
    """Test suite for physical pipeline pump insertion."""

    def test_get_pump_tiles_all_directions(self) -> None:
        """Pump input and output tiles match each cardinal orientation."""
        self.assertEqual(_get_pump_tiles(10.5, 20.0, DIRECTION_NORTH), ((10, 20), (10, 19)))
        self.assertEqual(_get_pump_tiles(10.5, 20.0, DIRECTION_SOUTH), ((10, 19), (10, 20)))
        self.assertEqual(_get_pump_tiles(10.0, 20.5, DIRECTION_EAST), ((9, 20), (10, 20)))
        self.assertEqual(_get_pump_tiles(10.0, 20.5, DIRECTION_WEST), ((10, 20), (9, 20)))

    def test_build_conduit_graph_connections(self) -> None:
        """Conduit graph connects surface pipes, pipe-to-ground mouths/spans, and pump endpoints."""
        p_north = {"name": "pump", "position": [10.5, 2.0], "direction": DIRECTION_NORTH}
        p_east = {"name": "pump", "position": {"x": 20.0, "y": 5.5}, "direction": DIRECTION_EAST}
        entities = [
            # Surface pipe feeding pump_north input at (10, 2) from South (10, 3)
            {"name": "pipe", "position": [10.5, 3.5]},
            p_north,
            # Surface pipe receiving pump_north output at (10, 1) to North (10, 0)
            {"name": "pipe", "position": {"x": 10.5, "y": 0.5}},
            # Pipe connected to pipe-to-ground mouth at (11, 0) facing West
            {"name": "pipe-to-ground", "position": {"x": 11.5, "y": 0.5}, "direction": DIRECTION_WEST},
            # Unaligned pipe-to-ground beside (10, 3) facing North (mouth at (9, 2), not (10, 3))
            {"name": "pipe-to-ground", "position": {"x": 9.5, "y": 3.5}, "direction": DIRECTION_NORTH},
            # Underground pair from (11, 0) East to (15, 0)
            {"name": "pipe-to-ground", "position": {"x": 15.5, "y": 0.5}, "direction": DIRECTION_EAST},
            # Mouth-to-mouth pipe-to-ground at (16, 0) facing West
            {"name": "pipe-to-ground", "position": {"x": 16.5, "y": 0.5}, "direction": DIRECTION_WEST},
            # Underground pair to (18, 0) facing East, feeding pump_east at (20.0, 5.5)
            {"name": "pipe-to-ground", "position": {"x": 18.5, "y": 5.5}, "direction": DIRECTION_EAST},
            p_east,
            {"name": "pipe-to-ground", "position": {"x": 21.5, "y": 5.5}, "direction": DIRECTION_WEST},
        ]
        adj, _tile_map, _pipe_entities = build_conduit_graph(entities)
        self.assertIn(("pump_in", id(p_north), 10, 2), adj[("pipe", 10, 3)])
        self.assertIn(("pump_out", id(p_north), 10, 1), adj[("pipe", 10, 0)])
        self.assertIn(("pipe-to-ground", 11, 0), adj[("pipe", 10, 0)])
        self.assertNotIn(("pipe-to-ground", 9, 3), adj[("pipe", 10, 3)])
        self.assertIn(("pipe-to-ground", 15, 0), adj[("pipe-to-ground", 11, 0)])
        self.assertIn(("pipe-to-ground", 16, 0), adj[("pipe-to-ground", 15, 0)])
        self.assertIn(("pump_in", id(p_east), 19, 5), adj[("pipe-to-ground", 18, 5)])
        self.assertIn(("pump_out", id(p_east), 20, 5), adj[("pipe-to-ground", 21, 5)])

        comps = find_fluid_components(adj)
        self.assertGreaterEqual(len(comps), 3)
        self.assertEqual(component_extent(set()), (0.0, 0.0, 0.0, 0.0, 0.0))

    def test_compress_straight_pipes_boundaries_and_blocking(self) -> None:
        """Straight pipe compression respects minimum run length and existing underground corridors."""
        entities = (
            [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(8)]
            + [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 2.5}} for x in range(10)]
            + [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 10.5}} for x in range(10)]
            + [
                {
                    "name": "pipe-to-ground",
                    "position": {"x": 2.5, "y": 10.5},
                    "direction": DIRECTION_WEST,
                }
            ]
        )
        occ = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in entities}
        grid = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in entities}
        placement = PlacementResult(
            entities=entities,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _compress_straight_pipes(placement, protected_tiles=set())

        y0_p2g = [
            e for e in placement.entities
            if e.get("name") == "pipe-to-ground" and e["position"]["y"] == 0.5
        ]
        self.assertEqual(len(y0_p2g), 0)

        y2_p2g = [
            e for e in placement.entities
            if e.get("name") == "pipe-to-ground" and e["position"]["y"] == 2.5
        ]
        self.assertEqual(len(y2_p2g), 2)
        for x in range(3, 7):
            self.assertNotIn((float(x) + 0.5, 2.5), placement.grid_entities)

        y10_p2g = [
            e for e in placement.entities
            if e.get("name") == "pipe-to-ground" and e["position"]["y"] == 10.5
        ]
        self.assertEqual(len(y10_p2g), 1)

        ents_v = (
            [{"name": "pipe", "position": {"x": 5.5, "y": float(y) + 0.5}} for y in range(12)]
            + [{"name": "pipe-to-ground", "position": {"x": 5.5, "y": 2.5}, "direction": DIRECTION_NORTH}]
        )
        occ_v = {(5.5, float(y) + 0.5) for y in range(12)}
        grid_v = {(5.5, float(e["position"]["y"])): e for e in ents_v}
        placement_v = PlacementResult(
            entities=ents_v,
            occupied_positions=occ_v,
            grid_entities=grid_v,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _compress_straight_pipes(placement_v, protected_tiles=set())
        v_p2g = [e for e in placement_v.entities if e.get("name") == "pipe-to-ground"]
        self.assertEqual(len(v_p2g), 1)

        entities_19 = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 20.5}} for x in range(19)]
        occ_19 = {(float(x) + 0.5, 20.5) for x in range(19)}
        grid_19 = {(float(x) + 0.5, 20.5): e for x, e in enumerate(entities_19)}
        placement_19 = PlacementResult(
            entities=entities_19,
            occupied_positions=occ_19,
            grid_entities=grid_19,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _compress_straight_pipes(placement_19, protected_tiles=set())
        y20_p2g = [e for e in placement_19.entities if e.get("name") == "pipe-to-ground"]
        self.assertEqual(len(y20_p2g), 4)

    def test_shrink_underground_pipe_spans_partial_and_overlapping(self) -> None:
        """Underground span shrinking handles partial unrolling and avoids colliding with overlapping spans."""
        p_u0 = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 0.5}, "direction": DIRECTION_WEST}
        p_v0 = {"name": "pipe-to-ground", "position": {"x": 9.5, "y": 0.5}, "direction": DIRECTION_EAST}
        tail_pipes = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(10, 31)]
        entities = [p_u0, p_v0, *tail_pipes]
        occ = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in entities}
        occ.add((5.5, 0.5))
        grid = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in entities}
        placement = PlacementResult(
            entities=entities,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _shrink_underground_pipe_spans(placement, protected_tiles=set(), max_segment_extent=20)
        self.assertEqual(p_u0["position"], {"x": 4.5, "y": 0.5})
        self.assertEqual(p_v0["position"], {"x": 6.5, "y": 0.5})
        for x in (0, 1, 2, 3, 7, 8, 9):
            self.assertEqual(placement.grid_entities[(float(x) + 0.5, 0.5)]["name"], "pipe")

        p_u1 = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 5.5}, "direction": DIRECTION_WEST}
        p_v1 = {"name": "pipe-to-ground", "position": {"x": 9.5, "y": 5.5}, "direction": DIRECTION_EAST}
        p_u2 = {"name": "pipe-to-ground", "position": {"x": 2.5, "y": 5.5}, "direction": DIRECTION_WEST}
        p_v2 = {"name": "pipe-to-ground", "position": {"x": 4.5, "y": 5.5}, "direction": DIRECTION_EAST}
        tail2 = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 5.5}} for x in range(10, 31)]
        ents2 = [p_u1, p_v1, p_u2, p_v2, *tail2]
        occ2 = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in ents2}
        grid2 = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in ents2}
        placement2 = PlacementResult(
            entities=ents2,
            occupied_positions=occ2,
            grid_entities=grid2,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _shrink_underground_pipe_spans(placement2, protected_tiles={(0, 5), (9, 5)}, max_segment_extent=20)
        self.assertEqual(p_u1["position"], {"x": 0.5, "y": 5.5})
        self.assertEqual(p_v1["position"], {"x": 9.5, "y": 5.5})


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

    def test_underground_pipe_chain_pump_insertion(self) -> None:
        """Overextended pipeline of pipe-to-ground hops unrolls unobstructed spans and inserts a pump."""
        y_coords: list[float] = [0.5]
        entities = [
            {"name": "pipe", "position": {"x": 10.5, "y": 0.5}},
        ]
        for hop in range(38):
            y_start = 1 + hop * 10
            y_end = y_start + 9
            y_s = round(float(y_start) + 0.5, 2)
            y_e = round(float(y_end) + 0.5, 2)
            y_coords.extend((y_s, y_e))
            entities.append(
                {
                    "name": "pipe-to-ground",
                    "position": {"x": 10.5, "y": y_s},
                    "direction": DIRECTION_NORTH,
                }
            )
            entities.append(
                {
                    "name": "pipe-to-ground",
                    "position": {"x": 10.5, "y": y_e},
                    "direction": DIRECTION_SOUTH,
                }
            )
        occ = {(10.5, y) for y in y_coords}
        grid = {(10.5, y): ent for y, ent in zip(y_coords, entities, strict=True)}
        placement = PlacementResult(
            entities=entities,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"water": {pos: DIRECTION_SOUTH for pos in occ}},
            tile_to_nets={pos: {"water"} for pos in occ},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {
            "units": 1000,
            "pins": [{"name": "water", "direction": "INPUT", "x": 10500, "y": 500}],
        }
        netlist_data = {"fluids": ["water"]}

        count = insert_pipeline_pumps(
            placement, routes, def_data, netlist_data, max_segment_extent=250
        )
        self.assertGreaterEqual(count, 1)
        checker = BeltDrcChecker()
        violations = checker.check_entities(placement.entities)
        self.assertEqual([v for v in violations if v.code == "DRC-16"], [])

    def test_shrink_overlapping_ug_and_chained_pair_updates(self) -> None:
        """Shrinking one underground pair updates its span set for subsequent pairs on the same axis."""
        p_a_u = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 0.5}, "direction": DIRECTION_WEST}
        p_a_v = {"name": "pipe-to-ground", "position": {"x": 6.5, "y": 0.5}, "direction": DIRECTION_EAST}
        p_b_u = {"name": "pipe-to-ground", "position": {"x": 1.5, "y": 0.5}, "direction": DIRECTION_WEST}
        p_b_v = {"name": "pipe-to-ground", "position": {"x": 9.5, "y": 0.5}, "direction": DIRECTION_EAST}
        tail = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(10, 35)]
        entities = [p_a_u, p_a_v, p_b_u, p_b_v, *tail]
        occ = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in entities}
        grid = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in entities}
        placement = PlacementResult(
            entities=entities,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _shrink_underground_pipe_spans(placement, protected_tiles={(0, 0), (9, 0)}, max_segment_extent=20)
        self.assertEqual(p_a_v["position"], {"x": 6.5, "y": 0.5})

        p1_u = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 2.5}, "direction": DIRECTION_WEST}
        p1_v = {"name": "pipe-to-ground", "position": {"x": 4.5, "y": 2.5}, "direction": DIRECTION_EAST}
        p2_u = {"name": "pipe-to-ground", "position": {"x": 3.5, "y": 2.5}, "direction": DIRECTION_WEST}
        p2_v = {"name": "pipe-to-ground", "position": {"x": 8.5, "y": 2.5}, "direction": DIRECTION_EAST}
        tail_2 = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 2.5}} for x in range(9, 35)]
        ents_2 = [p1_u, p1_v, p2_u, p2_v, *tail_2]
        occ_2 = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in ents_2}
        occ_2.add((2.5, 2.5))
        occ_2.add((5.5, 2.5))
        grid_2 = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in ents_2}
        placement_2 = PlacementResult(
            entities=ents_2,
            occupied_positions=occ_2,
            grid_entities=grid_2,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _shrink_underground_pipe_spans(placement_2, protected_tiles={(4, 2), (3, 2)}, max_segment_extent=20)
        self.assertEqual(p1_u["position"], {"x": 1.5, "y": 2.5})
        self.assertEqual(p2_v["position"], {"x": 6.5, "y": 2.5})

    def test_perpendicular_fluid_and_vertical_partial_shrink_and_other_ug_endpoint(self) -> None:
        """Span shrinking respects perpendicular fluid neighbors, vertical partial unrolling, and other pair endpoints."""
        p_u = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 0.5}, "direction": DIRECTION_WEST}
        p_v = {"name": "pipe-to-ground", "position": {"x": 5.5, "y": 0.5}, "direction": DIRECTION_EAST}
        perp_pipe = {"name": "pipe", "position": {"x": 2.5, "y": 1.5}}
        tail = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(6, 30)]
        ents = [p_u, p_v, perp_pipe, *tail]
        occ = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in ents}
        grid = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in ents}
        placement = PlacementResult(
            entities=ents,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _shrink_underground_pipe_spans(placement, protected_tiles=set(), max_segment_extent=20)
        self.assertEqual(p_u["name"], "pipe-to-ground")
        self.assertEqual(p_u["position"], {"x": 2.5, "y": 0.5})

        p_u_prot = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 4.5}, "direction": DIRECTION_WEST}
        p_v_prot = {"name": "pipe-to-ground", "position": {"x": 5.5, "y": 4.5}, "direction": DIRECTION_EAST}
        tail_prot = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 4.5}} for x in range(6, 30)]
        ents_prot = [p_u_prot, p_v_prot, *tail_prot]
        occ_prot = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in ents_prot}
        grid_prot = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in ents_prot}
        placement_prot = PlacementResult(
            entities=ents_prot,
            occupied_positions=occ_prot,
            grid_entities=grid_prot,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _shrink_underground_pipe_spans(placement_prot, protected_tiles={(3, 5)}, max_segment_extent=20)
        self.assertEqual(p_u_prot["name"], "pipe-to-ground")
        self.assertEqual(p_u_prot["position"], {"x": 3.5, "y": 4.5})

        vp_u = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 10.5}, "direction": DIRECTION_NORTH}
        vp_v = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 15.5}, "direction": DIRECTION_SOUTH}
        v_tail = [{"name": "pipe", "position": {"x": 0.5, "y": float(y) + 0.5}} for y in range(16, 40)]
        v_ents = [vp_u, vp_v, *v_tail]
        v_occ = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in v_ents}
        v_occ.add((0.5, 12.5))
        v_grid = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in v_ents}
        v_placement = PlacementResult(
            entities=v_ents,
            occupied_positions=v_occ,
            grid_entities=v_grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _shrink_underground_pipe_spans(v_placement, protected_tiles=set(), max_segment_extent=20)
        self.assertEqual(vp_u["position"], {"x": 0.5, "y": 11.5})
        self.assertEqual(vp_v["position"], {"x": 0.5, "y": 13.5})
        self.assertIs(v_placement.grid_entities[(0.5, 13.5)], vp_v)
        self.assertEqual(v_placement.grid_entities[(0.5, 14.5)]["name"], "pipe")
        self.assertEqual(v_placement.grid_entities[(0.5, 15.5)]["name"], "pipe")

        p_neg_u = {"name": "pipe-to-ground", "position": {"x": -8.5, "y": 0.5}, "direction": DIRECTION_WEST}
        p_neg_v = {"name": "pipe-to-ground", "position": {"x": -1.5, "y": 0.5}, "direction": DIRECTION_EAST}
        p_pos_u = {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 0.5}, "direction": DIRECTION_WEST}
        p_pos_v = {"name": "pipe-to-ground", "position": {"x": 5.5, "y": 0.5}, "direction": DIRECTION_EAST}
        neg_bridge = {"name": "pipe", "position": {"x": -0.5, "y": 0.5}}
        pos_tail = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(6, 25)]
        ents_mirror = [p_neg_u, p_neg_v, neg_bridge, p_pos_u, p_pos_v, *pos_tail]
        occ_mirror = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in ents_mirror}
        occ_mirror.add((-4.5, 0.5))
        grid_mirror = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in ents_mirror}
        placement_mirror = PlacementResult(
            entities=ents_mirror,
            occupied_positions=occ_mirror,
            grid_entities=grid_mirror,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        _shrink_underground_pipe_spans(
            placement_mirror, protected_tiles={(0, 0), (5, 0), (-2, 0)}, max_segment_extent=20
        )
        self.assertEqual(p_neg_u["position"], {"x": -5.5, "y": 0.5})

    def test_steam_engine_passthrough_and_west_flow_and_existing_pumps(self) -> None:
        """Pump insertion protects steam-engine passthrough tiles, handles West flow, and traverses existing pumps."""
        entities = [
            {"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}}
            for x in range(120)
        ]
        occ = {(float(x) + 0.5, 0.5) for x in range(120)}
        grid = {(float(x) + 0.5, 0.5): e for x, e in enumerate(entities)}
        placement = PlacementResult(
            entities=entities,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"steam"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"steam": {(float(x) + 0.5, 0.5): DIRECTION_WEST for x in range(120)}},
            tile_to_nets={(float(x) + 0.5, 0.5): {"steam"} for x in range(120)},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {
            "units": 1000,
            "pins": [
                {"name": "steam", "direction": "INPUT", "x": 119500, "y": 500},
                {"name": "steam", "direction": "OUTPUT", "x": 500, "y": 500},
            ],
            "components": [
                {"name": "se_1", "macro": "steam_cell", "x": 58000, "y": -6000},
                {"name": "boil_1", "macro": "boiler_cell", "x": 68000, "y": -4000},
            ],
        }
        netlist_data = {
            "fluids": ["steam"],
            "instances": [
                {"name": "se_1", "cell": "steam_cell"},
                {"name": "boil_1", "cell": "boiler_cell"},
            ],
            "cells": {
                "steam_cell": {"machine": "steam-engine", "inputs": [], "outputs": []},
                "boiler_cell": {"machine": "boiler", "inputs": [], "outputs": []},
            },
        }
        count = insert_pipeline_pumps(
            placement, routes, def_data, netlist_data, max_segment_extent=100, max_pumps=1
        )
        self.assertEqual(count, 1)
        pumps = [e for e in placement.entities if e.get("name") == "pump"]
        self.assertEqual(len(pumps), 1)
        self.assertEqual(pumps[0]["direction"], DIRECTION_WEST)
        in_t, out_t = _get_pump_tiles(pumps[0]["position"]["x"], pumps[0]["position"]["y"], pumps[0]["direction"])
        self.assertNotIn((60, 0), (in_t, out_t))
        self.assertIs(placement.grid_entities[(in_t[0] + 0.5, in_t[1] + 0.5)], pumps[0])
        self.assertIs(placement.grid_entities[(out_t[0] + 0.5, out_t[1] + 0.5)], pumps[0])

    def test_existing_pump_upstream_source_weighting_and_overlimit_tiebreak(self) -> None:
        """Pump insertion traverses existing pumps in full_adj and prioritizes cuts with smaller over_limit."""
        existing_pump = {"name": "pump", "position": {"x": 11.0, "y": 0.5}, "direction": DIRECTION_EAST}
        left_pipes = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(10)]
        right_pipes = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(12, 150)]
        entities = left_pipes + [existing_pump] + right_pipes
        occ = {(float(x) + 0.5, 0.5) for x in range(150)}
        grid = (
            {(float(x) + 0.5, 0.5): ent for x, ent in enumerate(left_pipes)}
            | {(10.5, 0.5): existing_pump, (11.5, 0.5): existing_pump}
            | {(float(x) + 0.5, 0.5): ent for x, ent in zip(range(12, 150), right_pipes, strict=True)}
        )
        placement = PlacementResult(
            entities=entities,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"water": {(float(x) + 0.5, 0.5): DIRECTION_EAST for x in range(150)}},
            tile_to_nets={(float(x) + 0.5, 0.5): {"water"} for x in range(150)},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {
            "units": 1000,
            "pins": [
                {"name": "water", "direction": "INPUT", "x": 500, "y": 500},
                {"name": "water", "direction": "INPUT", "x": 1500, "y": 500},
                {"name": "water", "direction": "INPUT", "x": 2500, "y": 500},
                {"name": "water", "direction": "INPUT", "x": 3500, "y": 500},
                {"name": "water", "direction": "INPUT", "x": 4500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 15500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 16500, "y": 500},
                {"name": "water", "direction": "INPUT", "x": 140500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 149500, "y": 500},
            ],
        }
        netlist_data = {"fluids": ["water"]}

        count = insert_pipeline_pumps(
            placement, routes, def_data, netlist_data, max_segment_extent=90, max_pumps=1
        )
        self.assertEqual(count, 1)
        new_pumps = [
            e for e in placement.entities
            if e.get("name") == "pump" and e is not existing_pump
        ]
        self.assertEqual(len(new_pumps), 1)
        self.assertEqual(new_pumps[0]["direction"], DIRECTION_EAST)
        px = new_pumps[0]["position"]["x"]
        self.assertGreaterEqual(px, 60.0)
        self.assertLessEqual(px, 100.0)

    def test_bfs_distance_and_overlimit_scoring_and_sink_accounting(self) -> None:
        """Pump placement respects BFS target distance, avoids over_limit cuts, and subtracts u_sinks from v_sinks."""
        horiz = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(150)]
        branch_left = [{"name": "pipe", "position": {"x": 50.5, "y": float(y) + 0.5}} for y in range(1, 91)]
        branch_right = [{"name": "pipe", "position": {"x": 90.5, "y": float(y) + 0.5}} for y in range(1, 91)]
        entities = horiz + branch_left + branch_right
        occ = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in entities}
        grid = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in entities}
        placement = PlacementResult(
            entities=entities,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        allowed = {(10, 0), (11, 0), (59, 0), (60, 0), (80, 0), (81, 0)}
        pin_tiles = {(float(x) + 0.5, 0.5) for x in range(150) if (x, 0) not in allowed}
        pin_tiles |= {(50.5, float(y) + 0.5) for y in range(1, 91)}
        pin_tiles |= {(90.5, float(y) + 0.5) for y in range(1, 91)}
        routes = RouteResult(
            net_surface_tiles={"water": {(float(x) + 0.5, 0.5): DIRECTION_EAST for x in range(150)}},
            tile_to_nets={(float(x) + 0.5, 0.5): {"water"} for x in range(150)},
            adjacent_fluid_conflicts=set(),
            pin_tiles=pin_tiles,
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {
            "units": 1000,
            "pins": [
                {"name": "water", "direction": "INPUT", "x": 500, "y": 500},
                {"name": "water", "direction": "INPUT", "x": 1500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 2500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 3500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 4500, "y": 500},
                {"name": "water", "direction": "INPUT", "x": 148500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 149500, "y": 500},
            ],
        }
        netlist_data = {"fluids": ["water"]}
        count = insert_pipeline_pumps(
            placement, routes, def_data, netlist_data, max_segment_extent=100, max_pumps=1
        )
        self.assertEqual(count, 1)
        pumps = [e for e in placement.entities if e.get("name") == "pump"]
        self.assertEqual(len(pumps), 1)
        self.assertEqual(pumps[0]["direction"], DIRECTION_WEST)
        self.assertEqual(pumps[0]["position"], {"x": 60.0, "y": 0.5})

        ents_l = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(111)]
        occ_l = {(float(x) + 0.5, 0.5) for x in range(111)}
        grid_l = {(float(x) + 0.5, 0.5): e for x, e in enumerate(ents_l)}
        placement_l = PlacementResult(
            entities=ents_l,
            occupied_positions=occ_l,
            grid_entities=grid_l,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        allowed_l = {(55, 0), (56, 0), (90, 0), (91, 0)}
        pins_l = {(float(x) + 0.5, 0.5) for x in range(111) if (x, 0) not in allowed_l}
        routes_l = RouteResult(
            net_surface_tiles={"water": dict.fromkeys(occ_l, DIRECTION_EAST)},
            tile_to_nets={pos: {"water"} for pos in occ_l},
            adjacent_fluid_conflicts=set(),
            pin_tiles=pins_l,
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_l = {
            "units": 1000,
            "pins": [
                {"name": "water", "direction": "INPUT", "x": 500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 1500, "y": 500},
            ],
        }
        count_l = insert_pipeline_pumps(
            placement_l, routes_l, def_l, {"fluids": ["water"]}, max_segment_extent=98, max_pumps=1
        )
        self.assertEqual(count_l, 1)
        pumps_l = [e for e in placement_l.entities if e.get("name") == "pump"]
        self.assertEqual(len(pumps_l), 1)
        self.assertEqual(pumps_l[0]["position"], {"x": 91.0, "y": 0.5})
        self.assertEqual(pumps_l[0]["direction"], DIRECTION_EAST)

    def test_component_fluid_ports_and_sep_penalty_and_overextended_order(self) -> None:
        """Pump insertion resolves machine fluid port geometry, penalizes non-separating cuts, and prioritizes worst extent."""
        comp1_pipes = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(71)]
        comp2_pipes = [{"name": "pipe", "position": {"x": 110.5, "y": float(y) + 0.5}} for y in range(11, 122)]
        entities = comp1_pipes + comp2_pipes
        occ = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)) for e in entities}
        grid = {(round(float(e["position"]["x"]), 2), round(float(e["position"]["y"]), 2)): e for e in entities}
        placement = PlacementResult(
            entities=entities,
            occupied_positions=occ,
            grid_entities=grid,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        routes = RouteResult(
            net_surface_tiles={"water": dict.fromkeys(occ, DIRECTION_NORTH)},
            tile_to_nets={pos: {"water"} for pos in occ},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_data = {
            "units": 1000,
            "pins": [],
            "components": [
                {"name": "m_src", "macro": "src_cell", "x": 108000, "y": 119000},
            ],
        }
        netlist_data = {
            "fluids": ["water"],
            "instances": [
                {"name": "m_src", "cell": "src_cell"},
            ],
            "cells": {
                "src_cell": {"machine": "chemical-plant", "width": 3, "height": 3, "inputs": [], "outputs": ["water"]},
            },
        }
        cfg = PumpInsertionConfig(max_segment_extent=50, max_pumps=1)
        count = insert_pipeline_pumps(placement, routes, def_data, netlist_data, cfg=cfg)
        self.assertEqual(count, 1)
        pumps = [e for e in placement.entities if e.get("name") == "pump"]
        self.assertEqual(len(pumps), 1)
        self.assertEqual(pumps[0]["position"]["x"], 110.5)
        self.assertEqual(pumps[0]["direction"], DIRECTION_NORTH)

        sink_pipes = [{"name": "pipe", "position": {"x": 0.5, "y": float(y) + 0.5}} for y in range(101)]
        occ_sk = {(0.5, float(y) + 0.5) for y in range(101)}
        grid_sk = {(0.5, float(y) + 0.5): e for y, e in enumerate(sink_pipes)}
        placement_sk = PlacementResult(
            entities=sink_pipes,
            occupied_positions=occ_sk,
            grid_entities=grid_sk,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        routes_sk = RouteResult(
            net_surface_tiles={"water": dict.fromkeys(occ_sk, DIRECTION_NORTH)},
            tile_to_nets={pos: {"water"} for pos in occ_sk},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_sk = {
            "units": 1000,
            "pins": [],
            "components": [{"name": "m_sink", "macro": "sink_cell", "x": 0, "y": 0}],
        }
        netlist_sk = {
            "fluids": ["water"],
            "instances": [{"name": "m_sink", "cell": "sink_cell"}],
            "cells": {
                "sink_cell": {"machine": "chemical-plant", "width": 3, "height": 3, "inputs": ["water"], "outputs": []},
            },
        }
        count_sk = insert_pipeline_pumps(placement_sk, routes_sk, def_sk, netlist_sk, cfg=cfg)
        self.assertEqual(count_sk, 1)
        pumps_sk = [e for e in placement_sk.entities if e.get("name") == "pump"]
        self.assertEqual(len(pumps_sk), 1)
        self.assertEqual(pumps_sk[0]["direction"], DIRECTION_NORTH)

        pipes_sep = [{"name": "pipe", "position": {"x": float(x) + 0.5, "y": 0.5}} for x in range(121)]
        occ_sep = {(float(x) + 0.5, 0.5) for x in range(121)}
        grid_sep = {(float(x) + 0.5, 0.5): e for x, e in enumerate(pipes_sep)}
        placement_sep = PlacementResult(
            entities=pipes_sep,
            occupied_positions=occ_sep,
            grid_entities=grid_sep,
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )
        allowed_sep = {(60, 0), (61, 0), (90, 0), (91, 0)}
        pins_sep = {(float(x) + 0.5, 0.5) for x in range(121) if (x, 0) not in allowed_sep}
        routes_sep = RouteResult(
            net_surface_tiles={"water": dict.fromkeys(occ_sep, DIRECTION_EAST)},
            tile_to_nets={pos: {"water"} for pos in occ_sep},
            adjacent_fluid_conflicts=set(),
            pin_tiles=pins_sep,
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        def_sep = {
            "units": 1000,
            "pins": [
                {"name": "water", "direction": "INPUT", "x": 80500, "y": 500},
                {"name": "water", "direction": "OUTPUT", "x": 110500, "y": 500},
            ],
        }
        count_sep = insert_pipeline_pumps(
            placement_sep, routes_sep, def_sep, {"fluids": ["water"]}, max_segment_extent=95, max_pumps=1
        )
        self.assertEqual(count_sep, 1)
        pumps_sep = [e for e in placement_sep.entities if e.get("name") == "pump"]
        self.assertEqual(len(pumps_sep), 1)
        self.assertEqual(pumps_sep[0]["position"], {"x": 91.0, "y": 0.5})
        self.assertEqual(pumps_sep[0]["direction"], DIRECTION_EAST)



if __name__ == "__main__":
    unittest.main()

