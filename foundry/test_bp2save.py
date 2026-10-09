"""Unit tests for the blueprint to save converter."""

import sys
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
        self.assertIn("loader", lua_code)
        self.assertIn("infinity-chest", lua_code)

    def test_build_scenario_archive(self) -> None:
        """Verify scenario archive contains required control.lua and metadata."""
        import tempfile
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            bp_file = tmp_dir / "test.blueprint"
            bp_file.write_text("0eNpjYGBg4GBgYGBmYDAEQgYnxgc\n")
            out_zip = tmp_dir / "test.zip"
            bp2save.build_scenario_archive(
                blueprint_file=bp_file,
                out_save_file=out_zip,
            )
            self.assertTrue(out_zip.is_file())
            with zipfile.ZipFile(out_zip, "r") as zf:
                names = zf.namelist()
                self.assertIn("test/control.lua", names)
                self.assertIn("test/description.json", names)
                self.assertIn("test/blueprint.txt", names)
                self.assertIn("test/README.txt", names)


if __name__ == "__main__":
    unittest.main()
