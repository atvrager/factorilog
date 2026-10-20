"""Unit tests for the native blueprint PNG renderer."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blueprint
import render_bp


def _make_sample_blueprint_string() -> str:
    """Create an encoded blueprint exchange string covering core entities, tiles, icons, and wires."""
    entities: list[dict[str, Any]] = [
        {
            "entity_number": 1,
            "name": "transport-belt",
            "position": {"x": 0.5, "y": 0.5},
            "direction": 4,
        },
        {
            "entity_number": 2,
            "name": "transport-belt",
            "position": {"x": 1.5, "y": 0.5},
            "direction": 8,
        },
        {
            "entity_number": 3,
            "name": "underground-belt",
            "type": "input",
            "position": {"x": 1.5, "y": 1.5},
            "direction": 8,
        },
        {
            "entity_number": 4,
            "name": "underground-belt",
            "type": "output",
            "position": {"x": 1.5, "y": 3.5},
            "direction": 8,
        },
        {
            "entity_number": 5,
            "name": "splitter",
            "position": {"x": 2.0, "y": 4.5},
            "direction": 8,
        },
        {
            "entity_number": 6,
            "name": "assembling-machine-2",
            "position": {"x": 5.5, "y": 2.5},
            "direction": 4,
            "recipe": "electronic-circuit",
        },
        {
            "entity_number": 7,
            "name": "fast-inserter",
            "position": {"x": 3.5, "y": 2.5},
            "direction": 12,
            "filters": [{"index": 1, "name": "iron-plate"}],
        },
        {
            "entity_number": 8,
            "name": "pipe",
            "position": {"x": 5.5, "y": 0.5},
        },
        {
            "entity_number": 9,
            "name": "pipe-to-ground",
            "position": {"x": 5.5, "y": -0.5},
            "direction": 8,
        },
        {
            "entity_number": 10,
            "name": "medium-electric-pole",
            "position": {"x": 3.5, "y": 0.5},
        },
        {
            "entity_number": 11,
            "name": "arithmetic-combinator",
            "position": {"x": 8.5, "y": 1.0},
            "direction": 0,
            "control_behavior": {
                "arithmetic_conditions": {
                    "operation": "+",
                    "output_signal": {"type": "item", "name": "electronic-circuit"},
                }
            },
        },
        {
            "entity_number": 12,
            "name": "decider-combinator",
            "position": {"x": 10.5, "y": 1.0},
            "direction": 4,
            "control_behavior": {
                "decider_conditions": {
                    "comparator": ">",
                    "output_signal": {"type": "virtual", "name": "signal-A"},
                }
            },
        },
        {
            "entity_number": 13,
            "name": "display-panel",
            "position": {"x": 12.5, "y": 0.5},
            "direction": 0,
            "icon": {"type": "item", "name": "iron-gear-wheel"},
        },
        {
            "entity_number": 14,
            "name": "power-switch",
            "position": {"x": 14.0, "y": 1.0},
        },
        {
            "entity_number": 15,
            "name": "steam-engine",
            "position": {"x": 18.5, "y": 2.5},
            "direction": 0,
        },
        {
            "entity_number": 16,
            "name": "cargo-landing-pad",
            "position": {"x": 26.0, "y": 4.0},
        },
    ]
    tiles: list[dict[str, Any]] = [
        {"name": "concrete", "position": {"x": 0, "y": 0}},
        {"name": "hazard-concrete-left", "position": {"x": 1, "y": 0}},
    ]
    wires: list[list[int]] = [
        [10, 1, 11, 1],
        [11, 4, 12, 2],
        [10, 5, 14, 5],
    ]
    return blueprint.encode(entities, label="render_test", wires=wires, tiles=tiles)


class TestRenderBp(unittest.TestCase):
    """Test suite for blueprint PNG rendering, sprite resolution, and CLI output."""

    def test_render_blueprint_from_string_and_file(self) -> None:
        """Verify rendering works from both a raw exchange string and a blueprint file."""
        bp_str = _make_sample_blueprint_string()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            out_png_1 = tmp_dir / "from_str.png"
            res_path_1 = render_bp.render_blueprint(bp_str, out_png_1)
            self.assertEqual(res_path_1, out_png_1.resolve())
            self.assertTrue(out_png_1.is_file())

            with Image.open(out_png_1) as img1:
                self.assertEqual(img1.format, "PNG")
                self.assertGreater(img1.width, 64)
                self.assertGreater(img1.height, 64)

            bp_file = tmp_dir / "sample.blueprint"
            bp_file.write_text(bp_str + "\n", encoding="utf-8")
            out_png_2 = tmp_dir / "from_file.png"
            res_path_2 = render_bp.render_blueprint(bp_file, out_png_2)
            self.assertEqual(res_path_2, out_png_2.resolve())
            self.assertTrue(out_png_2.is_file())

    def test_render_blueprint_fallback_without_game_assets(self) -> None:
        """Verify geometric fallback rendering succeeds when Factorio game assets are absent."""
        bp_str = _make_sample_blueprint_string()
        with tempfile.TemporaryDirectory() as tmp:
            out_png = Path(tmp) / "fallback.png"
            with mock.patch.object(render_bp, "_find_factorio_data_dir", return_value=None):
                render_bp.render_blueprint(bp_str, out_png)
            self.assertTrue(out_png.is_file())
            with Image.open(out_png) as img:
                self.assertEqual(img.mode, "RGBA")
                self.assertGreater(img.width, 64)

    def test_belt_curve_detection_and_tint_parsing(self) -> None:
        """Verify belt curve orientation and RGBA tint normalization."""
        entities = [
            {"name": "transport-belt", "position": {"x": 0.5, "y": 1.5}, "direction": 4},
            {"name": "transport-belt", "position": {"x": 1.5, "y": 1.5}, "direction": 0},
            {"name": "transport-belt", "position": {"x": 1.5, "y": 0.5}, "direction": 12},
        ]
        belt_grid = {
            coord: cell for ent in entities for coord, cell in render_bp._belt_grid_cells(ent)
        }
        self.assertEqual(render_bp._belt_curve_shape((0, 1), 4, belt_grid), "straight")
        self.assertEqual(render_bp._belt_curve_shape((1, 1), 0, belt_grid), "left")
        self.assertEqual(render_bp._belt_curve_shape((1, 0), 12, belt_grid), "left")

        self.assertEqual(
            render_bp._parse_tint({"r": 1.0, "g": 0.5, "b": 0.0, "a": 1.0}),
            (255, 128, 0, 255),
        )
        self.assertEqual(
            render_bp._parse_tint([100, 150, 200]),
            (100, 150, 200, 1),
        )
        self.assertIsNone(render_bp._parse_tint("invalid"))

    def test_main_cli_with_timing_json(self) -> None:
        """Verify CLI entry point renders PNG and writes timing metadata JSON."""
        bp_str = _make_sample_blueprint_string()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            bp_file = tmp_dir / "cli.blueprint"
            bp_file.write_text(bp_str, encoding="utf-8")
            out_png = tmp_dir / "cli.png"
            out_time = tmp_dir / "cli_time.json"

            argv = [
                "render_bp.py",
                str(bp_file),
                "-o",
                str(out_png),
                "--time-out",
                str(out_time),
            ]
            with mock.patch.object(sys, "argv", argv):
                rc = render_bp.main()

            self.assertEqual(rc, 0)
            self.assertTrue(out_png.is_file())
            self.assertTrue(out_time.is_file())
            time_data = json.loads(out_time.read_text(encoding="utf-8"))
            self.assertEqual(time_data["stage"], "render")
            self.assertGreaterEqual(time_data["elapsed_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
