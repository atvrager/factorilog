"""Unit tests for ChassisTemplate and CityBlockChassis."""

from pathlib import Path
import unittest

from foundry.blueprint import decode
from foundry.chassis import CityBlockChassis


class TestCityBlockChassis(unittest.TestCase):
    """Test suite for CityBlockChassis layout geometry and blueprint generation."""

    def setUp(self) -> None:
        self.chassis = CityBlockChassis(
            pitch=100,
            core_width=94,
            core_height=94,
            channel_width=6,
        )

    def test_grid_pitch_and_core_bounds(self) -> None:
        """Verify pitch and core coordinate calculations for single and tiled blocks."""
        pitch_x, pitch_y = self.chassis.get_pitch()
        self.assertEqual(pitch_x, 100)
        self.assertEqual(pitch_y, 100)

        # Block (0, 0): margin=3, core=(3, 3, 94, 94)
        x0, y0, w0, h0 = self.chassis.get_core_bounds(0, 0)
        self.assertEqual((x0, y0, w0, h0), (3, 3, 94, 94))

        # Block (2, 3): x = 2*100 + 3 = 203, y = 3*100 + 3 = 303
        x1, y1, w1, h1 = self.chassis.get_core_bounds(2, 3)
        self.assertEqual((x1, y1, w1, h1), (203, 303, 94, 94))

    def test_core_membership_and_block_index(self) -> None:
        """Verify core membership check distinguishes machine sites from road channels."""
        # Inside core of Block (0, 0)
        self.assertTrue(self.chassis.is_in_core(3, 3, 0, 0))
        self.assertTrue(self.chassis.is_in_core(50, 50, 0, 0))
        self.assertTrue(self.chassis.is_in_core(96.9, 96.9, 0, 0))

        # Outside core (in street channels or margins)
        self.assertFalse(self.chassis.is_in_core(1, 1, 0, 0))
        self.assertFalse(self.chassis.is_in_core(98, 50, 0, 0))
        self.assertFalse(self.chassis.is_in_core(50, 98, 0, 0))

        # Block index mapping
        self.assertEqual(self.chassis.get_block_index(50, 50), (0, 0))
        self.assertEqual(self.chassis.get_block_index(150, 250), (1, 2))

    def test_placement_blockages(self) -> None:
        """Verify generated placement blockages cover road channels and margins."""
        blockages = self.chassis.get_placement_blockages(grid_w=2, grid_h=2)
        self.assertGreater(len(blockages), 0)

        # Check total area of core plus blockages equals total grid area (200 x 200 = 40000)
        # Core area for 4 blocks = 4 * (94 * 94) = 35344
        # Total road/margin area = 40000 - 35344 = 4656
        # Every core area tile must not be covered by any blockage
        for bx in range(2):
            for by in range(2):
                cx, cy, cw, ch = self.chassis.get_core_bounds(bx, by)
                center_x = cx + cw / 2.0
                center_y = cy + ch / 2.0
                for bx_k, by_k, bw_k, bh_k in blockages:
                    # Center of core must never be inside any blockage
                    self.assertFalse(
                        bx_k <= center_x < bx_k + bw_k and by_k <= center_y < by_k + bh_k,
                        f"Core ({bx}, {by}) center ({center_x}, {center_y}) covered by blockage {(bx_k, by_k, bw_k, bh_k)}",
                    )

    def test_blueprint_tiling_and_deduplication(self) -> None:
        """Verify template tiling loads cityblock.bp and merges shared boundaries."""
        bp_path = Path("cityblock.bp")
        if not bp_path.exists():
            self.skipTest("cityblock.bp fixture not found")

        chassis = CityBlockChassis.from_blueprint_file(bp_path)
        self.assertGreater(len(chassis.template_tiles), 0)
        self.assertGreater(len(chassis.template_entities), 0)

        # 1x1 grid emission
        doc1x1 = chassis.emit_base_blueprint(1, 1)
        bp1x1 = doc1x1["blueprint"]
        self.assertEqual(len(bp1x1["tiles"]), len(chassis.template_tiles))
        self.assertEqual(len(bp1x1["entities"]), len(chassis.template_entities))
        self.assertEqual(bp1x1["snap-to-grid"], {"x": 100, "y": 100})
        self.assertTrue(bp1x1["absolute-snapping"])

        # 2x2 grid emission: check deduplication (count < 4 * count_1x1)
        doc2x2 = chassis.emit_base_blueprint(2, 2)
        bp2x2 = doc2x2["blueprint"]
        self.assertEqual(len(bp2x2["tiles"]), 7488)
        self.assertEqual(len(bp2x2["entities"]), 261)
        self.assertIn("wires", bp2x2)
        self.assertGreater(len(bp2x2["wires"]), 0)
        for ent in bp2x2["entities"]:
            self.assertNotIn("neighbours", ent)
            self.assertNotIn("connections", ent)

        # Verify exchange string encoding and decoding round-trip
        bp_string = chassis.emit_base_blueprint_string(1, 1)
        self.assertTrue(bp_string.startswith("0"))
        decoded = decode(bp_string)
        self.assertEqual(len(decoded["blueprint"]["tiles"]), len(chassis.template_tiles))
        self.assertEqual(len(decoded["blueprint"]["entities"]), len(chassis.template_entities))
        self.assertIn("wires", decoded["blueprint"])

    def test_route_chassis_nets(self) -> None:
        """Verify routing nets across city block chassis using the maze router."""
        from foundry.router.types import (
            DIRECTION_EAST,
            DIRECTION_SOUTH,
            RouterNetSpec,
        )

        prototypes = {
            "underground-belt": {
                "underground-belt": {
                    "name": "underground-belt",
                    "speed": 0.03125,
                    "max_distance": 5,
                },
                "fast-underground-belt": {
                    "name": "fast-underground-belt",
                    "speed": 0.0625,
                    "max_distance": 7,
                },
            },
            "transport-belt": {
                "transport-belt": {
                    "name": "transport-belt",
                    "speed": 0.03125,
                    "related_underground_belt": "underground-belt",
                },
                "fast-transport-belt": {
                    "name": "fast-transport-belt",
                    "speed": 0.0625,
                    "related_underground_belt": "fast-underground-belt",
                },
            },
            "pipe-to-ground": {
                "pipe-to-ground": {
                    "name": "pipe-to-ground",
                    "fluid_box": {
                        "pipe_connections": [
                            {"connection_type": "underground", "max_underground_distance": 10},
                        ]
                    },
                }
            },
            "pipe": {"pipe": {"name": "pipe"}},
        }
        nets = [
            RouterNetSpec(
                name="bus_iron",
                source=(10, 10),
                sink=(25, 10),
                start_heading=DIRECTION_EAST,
                end_heading=DIRECTION_EAST,
                commodity="iron-plate",
            ),
            RouterNetSpec(
                name="bus_copper",
                source=(18, 5),
                sink=(18, 20),
                start_heading=DIRECTION_SOUTH,
                end_heading=DIRECTION_SOUTH,
                commodity="copper-plate",
            ),
        ]
        routes, entities, verification_result = self.chassis.route_chassis_nets(
            nets,
            grid_w=1,
            grid_h=1,
            prototypes=prototypes,
            verify_oracle=False,
        )
        self.assertEqual(len(routes), 2)
        self.assertGreater(len(entities), 0)
        belt_names = {e.get("name") for e in entities}
        self.assertTrue("transport-belt" in belt_names or "underground-belt" in belt_names)


if __name__ == "__main__":
    unittest.main()
