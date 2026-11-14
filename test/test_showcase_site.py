"""Hermetic verification tests for showcase artifacts and GitHub Pages output.

Validates generated WebP renders, deterministic SVGs, provenance JSON,
compact die geometry, blueprint downloads, `site/index.html`, and `README.md`.
"""

from __future__ import annotations

import json
import math
import re
import tarfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from foundry.circuit_synth import synthesize_power_sequencer
from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
)
from tools.showcase_svg import (
    PALETTE,
    ROUTER_VP_H,
    ROUTER_VP_W,
    ROUTER_VP_X,
    ROUTER_VP_Y,
    build_router_3d_svg,
    build_verilog_campaign_svg,
)
from tools.showcase_template import render_showcase_html

RUNFILES_ROOT = Path(__file__).parent.parent
SITE_DIR = RUNFILES_ROOT / "site"
ASSETS_DIR = SITE_DIR / "assets"
DATA_DIR = SITE_DIR / "data"
README_PATH = RUNFILES_ROOT / "README.md"

EXPECTED_WEBPS: tuple[str, ...] = (
    "science_chassis.webp",
    "science_chassis_crop.webp",
    "automation_controlled_science.webp",
    "starter_base.webp",
)
EXPECTED_SVGS: tuple[str, ...] = (
    "pipeline_lp_dag.svg",
    "pipeline_placement.svg",
    "pipeline_router_3d.svg",
    "pipeline_verilog_campaign.svg",
)
EXPECTED_BLUEPRINTS: tuple[str, ...] = (
    "science_chassis.blueprint",
    "automation_controlled_science.blueprint",
    "automation_logistic_chassis.blueprint",
    "starter_base.blueprint",
)
EXPECTED_STAGES: tuple[str, ...] = ("synth", "pnr", "bp", "render", "save")
MIN_IMAGE_DIM = 400
MAX_WEBP_BYTES = 4_000_000
MIN_CHASSIS_MACROS = 3000
CHASSIS_DIE_DIM = 1200
REL_SITE_REF_RE = re.compile(
    r'(?:src|href)=["\']((?:assets|data)/[^"\']+|styles\.css|app\.js)["\']'
)
README_ASSET_REF_RE = re.compile(r"\((site/assets/[^)\s]+)\)")
SHORT_HEX_RE = re.compile(r"^[0-9a-f]{7,12}$")
FULL_HEX_RE = re.compile(r"^[0-9a-f]{40}$")
COORD_PAIR_RE = re.compile(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
VP_MIN_LEN = 1.0
BOUND_EPS = 0.5
VP_EPS = 0.2
RIGHT_STACK_X = 644.0


class ShowcaseSiteTest(unittest.TestCase):
    """Test suite verifying generated visuals, site links, and benchmarks."""

    def test_verilog_campaign_svg_generator(self) -> None:
        """Verify deterministic Verilog SVG rendering stays pure and valid XML."""
        synth_res = synthesize_power_sequencer()
        svg_text = build_verilog_campaign_svg(synth_res, "module power_sequencer(); endmodule")
        root = ET.fromstring(svg_text)
        self.assertTrue(root.tag.endswith("svg"))
        tags = {elem.tag.split("}")[-1].lower() for elem in root.iter()}
        self.assertNotIn("script", tags)
        self.assertNotIn("foreignobject", tags)

    def test_generated_site_bundle(self) -> None:
        """Verify committed WebP renders, SVGs, JSON provenance, HTML, and README links."""
        benchmarks_path = DATA_DIR / "benchmarks.json"
        self.assertTrue(benchmarks_path.is_file(), f"Missing benchmarks JSON: {benchmarks_path}")

        for webp_name in EXPECTED_WEBPS:
            webp_path = ASSETS_DIR / webp_name
            self.assertTrue(webp_path.is_file(), f"Missing WebP render: {webp_path}")
            self.assertLess(webp_path.stat().st_size, MAX_WEBP_BYTES)
            with Image.open(webp_path) as img:
                self.assertGreaterEqual(img.width, MIN_IMAGE_DIM)
                self.assertGreaterEqual(img.height, MIN_IMAGE_DIM)

        for svg_name in EXPECTED_SVGS:
            svg_path = ASSETS_DIR / svg_name
            self.assertTrue(svg_path.is_file(), f"Missing SVG diagram: {svg_path}")
            svg_text = svg_path.read_text(encoding="utf-8")
            root = ET.fromstring(svg_text)
            self.assertTrue(root.tag.endswith("svg"))
            tags = {elem.tag.split("}")[-1].lower() for elem in root.iter()}
            self.assertNotIn("script", tags)
            self.assertNotIn("foreignobject", tags)

        for bp_name in EXPECTED_BLUEPRINTS:
            bp_path = DATA_DIR / "blueprints" / bp_name
            self.assertTrue(bp_path.is_file(), f"Missing blueprint file: {bp_path}")
            self.assertTrue(bp_path.read_text(encoding="utf-8").strip().startswith("0"))

        benchmarks = json.loads(benchmarks_path.read_text(encoding="utf-8"))
        prov = benchmarks["provenance"]
        self.assertRegex(prov["git_commit_short"], SHORT_HEX_RE)
        self.assertRegex(prov["git_commit_full"], FULL_HEX_RE)
        self.assertTrue(prov["cpu_model"])
        self.assertGreaterEqual(int(prov["physical_cores"]), 1)
        self.assertGreaterEqual(int(prov["logical_threads"]), 1)
        for stage in EXPECTED_STAGES:
            self.assertTrue(prov["stage_concurrency"].get(stage))

        targets = {row["target"]: row for row in benchmarks["targets"]}
        self.assertIn("science_chassis", targets)
        chassis_row = targets["science_chassis"]
        self.assertEqual(chassis_row["drc_violations"], 0)
        self.assertGreaterEqual(chassis_row["macro_instances"], MIN_CHASSIS_MACROS)

        die_path = DATA_DIR / "die_science_chassis.json"
        self.assertTrue(die_path.is_file())
        die_doc = json.loads(die_path.read_text(encoding="utf-8"))
        self.assertEqual(die_doc["die"]["width"], CHASSIS_DIE_DIM)
        self.assertEqual(len(die_doc["macros"]), chassis_row["macro_instances"])
        self.assertTrue(die_doc["belts"])
        self.assertTrue(die_doc["underground"])
        self.assertTrue(die_doc["pipes"])
        self.assertTrue(die_doc["copper_wires"])

        html_path = SITE_DIR / "index.html"
        html_text = html_path.read_text(encoding="utf-8")
        self.assertEqual(html_text, render_showcase_html(benchmarks))
        self.assertNotIn("/home/", html_text)
        self.assertNotIn("file://", html_text)
        for rel_path in REL_SITE_REF_RE.findall(html_text):
            self.assertTrue((SITE_DIR / rel_path).is_file(), f"Broken HTML link: {rel_path}")

        verilog_src = (RUNFILES_ROOT / "flow" / "controllers" / "power_sequencer.v").read_text(
            encoding="utf-8"
        )
        expected_verilog_svg = build_verilog_campaign_svg(synthesize_power_sequencer(), verilog_src)
        self.assertEqual(
            (ASSETS_DIR / "pipeline_verilog_campaign.svg").read_text(encoding="utf-8"),
            expected_verilog_svg,
        )

        bundle_path = SITE_DIR / "pages_bundle.tar.gz"
        self.assertTrue(bundle_path.is_file(), f"Missing packaged site bundle: {bundle_path}")
        expected_members = {
            ".nojekyll",
            "index.html",
            "styles.css",
            "app.js",
            "data/benchmarks.json",
            "data/die_science_chassis.json",
            *(f"assets/{name}" for name in EXPECTED_WEBPS),
            *(f"assets/{name}" for name in EXPECTED_SVGS),
            *(f"data/blueprints/{name}" for name in EXPECTED_BLUEPRINTS),
        }
        with tarfile.open(bundle_path, "r:gz") as tf:
            member_names = set(tf.getnames())
            missing = expected_members - member_names
            self.assertEqual(missing, set(), f"Missing pages_bundle members: {sorted(missing)}")
            index_member = tf.extractfile("index.html")
            self.assertIsNotNone(index_member)
            assert index_member is not None
            self.assertEqual(index_member.read().decode("utf-8"), html_text)

        readme_text = README_PATH.read_text(encoding="utf-8")
        self.assertNotIn("/home/", readme_text)
        self.assertNotIn("file://", readme_text)
        for rel_path in README_ASSET_REF_RE.findall(readme_text):
            self.assertTrue((RUNFILES_ROOT / rel_path).is_file(), f"Broken README link: {rel_path}")

    def test_svg_coordinate_bounds(self) -> None:
        """Verify all SVG elements stay inside viewBox and router viewport bounds."""
        for svg_name in EXPECTED_SVGS:
            root = ET.fromstring((ASSETS_DIR / svg_name).read_text(encoding="utf-8"))
            vb_parts = [float(val) for val in root.attrib["viewBox"].split()]
            vb_w, vb_h = vb_parts[2], vb_parts[3]
            self._assert_tree_within_bounds(root, vb_w, vb_h, svg_name)

        router_root = ET.fromstring(
            (ASSETS_DIR / "pipeline_router_3d.svg").read_text(encoding="utf-8")
        )
        vp_group = next(
            (elem for elem in router_root.iter() if elem.attrib.get("id") == "router-viewport"),
            None,
        )
        self.assertIsNotNone(vp_group)
        assert vp_group is not None
        self.assertEqual(vp_group.attrib.get("clip-path"), "url(#router-vp-clip)")
        self._assert_router_viewport_contents(vp_group, require_all_layers=True)

        for elem in router_root.iter():
            if not elem.tag.endswith("text"):
                continue
            if float(elem.attrib.get("x", "0")) >= RIGHT_STACK_X:
                text_val = elem.text or ""
                self.assertFalse(text_val.endswith(".."), f"Truncated text in SVG: {text_val}")

    def test_router_svg_clips_escaping_segments(self) -> None:
        """Verify router SVG clips overflowing runs and extends 1-tile belts cleanly."""
        cat_map = {
            "assembling-machine-2": "assembling-machine",
            "transport-belt": "transport-belt",
            "underground-belt": "underground-belt",
            "express-underground-belt": "underground-belt",
            "pipe": "pipe",
        }
        entities: list[dict[str, object]] = [
            {"name": "assembling-machine-2", "position": {"x": 90.0, "y": 20.0}, "direction": 0},
            {"name": "transport-belt", "position": {"x": 94.5, "y": 16.5}, "direction": 0},
            {
                "name": "underground-belt",
                "type": "input",
                "position": {"x": 121.5, "y": 14.5},
                "direction": DIRECTION_EAST,
            },
            {
                "name": "underground-belt",
                "type": "output",
                "position": {"x": 126.5, "y": 14.5},
                "direction": DIRECTION_EAST,
            },
            {
                "name": "express-underground-belt",
                "type": "input",
                "position": {"x": 88.5, "y": 11.5},
                "direction": DIRECTION_NORTH,
            },
            {
                "name": "express-underground-belt",
                "type": "output",
                "position": {"x": 88.5, "y": 4.5},
                "direction": DIRECTION_NORTH,
            },
        ]
        for tx in range(0, 180):
            entities.append(
                {
                    "name": "transport-belt",
                    "position": {"x": tx + 0.5, "y": 26.5},
                    "direction": DIRECTION_EAST,
                }
            )
            entities.append(
                {
                    "name": "pipe",
                    "position": {"x": tx + 0.5, "y": 32.5},
                    "direction": DIRECTION_NORTH,
                }
            )
        for ty in range(-20, 120):
            entities.append(
                {
                    "name": "transport-belt",
                    "position": {"x": 102.5, "y": ty + 0.5},
                    "direction": DIRECTION_SOUTH,
                }
            )

        def_data = {
            "units": 1000,
            "blockages": [{"x1": 97000, "y1": 0, "x2": 103000, "y2": 120000}],
        }
        bp_payload = {"entities": entities}
        svg_text = build_router_3d_svg(
            def_data,
            bp_payload,
            cat_map,
            {"underground-belt": 5, "express-underground-belt": 9, "pipe-to-ground": 10},
        )
        root = ET.fromstring(svg_text)
        vp_group = next(
            (elem for elem in root.iter() if elem.attrib.get("id") == "router-viewport"),
            None,
        )
        self.assertIsNotNone(vp_group)
        assert vp_group is not None
        self._assert_router_viewport_contents(vp_group, require_all_layers=True)

        north_1tile = [
            elem
            for elem in vp_group.iter()
            if elem.tag.endswith("line")
            and elem.attrib.get("stroke") == PALETTE["amber"]
            and elem.attrib.get("x1") == elem.attrib.get("x2")
            and elem.attrib.get("marker-end") == "url(#arr-belt)"
        ]
        self.assertTrue(any(float(e.attrib["y2"]) < float(e.attrib["y1"]) for e in north_1tile))

    def _assert_tree_within_bounds(
        self,
        elem: ET.Element,
        vb_w: float,
        vb_h: float,
        label: str,
    ) -> None:
        """Check recursively that SVG elements outside `<marker>` stay in `viewBox`."""
        tag = elem.tag.split("}")[-1].lower()
        if tag == "marker":
            return
        for attr in ("x", "x1", "x2", "cx"):
            val = elem.attrib.get(attr)
            if val is not None:
                fval = float(val)
                self.assertGreaterEqual(fval, 0.0, f"{label} {tag} {attr}={fval}")
                self.assertLessEqual(fval, vb_w, f"{label} {tag} {attr}={fval}")
        for attr in ("y", "y1", "y2", "cy"):
            val = elem.attrib.get(attr)
            if val is not None:
                fval = float(val)
                self.assertGreaterEqual(fval, 0.0, f"{label} {tag} {attr}={fval}")
                self.assertLessEqual(fval, vb_h, f"{label} {tag} {attr}={fval}")
        if tag == "rect" and "width" in elem.attrib and "height" in elem.attrib:
            w_str, h_str = elem.attrib["width"], elem.attrib["height"]
            if not w_str.endswith("%") and not h_str.endswith("%"):
                x0 = float(elem.attrib.get("x", "0"))
                y0 = float(elem.attrib.get("y", "0"))
                self.assertLessEqual(x0 + float(w_str), vb_w + BOUND_EPS, label)
                self.assertLessEqual(y0 + float(h_str), vb_h + BOUND_EPS, label)
        if tag == "circle":
            cx = float(elem.attrib["cx"])
            cy = float(elem.attrib["cy"])
            rad = float(elem.attrib["r"])
            self.assertGreaterEqual(cx - rad, -BOUND_EPS, label)
            self.assertLessEqual(cx + rad, vb_w + BOUND_EPS, label)
            self.assertGreaterEqual(cy - rad, -BOUND_EPS, label)
            self.assertLessEqual(cy + rad, vb_h + BOUND_EPS, label)
        for attr in ("points", "d"):
            raw = elem.attrib.get(attr)
            if raw:
                for sx, sy in COORD_PAIR_RE.findall(raw):
                    self.assertGreaterEqual(float(sx), 0.0, label)
                    self.assertLessEqual(float(sx), vb_w, label)
                    self.assertGreaterEqual(float(sy), 0.0, label)
                    self.assertLessEqual(float(sy), vb_h, label)
        for child in elem:
            self._assert_tree_within_bounds(child, vb_w, vb_h, label)

    def _assert_router_viewport_contents(
        self,
        vp_group: ET.Element,
        *,
        require_all_layers: bool,
    ) -> None:
        """Check that all geometry inside `router-viewport` stays strictly inside box."""
        vx0 = ROUTER_VP_X - VP_EPS
        vy0 = ROUTER_VP_Y - VP_EPS
        vx1 = ROUTER_VP_X + ROUTER_VP_W + VP_EPS
        vy1 = ROUTER_VP_Y + ROUTER_VP_H + VP_EPS
        machines = 0
        belts = 0
        underground = 0
        pipes = 0
        for elem in vp_group.iter():
            tag = elem.tag.split("}")[-1].lower()
            if tag == "line":
                x1, y1 = float(elem.attrib["x1"]), float(elem.attrib["y1"])
                x2, y2 = float(elem.attrib["x2"]), float(elem.attrib["y2"])
                self.assertGreaterEqual(x1, vx0)
                self.assertLessEqual(x1, vx1)
                self.assertGreaterEqual(x2, vx0)
                self.assertLessEqual(x2, vx1)
                self.assertGreaterEqual(y1, vy0)
                self.assertLessEqual(y1, vy1)
                self.assertGreaterEqual(y2, vy0)
                self.assertLessEqual(y2, vy1)
                self.assertGreaterEqual(math.hypot(x2 - x1, y2 - y1), VP_MIN_LEN)
                stroke = elem.attrib.get("stroke")
                if stroke == PALETTE["amber"]:
                    belts += 1
                elif stroke == PALETTE["cyan"]:
                    underground += 1
                elif stroke == PALETTE["violet"]:
                    pipes += 1
            elif tag == "rect":
                x = float(elem.attrib["x"])
                y = float(elem.attrib["y"])
                w = float(elem.attrib["width"])
                h = float(elem.attrib["height"])
                self.assertGreaterEqual(x, vx0)
                self.assertGreaterEqual(y, vy0)
                self.assertLessEqual(x + w, vx1)
                self.assertLessEqual(y + h, vy1)
                if elem.attrib.get("stroke") == PALETTE["teal"]:
                    machines += 1
            elif tag == "circle":
                cx = float(elem.attrib["cx"])
                cy = float(elem.attrib["cy"])
                rad = float(elem.attrib["r"])
                self.assertGreaterEqual(cx - rad, vx0)
                self.assertLessEqual(cx + rad, vx1)
                self.assertGreaterEqual(cy - rad, vy0)
                self.assertLessEqual(cy + rad, vy1)
        if require_all_layers:
            self.assertGreaterEqual(machines, 1)
            self.assertGreaterEqual(belts, 1)
            self.assertGreaterEqual(underground, 1)
            self.assertGreaterEqual(pipes, 1)


if __name__ == "__main__":
    unittest.main()
