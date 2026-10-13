"""Unit tests for Factorio native engine loader and symbol resolution."""

from __future__ import annotations

import unittest
from pathlib import Path

from foundry.native_engine.engine import FactorioNativeEngine
from foundry.native_engine.symbol_finder import (
    FactorioSymbolFinder,
    resolve_factorio_binary,
)


class TestNativeEngine(unittest.TestCase):
    """Test suite for Factorio native engine integration."""

    def setUp(self) -> None:
        self.game_bin = resolve_factorio_binary()

    def test_symbol_finder_missing_binary(self) -> None:
        """Verify symbol finder raises FileNotFoundError on invalid paths."""
        with self.assertRaises(FileNotFoundError):
            FactorioSymbolFinder(Path("/nonexistent/path/to/factorio"))

    def test_symbol_finder_game_resolution(self) -> None:
        """Verify symbol finder discovers key simulation routines when game binary is present."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        finder = FactorioSymbolFinder(self.game_bin)
        self.assertTrue(finder.has_symbol("fluid_get_flow_rate"))
        self.assertTrue(finder.has_symbol("belt_move"))
        self.assertTrue(finder.has_symbol("belt_unsquash"))
        self.assertTrue(finder.has_symbol("splitter_resolve"))

        offsets = finder.all_offsets
        for key in (
            "fluid_get_flow_rate",
            "belt_move",
            "belt_unsquash",
            "splitter_resolve",
        ):
            self.assertIn(key, offsets)
            self.assertGreater(offsets[key], 0)

    def test_native_engine_fluid_flow(self) -> None:
        """Verify native execution of FluidBuffer::getFlowRate directly in the Factorio binary."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        flow_full = engine.calculate_fluid_flow(100.0, 100.0, 0.0, 100.0)
        self.assertAlmostEqual(flow_full, 80.0, places=4)

        flow_half = engine.calculate_fluid_flow(50.0, 100.0, 0.0, 100.0)
        self.assertAlmostEqual(flow_half, 40.0, places=4)

        flow_equal = engine.calculate_fluid_flow(50.0, 100.0, 50.0, 100.0)
        self.assertAlmostEqual(flow_equal, 0.0, places=4)


if __name__ == "__main__":
    unittest.main()
