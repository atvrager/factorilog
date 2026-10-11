"""Unit tests for the DEF routing untangler package."""

from __future__ import annotations

import unittest
from typing import Any

from foundry.def_untangle import (
    SpatialWireIndex,
    eliminate_staircase_jogs,
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
        self.assertEqual(conns_a, [("u_merge", "a"), ("driver_b", "out")])
        self.assertEqual(conns_b, [("u_merge", "b"), ("driver_a", "out")])

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


if __name__ == "__main__":
    unittest.main()
