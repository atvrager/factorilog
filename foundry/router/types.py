"""Data types and constants for the 3D multi-layer maze router.

Defines grid coordinates, layer assignments, graph nodes, and net specifications.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

from dataclasses import dataclass

# Factorio entity direction constants
DIRECTION_NORTH = 0
DIRECTION_EAST = 4
DIRECTION_SOUTH = 8
DIRECTION_WEST = 12

ALL_DIRECTIONS = (DIRECTION_NORTH, DIRECTION_EAST, DIRECTION_SOUTH, DIRECTION_WEST)

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

# Belt lane identifiers
LANE_LEFT = 0
LANE_RIGHT = 1
LANE_BOTH = 2

# Layer identifiers
LAYER_SURFACE = 0


@dataclass(frozen=True, slots=True)
class RouteNode:
    """A directed state node in the 5D routing graph."""

    x: int
    y: int
    z: int
    heading: int
    lane: int = LANE_BOTH

    @property
    def pos(self) -> tuple[int, int]:
        """Return the 2D surface position."""
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class RouteEdge:
    """A directed transition between two routing nodes."""

    start: RouteNode
    end: RouteNode
    move_type: str
    tier_name: str
    cost: float


@dataclass
class RouterNetSpec:
    """A logical net to route between source and sink."""

    name: str
    source: tuple[int, int]
    sink: tuple[int, int]
    commodity: str = "item"
    is_fluid: bool = False
    lane: int = LANE_BOTH
    tier_preference: str | None = None
    start_heading: int | None = None
    end_heading: int | None = None
    terminal_heading: int | None = None
    source_inward_dir: int | None = None
    sink_inward_dir: int | None = None


@dataclass
class RouteResult:
    """Result of routing a single net."""

    net_name: str
    nodes: list[RouteNode]
    edges: list[RouteEdge]
    cost: float
    is_success: bool = True
    error_message: str | None = None
