"""Unit tests for the DEF routing untangler package."""

from __future__ import annotations

import unittest
from typing import Any

from foundry.def_untangle import (
    SpatialWireIndex,
    eliminate_staircase_jogs,
    route_unrouted_signal_nets,
    untangle_congested_turn_crossings,
    untangle_merger_inputs,
    untangle_splitter_outputs,
)
from foundry.untangle import (
    untangle_splitter_outputs as pkg_untangle_splitter_outputs,
)


class TestDefUntangle(unittest.TestCase):
    """Test suite for untangle functions and spatial indexing."""

    def test_facade_matches_package(self) -> None:
        """Verify the facade re-exports the exact functions from the untangle package."""
        self.assertIs(untangle_splitter_outputs, pkg_untangle_splitter_outputs)

    def test_spatial_wire_index_clearance(self) -> None:
        """Verify spatial wire index clearance checks."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {"name": "c1", "macro": "belt_split2", "x": 10000, "y": 10000, "orient": "N"}
            ],
            "nets": [
                {
                    "name": "net1",
                    "wires": [
                        {"layer": "M1", "x1": 5000, "y1": 5000, "x2": 8000, "y2": 5000},
                        {"layer": "M2", "x1": 8000, "y1": 5000, "x2": 8000, "y2": 8000},
                    ],
                }
            ],
        }
        idx = SpatialWireIndex(def_data, units=1000)

        # Clear vertical line far away
        self.assertTrue(idx.is_v_clear_staircase(2000, 1000, 9000))

        # Obstructed vertical line through the component (x=10000..11000, y=10000..12000)
        self.assertFalse(idx.is_v_clear_staircase(10500, 9000, 13000))

        # Obstructed vertical line collinear with net1's M2 wire (x=8000, y=5000..8000)
        self.assertFalse(idx.is_v_clear_staircase(8000, 4000, 9000))

        # Same line ignored if net1 is ignored
        self.assertTrue(idx.is_v_clear_staircase(8000, 4000, 9000, net_to_ignore=def_data["nets"][0]))

    def test_untangle_splitter_outputs_turn_crossing(self) -> None:
        """Verify untangling splitter outputs with downstream crossing turns."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {
                    "name": "u_split",
                    "macro": "belt_split2",
                    "x": 480000,
                    "y": 420000,
                    "orient": "N",
                }
            ],
            "nets": [
                {
                    "name": "net_y1",
                    "connections": [("u_split", "y1")],
                    "wires": [
                        {"layer": "M1", "x1": 480750, "y1": 420500, "x2": 481500, "y2": 420500},
                        {"layer": "M2", "x1": 481500, "y1": 420500, "x2": 481500, "y2": 421500},
                        {"layer": "M1", "x1": 481500, "y1": 421500, "x2": 479000, "y2": 421500},
                    ],
                },
                {
                    "name": "net_y2",
                    "connections": [("u_split", "y2")],
                    "wires": [
                        {"layer": "M1", "x1": 480750, "y1": 421500, "x2": 480500, "y2": 421500},
                        {"layer": "M2", "x1": 480500, "y1": 421500, "x2": 480500, "y2": 422500},
                        {"layer": "M1", "x1": 480500, "y1": 422500, "x2": 483000, "y2": 422500},
                    ],
                },
            ],
        }

        untangle_splitter_outputs(def_data)

        # Pin connections should be swapped
        conns_y1 = def_data["nets"][0]["connections"]
        conns_y2 = def_data["nets"][1]["connections"]
        self.assertEqual(conns_y1, [("u_split", "y2")])
        self.assertEqual(conns_y2, [("u_split", "y1")])

    def test_untangle_merger_inputs_crossing(self) -> None:
        """Verify untangling merger inputs whose approach tracks cross."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {
                    "name": "u_merge",
                    "macro": "belt_merge2",
                    "x": 10000,
                    "y": 10000,
                    "orient": "N",
                }
            ],
            "nets": [
                {
                    "name": "net_a",
                    "connections": [("u_merge", "a"), ("driver_a", "out")],
                    "wires": [
                        {"layer": "M1", "x1": 10250, "y1": 10500, "x2": 9500, "y2": 10500},
                        {"layer": "M2", "x1": 9500, "y1": 10500, "x2": 9500, "y2": 12000},
                    ],
                },
                {
                    "name": "net_b",
                    "connections": [("u_merge", "b"), ("driver_b", "out")],
                    "wires": [
                        {"layer": "M1", "x1": 10250, "y1": 11500, "x2": 9500, "y2": 11500},
                        {"layer": "M2", "x1": 9500, "y1": 11500, "x2": 9500, "y2": 9000},
                    ],
                },
            ],
        }

        untangle_merger_inputs(def_data)

        conns_a = def_data["nets"][0]["connections"]
        conns_b = def_data["nets"][1]["connections"]
        self.assertEqual(conns_a, [("u_merge", "b"), ("driver_a", "out")])
        self.assertEqual(conns_b, [("u_merge", "a"), ("driver_b", "out")])

    def test_untangle_merger_inputs_two_track_crossing(self) -> None:
        """Verify untangling two-track merger inputs approaching from the South."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {
                    "name": "u_merge",
                    "macro": "belt_merge2",
                    "x": 467000,
                    "y": 1030000,
                    "orient": "N",
                }
            ],
            "nets": [
                {
                    "name": "net_a",
                    "connections": [("u_merge", "a")],
                    "wires": [
                        {"layer": "M1", "x1": 367500, "y1": 1029500, "x2": 466500, "y2": 1029500},
                        {"layer": "M2", "x1": 466500, "y1": 1029500, "x2": 466500, "y2": 1030500},
                        {"layer": "M1", "x1": 466500, "y1": 1030500, "x2": 467250, "y2": 1030500},
                    ],
                },
                {
                    "name": "net_b",
                    "connections": [("u_merge", "b")],
                    "wires": [
                        {"layer": "M1", "x1": 388500, "y1": 1028500, "x2": 465500, "y2": 1028500},
                        {"layer": "M2", "x1": 465500, "y1": 1028500, "x2": 465500, "y2": 1031500},
                        {"layer": "M1", "x1": 465500, "y1": 1031500, "x2": 467250, "y2": 1031500},
                    ],
                },
            ],
        }

        untangle_merger_inputs(def_data)

        # net_a should now connect to pin b and use inner track x=465500
        conns_a = def_data["nets"][0]["connections"]
        conns_b = def_data["nets"][1]["connections"]
        self.assertEqual(conns_a, [("u_merge", "b")])
        self.assertEqual(conns_b, [("u_merge", "a")])
        # Verify net_a trunk at 465500 and approach ends at 465500
        m1_app_a = def_data["nets"][0]["wires"][0]
        m2_trunk_a = def_data["nets"][0]["wires"][1]
        self.assertEqual(m1_app_a["x2"], 465500)
        self.assertEqual(m2_trunk_a["x1"], 465500)
        # Verify net_b trunk at 466500 and approach ends at 466500
        m1_app_b = def_data["nets"][1]["wires"][0]
        m2_trunk_b = def_data["nets"][1]["wires"][1]
        self.assertEqual(m1_app_b["x2"], 466500)
        self.assertEqual(m2_trunk_b["x1"], 466500)

    def test_eliminate_staircase_jogs(self) -> None:
        """Verify eliminating an unblocked staircase jog."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [],
            "nets": [
                {
                    "name": "net_jog",
                    "wires": [
                        {"layer": "M1", "x1": 1000, "y1": 1000, "x2": 2000, "y2": 1000},
                        {"layer": "M2", "x1": 2000, "y1": 1000, "x2": 2000, "y2": 2000},
                        {"layer": "M1", "x1": 2000, "y1": 2000, "x2": 3000, "y2": 2000},
                        {"layer": "M2", "x1": 3000, "y1": 2000, "x2": 3000, "y2": 3000},
                    ],
                }
            ],
        }

        eliminate_staircase_jogs(def_data)
        # The 4 wire segments simplify to 2 collinear-merged segments
        wires = def_data["nets"][0]["wires"]
        self.assertEqual(len(wires), 2)


    def test_untangle_balancer_inputs_north_crossing(self) -> None:
        """Verify untangling balancer inputs approaching from the North."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {
                    "name": "u_bal",
                    "macro": "belt_balancer2",
                    "x": 404000,
                    "y": 60000,
                    "orient": "N",
                }
            ],
            "nets": [
                {
                    "name": "net_a",
                    "connections": [("u_bal", "a1")],
                    "wires": [
                        {"layer": "M1", "x1": 359500, "y1": 66500, "x2": 402500, "y2": 66500},
                        {"layer": "M2", "x1": 402500, "y1": 60500, "x2": 402500, "y2": 66500},
                        {"layer": "M1", "x1": 402500, "y1": 60500, "x2": 404250, "y2": 60500},
                    ],
                },
                {
                    "name": "net_b",
                    "connections": [("u_bal", "a2")],
                    "wires": [
                        {"layer": "M1", "x1": 350000, "y1": 72500, "x2": 400500, "y2": 72500},
                        {"layer": "M2", "x1": 400500, "y1": 61500, "x2": 400500, "y2": 72500},
                        {"layer": "M1", "x1": 400500, "y1": 61500, "x2": 404250, "y2": 61500},
                    ],
                },
            ],
        }

        untangle_merger_inputs(def_data)

        # net_a should now connect to a2 at y=61500, net_b to a1 at y=60500
        conns_a = def_data["nets"][0]["connections"]
        conns_b = def_data["nets"][1]["connections"]
        self.assertEqual(conns_a, [("u_bal", "a2")])
        self.assertEqual(conns_b, [("u_bal", "a1")])
        self.assertEqual(def_data["nets"][0]["wires"][1]["y1"], 61500)
        self.assertEqual(def_data["nets"][0]["wires"][2]["y1"], 61500)
        self.assertEqual(def_data["nets"][1]["wires"][1]["y1"], 60500)
        self.assertEqual(def_data["nets"][1]["wires"][2]["y1"], 60500)

    def test_copper_plate_congested_turn_crossing(self) -> None:
        """Verify shifting M2 jog at copper plate merger crossing."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {
                    "name": "u_merge_copper_plate_23",
                    "macro": "belt_merge2",
                    "x": 166000,
                    "y": 316000,
                    "orient": "N",
                }
            ],
            "nets": [
                {
                    "name": "copper_plate__u_copper_plate_26",
                    "wires": [
                        {"layer": "M1", "x1": 129500, "y1": 319500, "x2": 164500, "y2": 319500},
                        {"layer": "M2", "x1": 164500, "y1": 315500, "x2": 164500, "y2": 319500},
                        {"layer": "M1", "x1": 164500, "y1": 315500, "x2": 180500, "y2": 315500},
                    ],
                },
                {
                    "name": "copper_plate__u_copper_plate_30",
                    "wires": [
                        {"layer": "M1", "x1": 107750, "y1": 321500, "x2": 163500, "y2": 321500},
                        {"layer": "M2", "x1": 163500, "y1": 317500, "x2": 163500, "y2": 321500},
                        {"layer": "M1", "x1": 163500, "y1": 317500, "x2": 166250, "y2": 317500},
                    ],
                },
                {
                    "name": "copper_plate__u_copper_plate_27",
                    "wires": [
                        {"layer": "M1", "x1": 149750, "y1": 317500, "x2": 150500, "y2": 317500},
                        {"layer": "M2", "x1": 150500, "y1": 317500, "x2": 150500, "y2": 318500},
                        {"layer": "M1", "x1": 150500, "y1": 318500, "x2": 182500, "y2": 318500},
                        {"layer": "M2", "x1": 182500, "y1": 317500, "x2": 182500, "y2": 318500},
                        {"layer": "M1", "x1": 182500, "y1": 317500, "x2": 183250, "y2": 317500},
                    ],
                },
                {
                    "name": "copper_plate__u_copper_plate_29",
                    "wires": [
                        {"layer": "M1", "x1": 86750, "y1": 317500, "x2": 87500, "y2": 317500},
                        {"layer": "M2", "x1": 87500, "y1": 317500, "x2": 87500, "y2": 321500},
                        {"layer": "M1", "x1": 87500, "y1": 321500, "x2": 163500, "y2": 321500},
                        {"layer": "M2", "x1": 163500, "y1": 314500, "x2": 163500, "y2": 321500},
                        {"layer": "M1", "x1": 163500, "y1": 314500, "x2": 165500, "y2": 314500},
                        {"layer": "M2", "x1": 165500, "y1": 314500, "x2": 165500, "y2": 316500},
                        {"layer": "M1", "x1": 165500, "y1": 316500, "x2": 166250, "y2": 316500},
                    ],
                },
            ],
        }
        untangle_congested_turn_crossings(def_data)
        m2_wire = def_data["nets"][0]["wires"][1]
        self.assertNotEqual(m2_wire["x1"], 164500)

    def test_route_unrouted_signal_nets(self) -> None:
        """Verify routing unrouted point-to-point signal nets through open channels."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {
                    "name": "u_split_src",
                    "macro": "belt_split2",
                    "x": 1076000,
                    "y": 1542000,
                    "orient": "N",
                },
                {
                    "name": "u_split_dst",
                    "macro": "belt_split2",
                    "x": 1080000,
                    "y": 1644000,
                    "orient": "N",
                },
            ],
            "nets": [
                {
                    "name": "iron_plate__rf_31_2",
                    "connections": [("u_split_src", "y1"), ("u_split_dst", "a")],
                    "wires": [],
                }
            ],
        }
        routed = route_unrouted_signal_nets(def_data)
        self.assertEqual(routed, 1)

        wires = def_data["nets"][0]["wires"]
        self.assertEqual(len(wires), 3)
        # First segment: M1 from source pin y1 (x=1076750, y=1542500)
        self.assertEqual(wires[0]["layer"], "M1")
        self.assertEqual(wires[0]["x1"], 1076750)
        self.assertEqual(wires[0]["y1"], 1542500)
        # Intermediate segment: M2 vertical
        self.assertEqual(wires[1]["layer"], "M2")
        self.assertEqual(wires[1]["y1"], 1542500)
        self.assertEqual(wires[1]["y2"], 1644500)
        # Last segment: M1 to sink pin a (x=1080250, y=1644500)
        self.assertEqual(wires[2]["layer"], "M1")
        self.assertEqual(wires[2]["x2"], 1080250)
        self.assertEqual(wires[2]["y2"], 1644500)


if __name__ == "__main__":
    unittest.main()
