"""End-to-end test case for factory blueprint generation and Factorio validation.

Generates the netlist, runs OpenROAD physical placement and routing,
translates DEF to a blueprint, and validates placement in Factorio.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def find_repo_root() -> Path:
    """Find workspace repository root directory."""
    ws = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if ws and Path(ws).is_dir():
        return Path(ws)
    cur = Path(__file__).resolve()
    for parent in [cur] + list(cur.parents):
        if (parent / "MODULE.bazel").is_file():
            return parent
    return Path.cwd()


REPO_ROOT = find_repo_root()
sys.path.insert(0, str(REPO_ROOT / "foundry"))
sys.path.insert(0, str(REPO_ROOT / "flow"))

import blueprint
import check_blueprint
import def2bp
import netbuild

OPENROAD_IMAGE = "docker.io/openroad/orfs:latest"


class TestEndToEndFactory(unittest.TestCase):
    """Verify that Factorio engine attests to 100 percent entity placement."""

    def test_end_to_end_factory_placement(self) -> None:
        """Run full synthesis, place-and-route, bridge, and Factorio engine check."""
        # Verify Factorio installation
        try:
            game_bin = check_blueprint.find_factorio_binary()
        except FileNotFoundError:
            self.skipTest("Factorio binary not found on system.")

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="factorilog-e2e-") as tmp:
            work_dir = Path(tmp)

            # Check if the caller supplied a prebuilt blueprint file
            bp_path = None
            if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
                candidate = Path(sys.argv[1])
                if candidate.is_file():
                    bp_path = candidate

            if bp_path is not None:
                bp_string = bp_path.read_text().strip()
            else:
                # Verify podman command
                if not shutil.which("podman"):
                    self.skipTest("podman command not found on system.")

                fixture_dump = REPO_ROOT / "test" / "data" / "dump-fixture.json"
                if not fixture_dump.is_file():
                    self.skipTest("Test fixture dump-fixture.json not found.")

                # Step 1: Synthesize netlist from dump fixture (60 SPM demand)
                demand = dict.fromkeys(netbuild.SPM_PACKS, 60.0)
                ret_build = netbuild.build_all(fixture_dump, demand, work_dir)
                self.assertEqual(ret_build, 0, "Netbuild failed to synthesize design")

                # Step 2: Run OpenROAD place and route in Podman container
                tcl_script = "/repo/flow/factorilog.tcl"
                cmd_orfs = [
                    "podman",
                    "run",
                    "--rm",
                    "-v",
                    f"{REPO_ROOT}:/repo",
                    "-v",
                    f"{work_dir}:/work",
                    "-w",
                    "/work",
                    OPENROAD_IMAGE,
                    "bash",
                    "-lc",
                    f"source /OpenROAD-flow-scripts/env.sh >/dev/null && openroad -no_init -exit {tcl_script}",
                ]
                podman_env = {**os.environ, "TMPDIR": "/tmp"}
                res_orfs = subprocess.run(cmd_orfs, env=podman_env, capture_output=True, text=True)
                self.assertEqual(
                    res_orfs.returncode,
                    0,
                    f"OpenROAD execution failed:\n{res_orfs.stderr}\n{res_orfs.stdout}",
                )

                def_path = work_dir / "factory.def"
                netlist_path = work_dir / "netlist.json"
                self.assertTrue(def_path.is_file(), "factory.def not generated")
                self.assertTrue(netlist_path.is_file(), "netlist.json not generated")

                # Step 3: Convert DEF and netlist to blueprint
                def_text = def_path.read_text()
                netlist_data = json.loads(netlist_path.read_text())
                bp_string = def2bp.def_to_blueprint(def_text, netlist_data, label="e2e_factory")

            self.assertTrue(bp_string.startswith("0"), "Invalid blueprint format prefix")

            entities = blueprint.entities_of(bp_string)
            total_entities = len(entities)
            self.assertGreater(total_entities, 0, "No entities in generated blueprint")

            # Step 4: Validate blueprint in headless Factorio engine
            results = check_blueprint.validate_blueprint(
                bp_string, game_bin, work_dir / "factorio_check"
            )

            import_code = results.get("RESULT_IMPORT", -1)
            self.assertEqual(import_code, 0, "Factorio rejected blueprint syntax.")

            built_count = results.get("RESULT_BUILT", 0)
            # Expect 100 percent entity placement
            self.assertEqual(
                built_count,
                total_entities,
                f"Expected 100% entity placement, but only {built_count}/{total_entities} placed.",
            )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
