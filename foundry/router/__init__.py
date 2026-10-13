"""Dual-lane directed 3D maze router module.

Exports routing orchestrator, grid representation, and net types.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

from foundry.router.blueprint_builder import build_blueprint_entities
from foundry.router.grid import RoutingGrid
from foundry.router.maze_router import DualLaneMazeRouter
from foundry.router.pathfinder import route_all_nets, route_single_net
from foundry.router.types import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    LANE_BOTH,
    LANE_LEFT,
    LANE_RIGHT,
    RouteEdge,
    RouteNode,
    RouteResult,
    RouterNetSpec,
)

__all__ = [
    "DIRECTION_EAST",
    "DIRECTION_NORTH",
    "DIRECTION_SOUTH",
    "DIRECTION_WEST",
    "LANE_BOTH",
    "LANE_LEFT",
    "LANE_RIGHT",
    "DualLaneMazeRouter",
    "RouteEdge",
    "RouteNode",
    "RouteResult",
    "RouterNetSpec",
    "RoutingGrid",
    "build_blueprint_entities",
    "route_all_nets",
    "route_single_net",
]
