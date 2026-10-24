"""Unit tests for the anti-cheat AST linter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_no_bot_cheats import check_directory, check_file


class TestCheckNoBotCheats(unittest.TestCase):
    """Test AST detection of forbidden Lua gameplay cheats and reach overrides."""

    def test_linter_detects_lua_cheats_and_reach_overrides(self) -> None:
        """Verify that Lua item grants, teleports, destroys, and large stop_tolerance are flagged."""
        sample_code = (
            "def bad_helper(phase1_bom_items=None):\n"
            "    lua = '''\n"
            "      p.character_build_distance_bonus = 60\n"
            "      p.character.teleport({10, 20})\n"
            "      p.insert({name = 'transport-belt', count = 100})\n"
            "      ent.destroy()\n"
            "      techs['logistics'].researched = true\n"
            "    '''\n"
            "    bot.step_lockstep_proxy(proxy, state, ids, stop_tolerance=24.0)\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py") as f:
            f.write(sample_code)
            f.flush()
            violations = check_file(Path(f.name), force=True)
            self.assertGreaterEqual(len(violations), 7)
            joined = "\n".join(violations)
            self.assertIn("phase1_bom_items", joined)
            self.assertIn("character_build_distance_bonus", joined)
            self.assertIn(".teleport()", joined)
            self.assertIn(".insert()", joined)
            self.assertIn(".destroy()", joined)
            self.assertIn(".researched = true", joined)
            self.assertIn("stop_tolerance=24.0", joined)

    def test_foundry_and_test_directories_clean(self) -> None:
        """Verify that foundry and test bot/MP modules contain zero gameplay cheats."""
        violations = check_directory(Path("foundry")) + check_directory(Path("test"))
        if violations:
            msg = "\n".join(violations)
            self.fail(f"Found {len(violations)} forbidden gameplay cheat(s):\n{msg}")


if __name__ == "__main__":
    unittest.main()
