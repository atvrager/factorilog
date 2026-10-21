"""Unit tests for Power Distribution Network (PDN) synthesis."""

from __future__ import annotations

import unittest
from collections.abc import Iterator

from foundry import entity_helpers as eh
from foundry import pdn


class TestPdn(unittest.TestCase):
    """Test suite for PDN synthesis and multi-domain power switch gating."""

    def test_pdn_is_aon_entity_and_empty_powered(self) -> None:
        self.assertTrue(pdn.is_aon_entity({"name": "arithmetic-combinator"}))
        self.assertTrue(pdn.is_aon_entity({"name": "decider-combinator"}))
        self.assertTrue(pdn.is_aon_entity({"name": "display-panel"}))
        self.assertTrue(pdn.is_aon_entity({"name": "steam-engine"}))
        self.assertTrue(pdn.is_aon_entity({"name": "fast-inserter", "power_domain": "aon"}))
        self.assertFalse(pdn.is_aon_entity({"name": "fast-inserter"}))
        self.assertFalse(pdn.is_aon_entity({"name": "assembling-machine-3"}))
        self.assertFalse(pdn.is_aon_entity({}))

        entities = [{"name": "transport-belt", "position": {"x": 0.5, "y": 0.5}}]
        poles, mst = pdn.generate_pdn(entities, set(), set(), set(), set())
        self.assertEqual(poles, {})
        self.assertEqual(mst, [])

    def test_pdn_pin_and_footprint_exclusions_and_greedy_cover(self) -> None:
        # Verify pin exclusion at (10.5, 10.5) and k in (1, 2, 3) on both x and y axes.
        pin_block_occupied = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(-5, 25)
            for y in range(-5, 25)
            if (float(x) + 0.5, float(y) + 0.5) != (10.5, 7.5)
        }
        with self.assertRaises(RuntimeError):
            pdn.generate_pdn(
                [{"name": "fast-inserter", "position": {"x": 10.5, "y": 5.5}}],
                pin_block_occupied,
                {(10.5, 10.5)},
                set(),
                set(),
            )
        with self.assertRaises(RuntimeError):
            pdn.generate_pdn(
                [{"name": "fast-inserter", "position": {"x": 10.5, "y": 5.5}}],
                pin_block_occupied,
                {(10.5, 4.5)},
                set(),
                set(),
            )
        with self.assertRaises(RuntimeError):
            pdn.generate_pdn(
                [{"name": "fast-inserter", "position": {"x": 10.5, "y": 5.5}}],
                pin_block_occupied,
                {(7.5, 7.5)},
                set(),
                set(),
            )
        with self.assertRaises(RuntimeError):
            pdn.generate_pdn(
                [{"name": "fast-inserter", "position": {"x": 10.5, "y": 5.5}}],
                pin_block_occupied,
                {(10.5, 7.5)},
                set(),
                set(),
            )

        # Verify entity footprint left-edge and right-edge exclusions at x=9.5 and x=11.5.
        for edge_x in (9.5, 11.5):
            footprint_occupied = {
                (float(x) + 0.5, float(y) + 0.5)
                for x in range(-5, 25)
                for y in range(-5, 25)
                if (float(x) + 0.5, float(y) + 0.5) != (edge_x, 10.5)
            }
            with self.assertRaises(RuntimeError):
                pdn.generate_pdn(
                    [{"name": "assembling-machine-3", "position": {"x": 10.5, "y": 10.5}}],
                    footprint_occupied,
                    set(),
                    set(),
                    set(),
                )

        # Verify that a 1x1 entity at (10.5, 10.5) does not block adjacent tiles.
        for adj_pt in ((9.5, 10.5), (11.5, 10.5), (10.5, 9.5), (10.5, 11.5)):
            non_splitter_occupied = {
                (float(x) + 0.5, float(y) + 0.5)
                for x in range(-5, 25)
                for y in range(-5, 25)
                if (float(x) + 0.5, float(y) + 0.5) != adj_pt
            }
            poles_ns, _ = pdn.generate_pdn(
                [
                    {
                        "name": "fast-inserter",
                        "position": {"x": 10.5, "y": 10.5},
                        "direction": eh.DIRECTION_NORTH,
                    }
                ],
                non_splitter_occupied,
                set(),
                set(),
                set(),
            )
            self.assertEqual(set(poles_ns.keys()), {adj_pt})

        # Verify that 6x6 grid candidate search finds half-integer tile centers.
        grid_cand_occupied = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(-10, 30)
            for y in range(-10, 30)
            if (float(x) + 0.5, float(y) + 0.5) != (10.5, 9.5)
        }
        poles_gc, _ = pdn.generate_pdn(
            [{"name": "assembling-machine-3", "position": {"x": 10.5, "y": 13.5}}],
            grid_cand_occupied,
            set(),
            set(),
            set(),
        )
        self.assertEqual(set(poles_gc.keys()), {(10.5, 9.5)})
        for px, py in poles_gc:
            self.assertEqual(px - int(px), 0.5)
            self.assertEqual(py - int(py), 0.5)

        # Verify horizontal greedy set cover across two machines.
        two_ent_occupied = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(-10, 35)
            for y in range(-10, 35)
            if (float(x) + 0.5, float(y) + 0.5) not in {(7.5, 10.5), (1.5, 10.5), (13.5, 5.5)}
        }
        poles_horiz, _ = pdn.generate_pdn(
            [
                {"name": "assembling-machine-3", "position": {"x": 11.5, "y": 10.5}},
                {"name": "assembling-machine-3", "position": {"x": 5.5, "y": 10.5}},
            ],
            two_ent_occupied,
            set(),
            set(),
            set(),
            config=pdn.PdnConfig(grid_step=12),
        )
        self.assertEqual(set(poles_horiz.keys()), {(7.5, 10.5)})

        # Verify vertical greedy set cover across gy_min and gy_max buckets.
        two_ent_vert_occupied = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(-10, 35)
            for y in range(-10, 35)
            if (float(x) + 0.5, float(y) + 0.5) not in {(10.5, 9.5), (10.5, 1.5)}
        }
        poles_vert, _ = pdn.generate_pdn(
            [
                {"name": "assembling-machine-3", "position": {"x": 10.5, "y": 13.5}},
                {"name": "assembling-machine-3", "position": {"x": 10.5, "y": 5.5}},
            ],
            two_ent_vert_occupied,
            set(),
            set(),
            set(),
        )
        self.assertEqual(set(poles_vert.keys()), {(10.5, 9.5)})

        # Verify greedy bucket count decrement across overlapping candidate sets.
        decrement_occupied = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(-10, 40)
            for y in range(-10, 40)
            if (float(x) + 0.5, float(y) + 0.5) not in {(7.5, 11.5), (10.5, 11.5), (16.5, 11.5)}
        }
        poles_dec, _ = pdn.generate_pdn(
            [
                {"name": "fast-inserter", "position": {"x": 4.5, "y": 10.5}},
                {"name": "fast-inserter", "position": {"x": 5.5, "y": 10.5}},
                {"name": "fast-inserter", "position": {"x": 8.5, "y": 10.5}},
                {"name": "fast-inserter", "position": {"x": 9.5, "y": 10.5}},
                {"name": "fast-inserter", "position": {"x": 13.5, "y": 10.5}},
                {"name": "fast-inserter", "position": {"x": 19.5, "y": 10.5}},
            ],
            decrement_occupied,
            set(),
            set(),
            set(),
        )
        self.assertEqual(set(poles_dec.keys()), {(7.5, 11.5), (16.5, 11.5)})

    def test_pdn_fallback_uncovered_search_and_unpowered_error(self) -> None:
        # Force fallback neighborhood scan to cover (26.9, 26.9) at (31.5, 31.5).
        open_tiles = {(-2.5, -2.5), (27.5, 32.5), (31.5, 31.5), (40.5, 31.5)}
        occupied_edge: set[tuple[float, float]] = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(-15, 55)
            for y in range(-15, 55)
            if (float(x) + 0.5, float(y) + 0.5) not in open_tiles
        }
        poles, mst = pdn.generate_pdn(
            [
                {"name": "fast-inserter", "position": {"x": 0.1, "y": 0.1}},
                {"name": "assembling-machine-3", "position": {"x": 26.9, "y": 26.9}},
                {"name": "fast-inserter", "position": {"x": 37.5, "y": 31.5}},
            ],
            occupied_edge,
            set(),
            set(),
            set(),
        )
        self.assertEqual(set(poles.keys()), {(-2.5, -2.5), (31.5, 31.5), (40.5, 31.5)})
        self.assertEqual(mst, [((31.5, 31.5), (40.5, 31.5))])

        # Block (31.5, 31.5) so the machine lacks pole coverage.
        occupied_edge.add((31.5, 31.5))
        with self.assertRaises(RuntimeError):
            pdn.generate_pdn(
                [
                    {"name": "fast-inserter", "position": {"x": 0.1, "y": 0.1}},
                    {"name": "assembling-machine-3", "position": {"x": 26.9, "y": 26.9}},
                ],
                occupied_edge,
                set(),
                set(),
                set(),
            )

    def test_pdn_multi_domain_power_switch_and_terminals(self) -> None:
        entities = [
            {"name": "power-switch", "position": {"x": 20.0, "y": 20.0}},
            # AON entity on the left
            {"name": "arithmetic-combinator", "position": {"x": 5.5, "y": 20.0}},
            # Switched entity on the right
            {"name": "assembling-machine-3", "position": {"x": 35.5, "y": 20.5}},
        ]
        poles, mst = pdn.generate_pdn(entities, set(), set(), set(), set())
        aon_poles = {pos for pos, ent in poles.items() if ent["power_domain"] == "aon"}
        sw_poles = {pos for pos, ent in poles.items() if ent["power_domain"] == "switched"}
        self.assertGreaterEqual(len(aon_poles), 2)
        self.assertGreaterEqual(len(sw_poles), 2)
        self.assertTrue(aon_poles.isdisjoint(sw_poles))
        self.assertGreaterEqual(len(mst), 2)

        # Verify terminal distance limit and dx search order near power-switch.
        allowed_near_switch = {(18.5, 22.5), (22.5, 22.5), (27.5, 22.5)}
        switch_test_occupied = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(-20, 40)
            for y in range(-20, 40)
            if (float(x) + 0.5, float(y) + 0.5) not in allowed_near_switch
        } | {(float(x), float(y)) for x in range(-20, 40) for y in range(-20, 40)}
        poles_sw, _ = pdn.generate_pdn(
            [
                {"name": "power-switch", "position": {"x": 20.0, "y": 20.0}},
                {"name": "assembling-machine-3", "position": {"x": 27.5, "y": 26.8}},
            ],
            switch_test_occupied,
            set(),
            set(),
            set(),
        )
        self.assertEqual(set(poles_sw.keys()), allowed_near_switch)
        self.assertEqual(poles_sw[(18.5, 22.5)]["power_domain"], "aon")
        self.assertEqual(poles_sw[(22.5, 22.5)]["power_domain"], "switched")
        self.assertEqual(poles_sw[(27.5, 22.5)]["power_domain"], "switched")

        # Verify positive x reach (dx = +7) from power-switch at (20.0, 20.0).
        allowed_pos_x = {(20.5, 22.5), (27.5, 22.5)}
        pos_x_occupied = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(-20, 40)
            for y in range(-20, 40)
            if (float(x) + 0.5, float(y) + 0.5) not in allowed_pos_x
        } | {(float(x), float(y)) for x in range(-20, 40) for y in range(-20, 40)}
        poles_px, _ = pdn.generate_pdn(
            [
                {"name": "power-switch", "position": {"x": 20.0, "y": 20.0}},
                {"name": "assembling-machine-3", "position": {"x": 27.5, "y": 26.8}},
            ],
            pos_x_occupied,
            set(),
            set(),
            set(),
        )
        self.assertEqual(set(poles_px.keys()), allowed_pos_x)

        # Reject candidate tiles outside radius 8.0 from power-switch.
        only_corner_open = {
            (float(x) + 0.5, float(y) + 0.5)
            for x in range(0, 40)
            for y in range(0, 40)
            if (float(x) + 0.5, float(y) + 0.5) != (14.5, 14.5)
        }
        with self.assertRaises(RuntimeError):
            pdn.generate_pdn(
                [
                    {"name": "power-switch", "position": {"x": 20.0, "y": 20.0}},
                    {"name": "arithmetic-combinator", "position": {"x": 5.5, "y": 20.0}},
                ],
                only_corner_open,
                set(),
                set(),
                set(),
            )

    def test_perf_cases_smoke(self) -> None:
        """Smoke-test all PDN perf_* generator benchmarks."""
        for perf_fn in (
            perf_pdn_build_wire_adjacency,
            perf_pdn_build_domain_mst,
            perf_pdn_greedy_set_cover,
            perf_pdn_generate_multi_domain,
        ):
            for _ in perf_fn(16):
                pass


