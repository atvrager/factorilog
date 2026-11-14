"""Hermetic verification tests for showcase artifacts and GitHub Pages output.

Validates generated WebP renders, deterministic SVGs, provenance JSON,
compact die geometry, blueprint downloads, `site/index.html`, and `README.md`.
"""

from __future__ import annotations

import json
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from foundry.circuit_synth import synthesize_power_sequencer
from tools.showcase_svg import build_verilog_campaign_svg

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = REPO_ROOT / "site"
ASSETS_DIR = SITE_DIR / "assets"
DATA_DIR = SITE_DIR / "data"
README_PATH = REPO_ROOT / "README.md"

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
        if not benchmarks_path.is_file():
            self.skipTest("Showcase site bundle not generated yet.")

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

        html_text = (SITE_DIR / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("/home/", html_text)
        self.assertNotIn("file://", html_text)
        for rel_path in REL_SITE_REF_RE.findall(html_text):
            self.assertTrue((SITE_DIR / rel_path).is_file(), f"Broken HTML link: {rel_path}")

        readme_text = README_PATH.read_text(encoding="utf-8")
        self.assertNotIn("/home/", readme_text)
        self.assertNotIn("file://", readme_text)
        for rel_path in README_ASSET_REF_RE.findall(readme_text):
            self.assertTrue((REPO_ROOT / rel_path).is_file(), f"Broken README link: {rel_path}")


if __name__ == "__main__":
    unittest.main()
