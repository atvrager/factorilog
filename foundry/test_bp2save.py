"""Unit tests for the blueprint to save converter."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bp2save


class TestBp2Save(unittest.TestCase):
    """Test suite for blueprint to playable Factorio save converter."""

    def test_find_factorio_binary(self) -> None:
        """Verify Factorio binary lookup succeeds if game is installed."""
        try:
            bin_path = bp2save.find_factorio_binary()
            self.assertTrue(bin_path.is_file())
        except FileNotFoundError:
            self.skipTest("Factorio binary not found in environment.")

    def test_generate_control_lua(self) -> None:
        """Verify generated control.lua script contains blueprint and pin data."""
        bp_string = "0eNpjYGBg4GBgYGBmYDAEQgYnxgc"
        pins_data = {
            "inputs": [{"name": "iron-ore", "x": 10.5, "y": 20.5, "type": "belt"}],
            "outputs": [{"name": "automation-science-pack", "x": 30.5, "y": 40.5, "type": "belt"}],
        }
        lua_code = bp2save.generate_control_lua(bp_string, pins_data)
        self.assertIn("local BP = [==[0eNpjYGBg4GBgYGBmYDAEQgYnxgc]==]", lua_code)
        self.assertIn("iron-ore", lua_code)
        self.assertIn("script.on_init", lua_code)
        self.assertIn("bulk-inserter", lua_code)
        self.assertIn("small-electric-pole", lua_code)


if __name__ == "__main__":
    unittest.main()
