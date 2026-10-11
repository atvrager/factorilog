"""Tests for the DEF inspection tool."""

import unittest

from tools.inspect_def import find_components_at, find_wires_at


class TestInspectDef(unittest.TestCase):
    """Test suite for inspect_def functions."""

    def setUp(self) -> None:
        self.sample_components = [
            {"name": "u_mach_1", "macro": "mach", "x": 10000, "y": 20000},
            {"name": "u_mach_2", "macro": "mach", "x": 30000, "y": 40000},
        ]
        self.sample_nets = [
            {
                "name": "net_a",
                "connections": [("u_mach_1", "out"), ("u_mach_2", "in")],
                "wires": [
                    {"layer": "M1", "x1": 10000, "y1": 20000, "x2": 20000, "y2": 20000},
                    {"layer": "M2", "x1": 20000, "y1": 20000, "x2": 20000, "y2": 40000},
                ],
            }
        ]

    def test_find_components_at(self) -> None:
        found = find_components_at(self.sample_components, 10.0, 20.0, 0.001)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "u_mach_1")

    def test_find_wires_at(self) -> None:
        found = find_wires_at(self.sample_nets, 15.0, 20.0, 0.001)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["net"], "net_a")
        self.assertEqual(found[0]["layer"], "M1")

        found_v = find_wires_at(self.sample_nets, 20.0, 30.0, 0.001)
        self.assertEqual(len(found_v), 1)
        self.assertEqual(found_v[0]["layer"], "M2")

    def test_unrouted_nets(self) -> None:
        nets = [
            {"name": "n1", "wires": [{"layer": "M1"}]},
            {"name": "n2", "wires": []},
        ]
        unrouted = [n["name"] for n in nets if not n.get("wires")]
        self.assertEqual(unrouted, ["n2"])


if __name__ == "__main__":
    unittest.main()
