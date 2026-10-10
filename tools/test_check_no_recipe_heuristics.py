"""Unit tests for the recipe heuristic AST linter."""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.check_no_recipe_heuristics import check_directory


class TestCheckNoRecipeHeuristics(unittest.TestCase):
    """Test AST checking of recipe heuristics."""

    def test_foundry_directory_clean(self) -> None:
        """Verify that foundry layout and compiler code contains zero recipe heuristics."""
        foundry_dir = Path("foundry")
        violations = check_directory(foundry_dir)
        if violations:
            msg = "\n".join(violations)
            self.fail(f"Found forbidden recipe heuristics in foundry:\n{msg}")


if __name__ == "__main__":
    unittest.main()
