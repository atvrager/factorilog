"""Unit tests for the hardcoded routing AST linter."""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.check_no_hardcoded_routing import check_directory, check_file


class TestCheckNoHardcodedRouting(unittest.TestCase):
    """Test AST checking of hardcoded routing constructs."""

    def test_linter_detects_hardcoded_coordinates(self) -> None:
        """Verify that comparing or assigning large coordinates is flagged."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write("if w['x1'] == 179500:\n    w['y1'] = 674500\n")
            f.flush()
            violations = check_file(Path(f.name))
            self.assertTrue(len(violations) >= 2)
            self.assertTrue(any("179500" in v for v in violations))
            self.assertTrue(any("674500" in v for v in violations))

    def test_linter_detects_instance_net_names(self) -> None:
        """Verify that matching instance-specific net names is flagged."""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write("if net.get('name') == 'iron_gear_wheel__u_engine_unit_4':\n    pass\n")
            f.flush()
            violations = check_file(Path(f.name))
            self.assertTrue(len(violations) >= 1)
            self.assertTrue(any("__u_" in v for v in violations))

    def test_foundry_directory_clean(self) -> None:
        """Verify that foundry layout and compiler code contains zero hardcoded routing constructs."""
        foundry_dir = Path("foundry")
        violations = check_directory(foundry_dir)
        if violations:
            msg = "\n".join(violations)
            self.fail(f"Found forbidden hardcoded routing constructs in foundry:\n{msg}")


if __name__ == "__main__":
    unittest.main()
