"""Canonical physical belt patterns and routing primitives for Factorio PDK."""

from __future__ import annotations

import enum
from dataclasses import dataclass

# Factorio 2.0 cardinal directions.
DIRECTION_NORTH = 0
DIRECTION_EAST = 4
DIRECTION_SOUTH = 8
DIRECTION_WEST = 12

DIR_STEPS: dict[int, tuple[int, int]] = {
    DIRECTION_NORTH: (0, -1),
    DIRECTION_EAST: (1, 0),
    DIRECTION_SOUTH: (0, 1),
    DIRECTION_WEST: (-1, 0),
}

OPPOSITE_DIR: dict[int, int] = {
    DIRECTION_NORTH: DIRECTION_SOUTH,
    DIRECTION_EAST: DIRECTION_WEST,
    DIRECTION_SOUTH: DIRECTION_NORTH,
    DIRECTION_WEST: DIRECTION_EAST,
}

# Maximum underground span in tiles between hoods.
# Values from prototype dump: 5 for yellow, 7 for red, 9 for blue.
MAX_UNDERGROUND_SPAN: dict[str, int] = {
    "underground-belt": 5,
    "fast-underground-belt": 7,
    "express-underground-belt": 9,
    "turbo-underground-belt": 11,
}


class PatternType(enum.Enum):
    """Enumeration of legal physical belt routing patterns."""

    STRAIGHT = "straight"
    TURN = "turn"
    SIDELOAD = "sideload"
    DUAL_MERGE = "dual_merge"
    UNDERGROUND_PAIR = "underground_pair"
    SPLITTER_SPLIT = "splitter_split"
    SPLITTER_MERGE = "splitter_merge"


@dataclass(frozen=True)
class BeltPattern:
    """A verified physical routing pattern on the tile grid."""

    pattern_type: PatternType
    direction: int
    inputs: list[tuple[int, int, int]]  # (dx, dy, required_in_dir)
    outputs: list[tuple[int, int, int]]  # (dx, dy, out_dir)
    lane_mapping: dict[int, int]  # input_lane -> output_lane
    max_throughput: float  # items per second

    def is_compatible(
        self,
        in_dirs: list[int],
        out_dir: int,
    ) -> bool:
        """Check if incoming and outgoing directions match this pattern."""
        expected_in = [d for _, _, d in self.inputs]
        return sorted(in_dirs) == sorted(expected_in) and out_dir == self.direction


def get_straight_pattern(direction: int, belt_name: str = "transport-belt") -> BeltPattern:
    """Create a straight belt pattern for a cardinal direction."""
    dx, dy = DIR_STEPS[direction]
    speed_factor = 15.0 if belt_name == "transport-belt" else 30.0
    return BeltPattern(
        pattern_type=PatternType.STRAIGHT,
        direction=direction,
        inputs=[(-dx, -dy, direction)],
        outputs=[(dx, dy, direction)],
        lane_mapping={1: 1, 2: 2},
        max_throughput=speed_factor,
    )


def get_turn_pattern(in_dir: int, out_dir: int, belt_name: str = "transport-belt") -> BeltPattern:
    """Create a 90-degree corner turn pattern."""
    in_dx, in_dy = DIR_STEPS[in_dir]
    out_dx, out_dy = DIR_STEPS[out_dir]
    speed_factor = 15.0 if belt_name == "transport-belt" else 30.0
    return BeltPattern(
        pattern_type=PatternType.TURN,
        direction=out_dir,
        inputs=[(-in_dx, -in_dy, in_dir)],
        outputs=[(out_dx, out_dy, out_dir)],
        lane_mapping={1: 1, 2: 2},
        max_throughput=speed_factor,
    )


def get_sideload_pattern(
    main_dir: int, side_dir: int, belt_name: str = "transport-belt"
) -> BeltPattern:
    """Create a sideloading pattern onto a straight belt."""
    main_dx, main_dy = DIR_STEPS[main_dir]
    side_dx, side_dy = DIR_STEPS[side_dir]
    speed_factor = 15.0 if belt_name == "transport-belt" else 30.0
    return BeltPattern(
        pattern_type=PatternType.SIDELOAD,
        direction=main_dir,
        inputs=[(-main_dx, -main_dy, main_dir), (-side_dx, -side_dy, side_dir)],
        outputs=[(main_dx, main_dy, main_dir)],
        lane_mapping={1: 1, 2: 1},  # Sideload drops onto near lane
        max_throughput=speed_factor,
    )


def get_dual_merge_pattern(
    left_in_dir: int, right_in_dir: int, out_dir: int, belt_name: str = "transport-belt"
) -> BeltPattern:
    """Create a dual perpendicular entry merge pattern."""
    l_dx, l_dy = DIR_STEPS[left_in_dir]
    r_dx, r_dy = DIR_STEPS[right_in_dir]
    out_dx, out_dy = DIR_STEPS[out_dir]
    speed_factor = 15.0 if belt_name == "transport-belt" else 30.0
    return BeltPattern(
        pattern_type=PatternType.DUAL_MERGE,
        direction=out_dir,
        inputs=[(-l_dx, -l_dy, left_in_dir), (-r_dx, -r_dy, right_in_dir)],
        outputs=[(out_dx, out_dy, out_dir)],
        lane_mapping={1: 1, 2: 2},
        max_throughput=speed_factor,
    )


def get_underground_pair_pattern(
    direction: int, span: int, tier_name: str = "underground-belt"
) -> BeltPattern:
    """Create an underground pair pattern with entrance and exit hoods."""
    dx, dy = DIR_STEPS[direction]
    speed_factor = 15.0 if tier_name == "underground-belt" else 30.0
    return BeltPattern(
        pattern_type=PatternType.UNDERGROUND_PAIR,
        direction=direction,
        inputs=[(-dx, -dy, direction)],
        outputs=[(dx * (span + 1), dy * (span + 1), direction)],
        lane_mapping={1: 1, 2: 2},
        max_throughput=speed_factor,
    )