def perf_pdn_build_wire_adjacency(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for PDN candidate pole wire adjacency construction."""
    cands = [(float((i % 32) * 6 + 0.5), float((i // 32) * 6 + 0.5)) for i in range(n)]
    reach = pdn.DEFAULT_PDN_CONFIG.wire_reach
    yield
    pdn._build_wire_adjacency(cands, reach)


def perf_pdn_build_domain_mst(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for PDN domain minimum spanning tree construction."""
    poles = {(float((i % 32) * 6 + 0.5), float((i // 32) * 6 + 0.5)) for i in range(n)}
    reach = pdn.DEFAULT_PDN_CONFIG.wire_reach
    yield
    pdn._build_domain_mst(poles, reach)


def perf_pdn_greedy_set_cover(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for PDN greedy set cover pole selection."""
    cand_covers = {(float(i * 6 + 0.5), 0.5): {i, (i + 1) % n} for i in range(n)}
    yield
    pdn._greedy_set_cover(n, cand_covers)


def perf_pdn_generate_multi_domain(
    n: int = 32,
    *,
    _scales: tuple[int, ...] = (16, 32, 64, 128),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for multi-domain PDN synthesis with forbidden-entity checks."""
    entities: list[dict[str, object]] = [
        {"name": "power-switch", "position": {"x": 2.0, "y": 2.0}},
    ]
    for i in range(n):
        gx = float((i % 16) * 6 + 10.5)
        gy = float((i // 16) * 12 + 10.5)
        entities.append({"name": "fast-inserter", "position": {"x": gx, "y": gy}})
        entities.append({"name": "arithmetic-combinator", "position": {"x": gx, "y": gy + 6.0}})
    yield
    pdn.generate_pdn(list(entities), set(), set(), set(), set())


if __name__ == "__main__":
    unittest.main()
