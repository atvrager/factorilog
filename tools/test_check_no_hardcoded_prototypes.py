"""Unit tests for the hardcoded prototype AST linter.

Verifies detection of hardcoded spans, tier tables, and expansion entities.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_no_hardcoded_prototypes import check_directory, check_file


class TestCheckNoHardcodedPrototypes(unittest.TestCase):
    """Test suite for the prototype AST linter."""

    def test_linter_detects_expansion_item_name(self) -> None:
        """Verify that hardcoding expansion item names is flagged."""
        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write("BELT = 'turbo-underground-belt'\n")
            f.flush()
            violations = check_file(Path(f.name))
            self.assertTrue(len(violations) >= 1)
            self.assertTrue(any("turbo-underground-belt" in v for v in violations))

    def test_linter_detects_hardcoded_span_constant(self) -> None:
        """Verify that assigning hardcoded span constants is flagged."""
        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write("MAX_PIPE_GROUND_SPAN = 10\n")
            f.flush()
            violations = check_file(Path(f.name))
            self.assertTrue(len(violations) >= 1)
            self.assertTrue(any("MAX_PIPE_GROUND_SPAN" in v for v in violations))

    def test_linter_detects_numeric_assignment_to_span_var(self) -> None:
        """Verify that assigning numeric literals to span variables is flagged."""
        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write("max_span = 10\n")
            f.flush()
            violations = check_file(Path(f.name))
            self.assertTrue(len(violations) >= 1)
            self.assertTrue(any("max_span" in v for v in violations))

    def test_linter_detects_fallback_default_in_get(self) -> None:
        """Verify that prototype get() with numeric fallback is flagged."""
        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write("span = proto.get('max_distance', 5)\n")
            f.flush()
            violations = check_file(Path(f.name))
            self.assertTrue(len(violations) >= 1)
            self.assertTrue(any("max_distance" in v for v in violations))

    def test_linter_detects_hardcoded_tier_table(self) -> None:
        """Verify that assigning hardcoded tier lists is flagged."""
        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write("known_tiers = [('underground-belt', 5)]\n")
            f.flush()
            violations = check_file(Path(f.name))
            self.assertTrue(len(violations) >= 1)
            self.assertTrue(any("known_tiers" in v for v in violations))

    def test_router_and_native_engine_clean(self) -> None:
        """Verify that router and native engine contain zero hardcoded prototypes."""
        foundry_dir = Path("foundry")
        if not foundry_dir.is_dir():
            foundry_dir = Path(__file__).resolve().parent.parent / "foundry"
        target_dirs = [
            foundry_dir / "router",
            foundry_dir / "native_engine",
        ]
        for d in target_dirs:
            if d.is_dir():
                violations = check_directory(d)
                if violations:
                    msg = "\n".join(violations)
                    self.fail(f"Found forbidden hardcoded prototypes in {d}:\n{msg}")


if __name__ == "__main__":
    unittest.main()
