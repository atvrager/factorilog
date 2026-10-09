"""Unit tests for TCL structural linter."""

import tempfile
import unittest
from pathlib import Path

from tools.check_flow_tcl import check_tcl_file


class TestCheckFlowTcl(unittest.TestCase):
    def test_clean_file_passes(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as f:
            f.write("global_placement -density $density\n")
            f_path = Path(f.name)
        try:
            violations = check_tcl_file(f_path)
            self.assertEqual(len(violations), 0)
        finally:
            f_path.unlink()

    def test_create_obstruction_forbidden(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as f:
            f.write("create_obstruction -layer M1 -region {0 0 10 10}\n")
            f_path = Path(f.name)
        try:
            violations = check_tcl_file(f_path)
            self.assertTrue(any("create_obstruction" in v for v in violations))
        finally:
            f_path.unlink()

    def test_find_inst_forbidden(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as f:
            f.write('set inst [$block findInst "u_something"]\n')
            f_path = Path(f.name)
        try:
            violations = check_tcl_file(f_path)
            self.assertTrue(any("findInst" in v for v in violations))
        finally:
            f_path.unlink()

    def test_hardcoded_region_forbidden(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as f:
            f.write("set region {10.0 20.0 30.0 40.0}\n")
            f_path = Path(f.name)
        try:
            violations = check_tcl_file(f_path)
            self.assertTrue(any("hardcoded region" in v.lower() for v in violations))
        finally:
            f_path.unlink()


if __name__ == "__main__":
    unittest.main()
