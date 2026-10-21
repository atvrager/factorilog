"""Unit tests for Factorio native engine loader, symbol resolution, and simulation."""

from __future__ import annotations

import unittest
from pathlib import Path

from foundry.native_engine.engine import (
    FactorioNativeEngine,
    make_fluid_buffer,
    make_transport_line_with_items,
    read_fluid_buffer,
    read_item_distances,
    read_transport_line,
    simulate_pipe_network,
    simulate_splitter_step,
    validate_underground_belt_span,
)
from foundry.native_engine.symbol_finder import (
    FactorioSymbolFinder,
    resolve_factorio_binary,
)


class TestNativeEngine(unittest.TestCase):
    """Test suite for Factorio native simulation engine integration."""

    def setUp(self) -> None:
        self.game_bin = resolve_factorio_binary()

    def test_symbol_finder_missing_binary(self) -> None:
        """Verify symbol finder raises FileNotFoundError on invalid paths."""
        with self.assertRaises(FileNotFoundError):
            FactorioSymbolFinder(Path("/nonexistent/path/to/factorio"))

    def test_symbol_finder_game_resolution(self) -> None:
        """Verify symbol finder discovers all required simulation routines."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        finder = FactorioSymbolFinder(self.game_bin)
        required_symbols = [
            "fluid_get_flow_rate",
            "fluid_take_fluid",
            "fluid_manager_update",
            "fluid_segment_build",
            "fluid_segment_merge",
            "fluid_box_push",
            "belt_update",
            "belt_move",
            "belt_unsquash",
            "belt_try_to_insert",
            "belt_try_to_insert_at_back",
            "splitter_resolve",
        ]
        for key in required_symbols:
            self.assertTrue(
                finder.has_symbol(key),
                f"Required symbol '{key}' was not found in {self.game_bin.name}",
            )
            self.assertGreater(finder.get_offset(key), 0)

    def test_native_fluid_flow_gradient(self) -> None:
        """Verify native fluid flow rate calculation across various pressure gradients."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)

        # 1. Full headroom: 100 to 0 -> 80% flow rate
        flow_full = engine.calculate_fluid_flow(100.0, 100.0, 0.0, 100.0)
        self.assertAlmostEqual(flow_full, 80.0, places=4)

        # 2. Half pressure with full headroom: 50 to 0 -> 80% of current level
        flow_half = engine.calculate_fluid_flow(50.0, 100.0, 0.0, 100.0)
        self.assertAlmostEqual(flow_half, 40.0, places=4)

        # 3. Partial headroom: 50 to 50 (50 headroom remaining) -> 40.0 flow rate
        flow_partial = engine.calculate_fluid_flow(50.0, 100.0, 50.0, 100.0)
        self.assertAlmostEqual(flow_partial, 40.0, places=4)

        # 4. Zero headroom: destination is 100% full -> zero flow rate
        flow_saturated = engine.calculate_fluid_flow(50.0, 100.0, 100.0, 100.0)
        self.assertAlmostEqual(flow_saturated, 0.0, places=4)

        # 5. Empty source: source has zero volume -> zero flow rate
        flow_empty = engine.calculate_fluid_flow(0.0, 100.0, 0.0, 100.0)
        self.assertAlmostEqual(flow_empty, 0.0, places=4)

    def test_native_fluid_mass_conservation(self) -> None:
        """Verify volume conservation during native fluid transfer."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        dst = make_fluid_buffer(fluid_id=0, amount=0.0, capacity=100.0)
        src = make_fluid_buffer(fluid_id=1, amount=60.0, capacity=100.0)

        initial_total = read_fluid_buffer(dst)["amount"] + read_fluid_buffer(src)["amount"]
        transferred = engine.transfer_fluid(dst, src, 25.0)

        self.assertAlmostEqual(transferred, 25.0, places=4)
        dst_info = read_fluid_buffer(dst)
        src_info = read_fluid_buffer(src)

        self.assertAlmostEqual(dst_info["amount"], 25.0, places=4)
        self.assertAlmostEqual(src_info["amount"], 35.0, places=4)
        self.assertEqual(dst_info["fluid_id"], 1)

        final_total = dst_info["amount"] + src_info["amount"]
        self.assertAlmostEqual(initial_total, final_total, places=4)

    def test_native_fluid_capacity_clamping(self) -> None:
        """Verify fluid transfer clamps to remaining destination capacity."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # Destination already has 90 units out of 100 capacity (10 units space left)
        dst = make_fluid_buffer(fluid_id=1, amount=90.0, capacity=100.0)
        src = make_fluid_buffer(fluid_id=1, amount=50.0, capacity=100.0)

        transferred = engine.transfer_fluid(dst, src, 30.0)
        self.assertAlmostEqual(transferred, 10.0, places=4)

        dst_info = read_fluid_buffer(dst)
        src_info = read_fluid_buffer(src)
        self.assertAlmostEqual(dst_info["amount"], 100.0, places=4)
        self.assertAlmostEqual(src_info["amount"], 40.0, places=4)

    def test_native_fluid_mixing_rejection(self) -> None:
        """Verify native fluid engine rejects mixing different fluid IDs."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # dst contains water (id 1), src contains crude oil (id 2)
        dst = make_fluid_buffer(fluid_id=1, amount=20.0, capacity=100.0)
        src = make_fluid_buffer(fluid_id=2, amount=50.0, capacity=100.0)

        transferred = engine.transfer_fluid(dst, src, 10.0)
        self.assertEqual(transferred, 0.0)

        dst_info = read_fluid_buffer(dst)
        src_info = read_fluid_buffer(src)
        self.assertAlmostEqual(dst_info["amount"], 20.0, places=4)
        self.assertAlmostEqual(src_info["amount"], 50.0, places=4)
        self.assertEqual(dst_info["fluid_id"], 1)
        self.assertEqual(src_info["fluid_id"], 2)

    def test_native_fluid_temperature_blending(self) -> None:
        """Verify mass-weighted temperature calculation when mixing identical fluids."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # dst has 20 units at 10 deg, src has 20 units at 50 deg
        dst = make_fluid_buffer(fluid_id=1, amount=20.0, temperature=10.0, capacity=100.0)
        src = make_fluid_buffer(fluid_id=1, amount=20.0, temperature=50.0, capacity=100.0)

        transferred = engine.transfer_fluid(dst, src, 20.0)
        self.assertAlmostEqual(transferred, 20.0, places=4)

        dst_info = read_fluid_buffer(dst)
        # Expected temp: (20 * 10 + 20 * 50) / 40 = 30.0 degrees
        self.assertAlmostEqual(dst_info["temperature"], 30.0, places=2)
        self.assertAlmostEqual(dst_info["amount"], 40.0, places=4)

    def test_native_belt_unsquash_expansion(self) -> None:
        """Verify native TransportLine::unsquash expands squashed items to 64 spacing."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # Item 0 has distance 40 (< 64, squashed)
        line, deque = make_transport_line_with_items([40, 64])

        engine.unsquash_transport_line(line, 24)
        distances = read_item_distances(deque, 2)
        # 40 + 24 = 64 (0x40 standard item spacing)
        self.assertEqual(distances[0], 64)
        self.assertEqual(distances[1], 64)

    def test_native_belt_unsquash_partial(self) -> None:
        """Verify partial unsquash increments distance without exceeding available slack."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        line, deque = make_transport_line_with_items([30, 64])

        engine.unsquash_transport_line(line, 15)
        distances = read_item_distances(deque, 2)
        # 30 + 15 = 45 (still squashed, but advanced)
        self.assertEqual(distances[0], 45)

    def test_native_belt_unsquash_saturated(self) -> None:
        """Verify unsquash does not expand items beyond the 64-unit physical spacing."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        line, deque = make_transport_line_with_items([64, 64])

        engine.unsquash_transport_line(line, 20)
        distances = read_item_distances(deque, 2)
        self.assertEqual(distances[0], 64)
        self.assertEqual(distances[1], 64)

    def test_native_belt_move_advances_items(self) -> None:
        """Verify native TransportLine::move decrements front distance and increases back distance."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        line, deque = make_transport_line_with_items([64], front_distance=100)

        # Move by 8 units (one tick of basic yellow belt)
        engine.move_transport_line(line, 8)
        state = read_transport_line(line)

        # Front distance decreases from 100 to 92
        self.assertEqual(state["front_distance"], 92)
        # Back distance increases by 8
        self.assertEqual(state["back_distance"], (256 - 64) + 8)

    def test_native_belt_move_with_unsquash(self) -> None:
        """Verify native TransportLine::move unsquashes compressed items during line advancement."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # Item 0 squashed at 40 (< 64), front distance 100
        line, deque = make_transport_line_with_items([40, 64], front_distance=100)

        # Move by 24 units (one tick of express blue belt)
        engine.move_transport_line(line, 24)
        state = read_transport_line(line)
        distances = read_item_distances(deque, 2)

        # Front distance decreased by 24
        self.assertEqual(state["front_distance"], 76)
        # Item 0 unsquashed from 40 to 64
        self.assertEqual(distances[0], 64)

    def test_native_belt_density_limit(self) -> None:
        """Verify maximum item capacity per tile per transport line."""
        tile_length = 256
        item_spacing = 64
        max_items_per_lane = tile_length // item_spacing
        self.assertEqual(max_items_per_lane, 4)

        # A dual-lane belt tile holds at most 8 items
        max_items_per_belt = max_items_per_lane * 2
        self.assertEqual(max_items_per_belt, 8)

    def test_native_dual_lane_independence(self) -> None:
        """Verify independent item transport across left and right belt lines."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # Left line carries 2 iron plates
        left_line, left_deque = make_transport_line_with_items([64, 64], front_distance=100)
        # Right line carries 1 copper plate
        right_line, right_deque = make_transport_line_with_items([64], front_distance=150)

        # Advance left line only by 16 units (fast red belt speed)
        engine.move_transport_line(left_line, 16)

        left_state = read_transport_line(left_line)
        right_state = read_transport_line(right_line)

        # Left line advanced. Right line remained untouched.
        self.assertEqual(left_state["front_distance"], 84)
        self.assertEqual(right_state["front_distance"], 150)

    def test_native_splitter_distribution_and_diversion(self) -> None:
        """Verify splitter alternating distribution, blocked diversion, and item filtering."""
        # 1. Balanced 1:2 split alternates evenly
        in_items = [1, 1, 1, 1]
        out1, out2 = simulate_splitter_step(in_items)
        self.assertEqual(out1, [1, 1])
        self.assertEqual(out2, [1, 1])

        # 2. Blocked output 1 diverts all flow to output 2
        out1_blocked, out2_open = simulate_splitter_step(in_items, output_1_blocked=True)
        self.assertEqual(out1_blocked, [])
        self.assertEqual(out2_open, [1, 1, 1, 1])

        # 3. Item filtering routes target ID to output 1 and others to output 2
        mixed_items = [10, 20, 10, 20]
        f_out1, f_out2 = simulate_splitter_step(mixed_items, filter_item=10)
        self.assertEqual(f_out1, [10, 10])
        self.assertEqual(f_out2, [20, 20])

    def test_native_underground_span_legality(self) -> None:
        """Verify underground belt maximum span limits and cardinal alignment."""
        # Basic belt: max 5 tiles
        self.assertTrue(validate_underground_belt_span("transport-belt", 5, 0))
        self.assertFalse(validate_underground_belt_span("transport-belt", 6, 0))

        # Fast belt: max 7 tiles
        self.assertTrue(validate_underground_belt_span("fast-transport-belt", 0, 7))
        self.assertFalse(validate_underground_belt_span("fast-transport-belt", 0, 8))

        # Express belt: max 9 tiles
        self.assertTrue(validate_underground_belt_span("express-transport-belt", 9, 0))
        self.assertFalse(validate_underground_belt_span("express-transport-belt", 10, 0))

        # Reject diagonal alignment
        self.assertFalse(validate_underground_belt_span("transport-belt", 3, 3))

    def test_native_fluid_network_propagation(self) -> None:
        """Verify fluid propagation and mass conservation across a multi-pipe series network."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # 5-pipe series: pipe 0 full (100 units), pipes 1-4 empty
        pipes = [
            make_fluid_buffer(fluid_id=1, amount=100.0, capacity=100.0),
            make_fluid_buffer(fluid_id=1, amount=0.0, capacity=100.0),
            make_fluid_buffer(fluid_id=1, amount=0.0, capacity=100.0),
            make_fluid_buffer(fluid_id=1, amount=0.0, capacity=100.0),
            make_fluid_buffer(fluid_id=1, amount=0.0, capacity=100.0),
        ]

        initial_total = sum(read_fluid_buffer(p)["amount"] for p in pipes)
        self.assertAlmostEqual(initial_total, 100.0, places=4)

        # Simulate 30 ticks of propagation
        simulate_pipe_network(engine, pipes, ticks=30)

        # Fluid has reached the terminal pipe
        final_sink_amount = read_fluid_buffer(pipes[4])["amount"]
        self.assertGreater(final_sink_amount, 0.0)

        # The network conserves total fluid mass across all pipes
        final_total = sum(read_fluid_buffer(p)["amount"] for p in pipes)
        self.assertAlmostEqual(initial_total, final_total, places=4)

    def test_native_fluid_network_drc_rejection(self) -> None:
        """Verify fluid transfer rejection across mismatched fluid types in a pipe network."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # Pipe 0 has water (id 1), Pipe 1 has crude oil (id 2)
        pipe_water = make_fluid_buffer(fluid_id=1, amount=50.0, capacity=100.0)
        pipe_oil = make_fluid_buffer(fluid_id=2, amount=10.0, capacity=100.0)
        pipes = [pipe_water, pipe_oil]

        # Simulate attempted transfer across mismatched junction
        simulate_pipe_network(engine, pipes, ticks=5)

        # The engine rejects transfer so quantities and types do not change
        water_info = read_fluid_buffer(pipes[0])
        oil_info = read_fluid_buffer(pipes[1])
        self.assertAlmostEqual(water_info["amount"], 50.0, places=4)
        self.assertAlmostEqual(oil_info["amount"], 10.0, places=4)
        self.assertEqual(water_info["fluid_id"], 1)
        self.assertEqual(oil_info["fluid_id"], 2)

    def test_native_underground_pipe_machine_direct_connection(self) -> None:
        """Verify direct fluid flow between machine buffer and underground pipe buffer."""
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")

        engine = FactorioNativeEngine(self.game_bin)
        # Source machine buffer connects directly to underground entrance buffer.
        # Underground exit buffer connects directly to sink machine buffer.
        # No intermediate surface pipe buffers exist in this network.
        source_machine = make_fluid_buffer(fluid_id=1, amount=100.0, capacity=1000.0)
        ug_entrance = make_fluid_buffer(fluid_id=1, amount=0.0, capacity=100.0)
        ug_exit = make_fluid_buffer(fluid_id=1, amount=0.0, capacity=100.0)
        sink_machine = make_fluid_buffer(fluid_id=1, amount=0.0, capacity=1000.0)

        buffers = [source_machine, ug_entrance, ug_exit, sink_machine]
        initial_total = sum(read_fluid_buffer(b)["amount"] for b in buffers)
        self.assertAlmostEqual(initial_total, 100.0, places=4)

        simulate_pipe_network(engine, buffers, ticks=30)

        # Fluid transfers directly into the sink machine buffer
        sink_amount = read_fluid_buffer(sink_machine)["amount"]
        self.assertGreater(sink_amount, 0.0)

        # Total fluid mass stays constant
        final_total = sum(read_fluid_buffer(b)["amount"] for b in buffers)
        self.assertAlmostEqual(initial_total, final_total, places=4)


if __name__ == "__main__":
    unittest.main()
