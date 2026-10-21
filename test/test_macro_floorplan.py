"""Unit tests for Level 1 macro floorplanning, recipe clustering, and channel routing."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from typing import Any

from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    check_collisions,
    check_underground_pairing,
)
from foundry.macro_floorplan import MacroFloorplan, MacroFloorplanner, RecipeCluster


MOCK_NETLIST_DATA: dict[str, Any] = {
    "demand": {"automation-science-pack": 60.0},
    "fluids": ["crude-oil", "water"],
    "inputs": ["copper-ore", "iron-ore"],
    "outputs": ["automation-science-pack"],
    "cells": {
        "electric-furnace.iron-plate": {
            "cell": "electric-furnace.iron-plate",
            "recipe": "iron-plate",
            "width": 5,
            "height": 3,
            "inputs": ["iron-ore"],
            "outputs": ["iron-plate"],
        },
        "electric-furnace.copper-plate": {
            "cell": "electric-furnace.copper-plate",
            "recipe": "copper-plate",
            "width": 5,
            "height": 3,
            "inputs": ["copper-ore"],
            "outputs": ["copper-plate"],
        },
        "assembling-machine-3.iron-gear-wheel": {
            "cell": "assembling-machine-3.iron-gear-wheel",
            "recipe": "iron-gear-wheel",
            "width": 5,
            "height": 3,
            "inputs": ["iron-plate"],
            "outputs": ["iron-gear-wheel"],
        },
        "assembling-machine-3.automation-science-pack": {
            "cell": "assembling-machine-3.automation-science-pack",
            "recipe": "automation-science-pack",
            "width": 5,
            "height": 5,
            "inputs": ["copper-plate", "iron-gear-wheel"],
            "outputs": ["automation-science-pack"],
        },
    },
    "instances": [
        {
            "name": "u_iron_1",
            "cell": "electric-furnace.iron-plate",
            "recipe": "iron-plate",
            "rank": 0,
        },
        {
            "name": "u_iron_2",
            "cell": "electric-furnace.iron-plate",
            "recipe": "iron-plate",
            "rank": 0,
        },
        {
            "name": "u_copper_1",
            "cell": "electric-furnace.copper-plate",
            "recipe": "copper-plate",
            "rank": 0,
        },
        {
            "name": "u_gear_1",
            "cell": "assembling-machine-3.iron-gear-wheel",
            "recipe": "iron-gear-wheel",
            "rank": 1,
        },
        {
            "name": "u_sci_1",
            "cell": "assembling-machine-3.automation-science-pack",
            "recipe": "automation-science-pack",
            "rank": 2,
        },
        {
            "name": "u_sci_2",
            "cell": "assembling-machine-3.automation-science-pack",
            "recipe": "automation-science-pack",
            "rank": 2,
        },
    ],
}


class TestMacroFloorplan(unittest.TestCase):
    """Test suite for Level 1 macro floorplanning, clustering, and channel routing."""

    def setUp(self) -> None:
        self.floorplanner = MacroFloorplanner(max_block_area=8836.0)

    def test_cluster_netlist_grouping(self) -> None:
        """Verify grouping of instances by recipe and area calculation."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        self.assertEqual(len(clusters), 4)

        cluster_map = {c.recipe: c for c in clusters}
        self.assertIn("iron-plate", cluster_map)
        self.assertIn("copper-plate", cluster_map)
        self.assertIn("iron-gear-wheel", cluster_map)
        self.assertIn("automation-science-pack", cluster_map)

        # iron-plate has 2 instances of 5x3 = 30.0 tiles
        iron_cluster = cluster_map["iron-plate"]
        self.assertEqual(len(iron_cluster.instances), 2)
        self.assertEqual(iron_cluster.total_area, 30.0)
        self.assertEqual(iron_cluster.inputs, {"iron-ore"})
        self.assertEqual(iron_cluster.outputs, {"iron-plate"})

        # automation-science-pack has 2 instances of 5x5 = 50.0 tiles
        sci_cluster = cluster_map["automation-science-pack"]
        self.assertEqual(len(sci_cluster.instances), 2)
        self.assertEqual(sci_cluster.total_area, 50.0)
        self.assertEqual(sci_cluster.inputs, {"copper-plate", "iron-gear-wheel"})
        self.assertEqual(sci_cluster.outputs, {"automation-science-pack"})

    def test_multi_block_stage_allocation(self) -> None:
        """Verify dividing an oversized stage across multiple blocks."""
        # Set small max_block_area (20 tiles). A 30-tile cluster must split into 2 parts.
        small_planner = MacroFloorplanner(max_block_area=20.0)
        clusters = small_planner.cluster_netlist(MOCK_NETLIST_DATA)

        iron_parts = [c for c in clusters if c.recipe == "iron-plate"]
        self.assertEqual(len(iron_parts), 2)
        self.assertEqual(iron_parts[0].sub_cluster_index, 0)
        self.assertEqual(iron_parts[1].sub_cluster_index, 1)
        self.assertEqual(iron_parts[0].total_sub_clusters, 2)
        self.assertEqual(len(iron_parts[0].instances), 1)
        self.assertEqual(len(iron_parts[1].instances), 1)

    def test_plan_grid_dimensions_and_channels(self) -> None:
        """Verify grid dimensioning and instantiation of street channels and crossroads."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        # 4 clusters -> 2x2 grid
        floorplan = self.floorplanner.plan_grid(clusters)
        self.assertEqual(floorplan.grid_w, 2)
        self.assertEqual(floorplan.grid_h, 2)

        # Verify internal channels
        self.assertIn("h_channel_0_0", floorplan.channels)
        self.assertIn("h_channel_1_0", floorplan.channels)
        self.assertIn("v_channel_0_0", floorplan.channels)
        self.assertIn("v_channel_0_1", floorplan.channels)

        # Verify 4-way crossroad intersection
        self.assertIn((1, 1), floorplan.intersections)
        ix = floorplan.intersections[(1, 1)]
        self.assertEqual(ix.center_x, 100.0)
        self.assertEqual(ix.center_y, 100.0)

    def test_inter_block_route_assignment(self) -> None:
        """Verify inter-block nets allocate lanes in street channels."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        floorplan = self.floorplanner.plan_grid(clusters, grid_w=2, grid_h=2)
        self.floorplanner.assign_inter_block_routes(floorplan, MOCK_NETLIST_DATA)

        # Verify inter-block routes exist
        self.assertIn("iron-plate", floorplan.routes)
        self.assertIn("copper-plate", floorplan.routes)
        self.assertIn("iron-gear-wheel", floorplan.routes)

        iron_route = floorplan.routes["iron-plate"]
        self.assertGreater(len(iron_route.segments), 0)
        # Net must have allocated lanes in traversed channels
        for seg in iron_route.segments:
            ch = floorplan.channels[seg.channel_id]
            lane = ch.get_lane(seg.lane_index)
            self.assertTrue(lane.is_allocated)
            self.assertEqual(lane.net_name, "iron-plate")

    def test_emit_channel_entities_collision_free(self) -> None:
        """Verify emitted channel entities have zero collisions and valid undergrounds."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        floorplan = self.floorplanner.plan_grid(clusters, grid_w=2, grid_h=2)
        self.floorplanner.assign_inter_block_routes(floorplan, MOCK_NETLIST_DATA)

        entities = floorplan.emit_channel_entities()
        self.assertGreater(len(entities), 0)

        # Check for overlapping collisions
        collisions = check_collisions(entities)
        self.assertEqual(collisions, [])

        # Check underground belt pairing
        unpaired = check_underground_pairing(entities)
        self.assertEqual(unpaired, [])

    def test_full_pipeline_with_automation_science_fixture(self) -> None:
        """Verify macro floorplanning with actual science factory netlist fixture."""
        fixture_path = Path("flow/automation_science_netlist.json")
        if not fixture_path.exists():
            self.skipTest("automation_science_netlist.json fixture not found")

        netlist_data = json.loads(fixture_path.read_text(encoding="utf-8"))

        # Plan with small block area (80 tiles) to force multi-block allocation
        planner = MacroFloorplanner(max_block_area=80.0)
        floorplan = planner.plan(netlist_data)

        self.assertGreaterEqual(floorplan.grid_w, 2)
        self.assertGreaterEqual(floorplan.grid_h, 2)

        # Every instance must map to a block coordinate
        for inst in netlist_data["instances"]:
            iname = inst["name"]
            self.assertIn(iname, floorplan.instance_to_block)
            bx, by = floorplan.instance_to_block[iname]
            self.assertGreaterEqual(bx, 0)
            self.assertLess(bx, floorplan.grid_w)
            self.assertGreaterEqual(by, 0)
            self.assertLess(by, floorplan.grid_h)

        # Emit channel entities and verify zero collisions
        entities = floorplan.emit_channel_entities()
        self.assertGreater(len(entities), 0)
        collisions = check_collisions(entities)
        self.assertEqual(collisions, [])

    def test_recursive_splitting_by_row_width(self) -> None:
        """Verify that oversized rows recursively split until each part fits within width."""
        # Iron plate in mock data has 2 instances of width 5 + pad 8 = 13 tiles each -> 26 tiles.
        # Set max_block_width to 15.0 so that 26 tiles must split recursively into 2 parts.
        planner = MacroFloorplanner(max_block_area=8836.0, max_block_width=15.0)
        clusters = planner.cluster_netlist(MOCK_NETLIST_DATA)

        iron_parts = [c for c in clusters if c.recipe == "iron-plate"]
        self.assertEqual(len(iron_parts), 2)
        for part in iron_parts:
            part_insts = [
                inst for inst in MOCK_NETLIST_DATA["instances"] if inst["name"] in part.instances
            ]
            part_w = planner._cluster_width(part_insts, MOCK_NETLIST_DATA["cells"])
            self.assertLessEqual(part_w, 15.0)

    def test_square_grid_with_empty_middle_block(self) -> None:
        """Verify square layout dimensioning and empty middle block placement."""
        # 8 distinct clusters should form a 3x3 square grid with center (1, 1) empty
        clusters = [
            RecipeCluster(
                name=f"cluster_{i}",
                recipe=f"item_{i}",
                instances=[f"u_inst_{i}"],
                total_area=10.0,
            )
            for i in range(8)
        ]
        floorplan = self.floorplanner.plan_grid(clusters)
        self.assertEqual(floorplan.grid_w, 3)
        self.assertEqual(floorplan.grid_h, 3)
        self.assertEqual(floorplan.empty_blocks, {(1, 1)})

        # Verify planner assigns all 8 clusters to outer perimeter cells
        for c in clusters:
            self.assertIsNotNone(c.assigned_block)
            bx, by = c.assigned_block
            self.assertIn((bx, by), floorplan.block_to_clusters)
            # Distance to border is 0 for perimeter cells
            self.assertEqual(min(bx, 2 - bx, by, 2 - by), 0)

        # Verify emitted floorplan TCL creates blockage over center block (1, 1) core
        tcl = floorplan.emit_floorplan_tcl()
        self.assertIn("create_blockage -region [list 103 103 197 197]", tcl)

        # Verify DEF blockages include center block core
        def_blockages = floorplan.emit_def_blockages(dbu=1000)
        self.assertIn("PLACEMENT RECT ( 103000 103000 ) ( 197000 197000 )", def_blockages)

    def test_single_item_type_per_cell(self) -> None:
        """Verify each cell is responsible for only one item type and no composites."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        floorplan = self.floorplanner.plan_grid(clusters)

        for (bx, by), blk_clusters in floorplan.block_to_clusters.items():
            recipes = {c.recipe for c in blk_clusters}
            self.assertEqual(
                len(recipes),
                1,
                f"Block ({bx}, {by}) contains multiple item types: {recipes}",
            )

    def test_auto_square_grid_for_22_clusters(self) -> None:
        """Verify automatic square-est grid calculation for 22 clusters."""
        clusters = [
            RecipeCluster(
                name=f"cluster_{i}",
                recipe=f"item_{i}",
                instances=[f"u_inst_{i}"],
                total_area=10.0,
            )
            for i in range(22)
        ]
        # Without specifying grid_w and grid_h
        floorplan = self.floorplanner.plan_grid(clusters)
        self.assertEqual(floorplan.grid_w, 5)
        self.assertEqual(floorplan.grid_h, 5)
        self.assertEqual(len(floorplan.empty_blocks), 3)

        # With grid_w=0 and grid_h=0
        floorplan_zero = self.floorplanner.plan_grid(clusters, grid_w=0, grid_h=0)
        self.assertEqual(floorplan_zero.grid_w, 5)
        self.assertEqual(floorplan_zero.grid_h, 5)
        self.assertEqual(len(floorplan_zero.empty_blocks), 3)

    def test_plan_hierarchical_nets_decomposition(self) -> None:
        """Verify hierarchical decomposition into 94x94 block specs and street highway specs."""
        from foundry.router.types import RouterNetSpec

        net_specs = [
            # Purely intra-block net inside (0, 0)
            RouterNetSpec(
                name="local_gear",
                source=(20, 30),
                sink=(50, 30),
                commodity="iron-gear-wheel",
                start_heading=DIRECTION_EAST,
                end_heading=DIRECTION_EAST,
            ),
            # Inter-block net from (0, 0) to (1, 0)
            RouterNetSpec(
                name="cross_plate",
                source=(80, 40),
                sink=(120, 40),
                commodity="iron-plate",
                start_heading=DIRECTION_EAST,
                end_heading=DIRECTION_EAST,
            ),
            # External input PIN at (0, 20) to block (1, 0)
            RouterNetSpec(
                name="ext_copper",
                source=(0, 20),
                sink=(115, 25),
                commodity="copper-ore",
                start_heading=DIRECTION_EAST,
                end_heading=DIRECTION_EAST,
            ),
            # Multi-terminal fluid net across PIN (0, 60) and two terminals in (1, 0)
            RouterNetSpec(
                name="water_mst_0",
                source=(130, 60),
                sink=(0, 60),
                commodity="water",
                is_fluid=True,
            ),
            RouterNetSpec(
                name="water_mst_1",
                source=(130, 75),
                sink=(130, 60),
                commodity="water",
                is_fluid=True,
            ),
        ]
        intra_specs, hwy_specs, no_hood_tiles, port_headings, floorplan = (
            self.floorplanner.plan_hierarchical_nets(
                net_specs=net_specs,
                grid_w=2,
                grid_h=1,
                obstacles=set(),
                min_x=0,
                min_y=0,
                max_x=200,
                max_y=100,
            )
        )
        # Intra-block net stays in (0, 0) without highway spec
        blk00_names = {s.name for s in intra_specs[(0, 0)]}
        self.assertIn("local_gear", blk00_names)
        self.assertIn("cross_plate_export_0_0", blk00_names)

        # Block (1, 0) receives import stubs and local fluid MST
        blk10_names = {s.name for s in intra_specs[(1, 0)]}
        self.assertIn("cross_plate_import_1_0", blk10_names)
        self.assertIn("ext_copper_import_1_0", blk10_names)
        self.assertIn("water_blk_1_0_mst_0", blk10_names)
        self.assertIn("water_blk_1_0_mst_1", blk10_names)

        # Highway receives only inter-block trunks (and only 1 water trunk for block (1, 0))
        hwy_names = {s.name for s in hwy_specs}
        self.assertEqual(hwy_names, {"cross_plate_hwy", "ext_copper_hwy", "water_hwy_mst_0"})
        self.assertGreaterEqual(len(no_hood_tiles), 4)
        self.assertGreaterEqual(len(port_headings), 6)
        self.assertEqual(len(floorplan.ports), 4)

    def test_no_opposite_wall_shortcut_and_switchbox_drc(self) -> None:
        """Verify ports across a shared street are forced onto distinct street segments and checked by DRC."""
        from foundry.belt_drc import BeltDrcChecker
        from foundry.macro_floorplan import BlockPort, InterBlockRoute
        from foundry.router.types import RouterNetSpec

        # Source at (90, 40) in (0, 0) and sink at (110, 40) in (1, 0) face each other across V street x in [97..102]
        net_specs = [
            RouterNetSpec(
                name="iron_plate_bridge",
                source=(90, 40),
                sink=(110, 40),
                commodity="iron-plate",
                start_heading=DIRECTION_EAST,
                end_heading=DIRECTION_EAST,
            )
        ]
        _intra, hwy_specs, _no_hood, _headings, floorplan = (
            self.floorplanner.plan_hierarchical_nets(
                net_specs=net_specs,
                grid_w=2,
                grid_h=2,
                obstacles=set(),
                min_x=0,
                min_y=0,
                max_x=200,
                max_y=200,
                bay_blocks={(0, 1), (1, 1)},
            )
        )
        self.assertIn("iron_plate_bridge", floorplan.routes)
        route = floorplan.routes["iron_plate_bridge"]
        assert route.source_port is not None
        self.assertEqual(len(route.target_ports), 1)
        src_seg = self.floorplanner._street_segment_of_edge(
            route.source_port.block[0], route.source_port.block[1], route.source_port.edge
        )
        dst_seg = self.floorplanner._street_segment_of_edge(
            route.target_ports[0].block[0],
            route.target_ports[0].block[1],
            route.target_ports[0].edge,
        )
        self.assertNotEqual(src_seg, dst_seg)

        # Verify BeltDrcChecker catches a synthetic opposite-wall shortcut and missing switchbox traversal
        drc = BeltDrcChecker()
        bad_src_port = BlockPort(
            name="bad_shortcut_export_0_0",
            block=(0, 0),
            net_name="bad_shortcut",
            port_type="export",
            edge="E",
            position=(97.0, 40.0),
            channel_id="v_channel_0_0",
            lane_index=0,
            direction=DIRECTION_EAST,
            internal_pos=(96.0, 40.0),
        )
        bad_dst_port = BlockPort(
            name="bad_shortcut_import_1_0",
            block=(1, 0),
            net_name="bad_shortcut",
            port_type="import",
            edge="W",
            position=(102.0, 40.0),
            channel_id="v_channel_0_0",
            lane_index=0,
            direction=DIRECTION_EAST,
            internal_pos=(103.0, 40.0),
        )
        bad_fp = self.floorplanner.plan_grid([], grid_w=2, grid_h=2)
        bad_fp.ports = [bad_src_port, bad_dst_port]
        bad_fp.routes["bad_shortcut"] = InterBlockRoute(
            net_name="bad_shortcut",
            source_block=(0, 0),
            target_blocks=[(1, 0)],
            source_port=bad_src_port,
            target_ports=[bad_dst_port],
        )

        class _MockRoute:
            path = [(97, 40), (98, 40), (99, 40), (100, 40), (101, 40), (102, 40)]

        violations = drc.check_chassis_matrix_discipline(
            entities=[],
            macro_floorplan=bad_fp,
            router_routes={"bad_shortcut_hwy": _MockRoute()},
            highway_specs=hwy_specs,
        )
        codes = {v.code for v in violations}
        self.assertIn("NO_OPPOSITE_WALL_SHORTCUT", codes)
        self.assertIn("SWITCHBOX_ROUTING_DISCIPLINE", codes)

    def test_self_contained_cell_trees_drc(self) -> None:
        """Verify CELL_SELF_CONTAINED_TREES flags splitters/mergers that cross cell boundaries."""
        from foundry.belt_drc import BeltDrcChecker

        drc = BeltDrcChecker()
        fp = self.floorplanner.plan_grid([], grid_w=2, grid_h=2)

        # Case 1: Splitter inside core (0, 0) driving a sink in core (1, 0) -> VIOLATION
        bad_def = {
            "units": 1000,
            "die_area": [0, 0, 200000, 200000],
            "components": [
                {"name": "u_split_1", "macro": "belt_split2", "x": 50000, "y": 50000},
                {"name": "u_mach_a", "macro": "assembling-machine-3", "x": 60000, "y": 50000},
                {"name": "u_mach_b", "macro": "assembling-machine-3", "x": 150000, "y": 50000},
            ],
            "nets": [
                {"name": "n1", "connections": [("u_split_1", "y1"), ("u_mach_a", "in")]},
                {"name": "n2", "connections": [("u_split_1", "y2"), ("u_mach_b", "in")]},
            ],
        }
        violations = drc.check_chassis_matrix_discipline(
            entities=[],
            macro_floorplan=fp,
            def_data=bad_def,
            bay_blocks={(0, 1), (1, 1)},
        )
        self.assertEqual([v.code for v in violations], ["CELL_SELF_CONTAINED_TREES"])

        # Case 2: Splitter moved to Matrix Switchbox Bay of (0, 1) at (50, 106) -> VALID
        good_def = {
            "units": 1000,
            "die_area": [0, 0, 200000, 200000],
            "components": [
                {"name": "u_split_1", "macro": "belt_split2", "x": 50000, "y": 106000},
                {"name": "u_mach_a", "macro": "assembling-machine-3", "x": 60000, "y": 50000},
                {"name": "u_mach_b", "macro": "assembling-machine-3", "x": 150000, "y": 50000},
            ],
            "nets": [
                {"name": "n1", "connections": [("u_split_1", "y1"), ("u_mach_a", "in")]},
                {"name": "n2", "connections": [("u_split_1", "y2"), ("u_mach_b", "in")]},
            ],
        }
        violations_good = drc.check_chassis_matrix_discipline(
            entities=[],
            macro_floorplan=fp,
            def_data=good_def,
            bay_blocks={(0, 1), (1, 1)},
        )
        self.assertEqual(violations_good, [])

    def test_programmatic_8_lane_chassis_and_drc(self) -> None:
        """Verify 8-lane programmatic chassis (92x92 core, 8x8 switchboxes) on a 2x2 grid."""
        from foundry.belt_drc import BeltDrcChecker
        from foundry.chassis import CityBlockChassis
        from foundry.router.types import RouterNetSpec

        chassis_8 = CityBlockChassis(pitch=100, channel_width=8, programmatic=True)
        self.assertEqual(chassis_8.core_width, 92)
        self.assertEqual(chassis_8.core_height, 92)
        self.assertEqual(chassis_8.core_margin_x, 4)
        self.assertEqual(chassis_8.core_margin_y, 4)
        bp_doc = chassis_8.emit_base_blueprint(2, 2)
        bp_ents = bp_doc["blueprint"]["entities"]
        radars = [e for e in bp_ents if e.get("name") == "radar"]
        poles = [e for e in bp_ents if e.get("name") == "big-electric-pole"]
        lamps = [e for e in bp_ents if e.get("name") == "small-lamp"]
        self.assertEqual(len(radars), 4)
        self.assertEqual(len(poles), 48)
        self.assertEqual(len(lamps), 96)

        fp_8 = MacroFloorplanner(chassis=chassis_8)
        net_specs = [
            RouterNetSpec(
                name="iron_plate_8lane",
                source=(80, 40),
                sink=(120, 40),
                commodity="iron-plate",
                start_heading=DIRECTION_EAST,
                end_heading=DIRECTION_EAST,
            )
        ]
        intra, hwy_specs, _no_hood, _headings, floorplan = fp_8.plan_hierarchical_nets(
            net_specs=net_specs,
            grid_w=2,
            grid_h=2,
            obstacles=set(),
            min_x=0,
            min_y=0,
            max_x=200,
            max_y=200,
        )
        self.assertEqual(len(floorplan.ports), 2)
        for p in floorplan.ports:
            cx, cy = p.core_pos
            hx, hy = p.channel_pos
            self.assertEqual(abs(cx - hx) + abs(cy - hy), 1)
            self.assertTrue((cx % 100) in (4, 95) or (cy % 100) in (4, 95))
            self.assertTrue((hx % 100) in (3, 96) or (hy % 100) in (3, 96))

        # Check core bbox uses margin=4 -> [4, 95]
        self.assertEqual(intra[(0, 0)][0].corridor_bbox, (4, 4, 95, 95))

        drc = BeltDrcChecker()
        violations = drc.check_chassis_matrix_discipline(
            entities=[],
            macro_floorplan=floorplan,
            highway_specs=hwy_specs,
        )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
