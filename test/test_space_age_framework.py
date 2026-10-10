"""Unit tests for Space Age framework and data-driven prototype mechanics."""

from __future__ import annotations

import unittest

from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    entity_bounding_box,
    register_prototype_boxes,
)
from foundry.lef import get_macro_port_geometry
from foundry.netlist import assign_gaa_pin_faces


class TestSpaceAgeFramework(unittest.TestCase):
    """Test suite for data-driven prototype parameters and GAA pin assignment."""

    def test_dynamic_bounding_box_extraction(self) -> None:
        """Verify bounding box calculation for standard and synthetic expansion machines."""
        small_proto = {"collision_box": [[-0.4, -0.4], [0.4, 0.4]]}
        self.assertEqual(entity_bounding_box(small_proto), (1.0, 1.0))

        standard_proto = {"collision_box": [[-1.2, -1.2], [1.2, 1.2]]}
        self.assertEqual(entity_bounding_box(standard_proto), (3.0, 3.0))

        large_proto = {"collision_box": [[-2.3, -2.3], [2.3, 2.3]]}
        self.assertEqual(entity_bounding_box(large_proto), (5.0, 5.0))

        rectangular_proto = {"collision_box": [[-3.2, -1.7], [3.2, 1.7]]}
        self.assertEqual(entity_bounding_box(rectangular_proto), (7.0, 4.0))

    def test_register_prototype_boxes(self) -> None:
        """Verify dynamic registration of synthetic prototypes in entity helpers."""
        prototypes = {
            "assembling-machine": {
                "synthetic-foundry": {
                    "name": "synthetic-foundry",
                    "collision_box": [[-2.4, -2.4], [2.4, 2.4]],
                },
            },
        }
        register_prototype_boxes(prototypes)
        from foundry.entity_helpers import entity_size

        size = entity_size("synthetic-foundry")
        self.assertEqual(size, (5.0, 5.0))

    def test_gaa_face_assignment_multi_ingredient(self) -> None:
        """Verify that multi-ingredient recipes distribute across machine faces."""
        inputs = ["item_a", "item_b", "item_c", "item_d"]
        depths = {"item_a": 1, "item_b": 2, "item_c": 3, "item_d": 4}

        faces = assign_gaa_pin_faces(inputs, depths=depths)

        self.assertIn("W", faces.values())
        self.assertIn("N", faces.values())
        self.assertIn("S", faces.values())
        self.assertEqual(len(faces), 4)

    def test_macro_port_geometry_gaa(self) -> None:
        """Verify macro port geometry, inserter positions, and approach tiles."""
        cell = {
            "cell": "synthetic-large-machine",
            "machine": "synthetic-foundry",
            "width": 7,
            "height": 7,
            "machine_width": 5,
            "machine_height": 5,
            "inputs": ["item_w", "item_n", "item_s"],
            "outputs": ["item_out"],
            "port_faces": {
                "item_w": "W",
                "item_n": "N",
                "item_s": "S",
                "item_out": "E",
            },
        }

        w_geom = get_macro_port_geometry(cell, "item_w", is_output=False)
        self.assertEqual(w_geom["face"], "W")
        self.assertEqual(w_geom["inserter_dir"], DIRECTION_WEST)
        self.assertEqual(w_geom["approach_dir"], DIRECTION_EAST)
        self.assertEqual(w_geom["inserter_pos"], (0.5, 3.5))
        self.assertEqual(w_geom["approach_pos"], (-0.5, 3.5))

        n_geom = get_macro_port_geometry(cell, "item_n", is_output=False)
        self.assertEqual(n_geom["face"], "N")
        self.assertEqual(n_geom["inserter_dir"], DIRECTION_SOUTH)
        self.assertEqual(n_geom["approach_dir"], DIRECTION_NORTH)
        self.assertEqual(n_geom["inserter_pos"], (3.5, 6.5))
        self.assertEqual(n_geom["approach_pos"], (3.5, 7.5))

        s_geom = get_macro_port_geometry(cell, "item_s", is_output=False)
        self.assertEqual(s_geom["face"], "S")
        self.assertEqual(s_geom["inserter_dir"], DIRECTION_NORTH)
        self.assertEqual(s_geom["approach_dir"], DIRECTION_SOUTH)
        self.assertEqual(s_geom["inserter_pos"], (3.5, 0.5))
        self.assertEqual(s_geom["approach_pos"], (3.5, -0.5))

        e_geom = get_macro_port_geometry(cell, "item_out", is_output=True)
        self.assertEqual(e_geom["face"], "E")
        self.assertEqual(e_geom["inserter_dir"], DIRECTION_WEST)
        self.assertEqual(e_geom["approach_dir"], DIRECTION_EAST)
        self.assertEqual(e_geom["inserter_pos"], (6.5, 3.5))
        self.assertEqual(e_geom["approach_pos"], (7.5, 3.5))


if __name__ == "__main__":
    unittest.main()
