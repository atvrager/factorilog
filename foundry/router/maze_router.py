"""High-level dual-lane maze router orchestrator.

Coordinates 3D multi-layer PathFinder routing and native layout verification.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

from typing import Any

from foundry.native_engine.engine import FactorioNativeEngine
from foundry.native_engine.verifier import (
    NativeLayoutVerifier,
    NetSpec,
    VerificationResult,
)
from foundry.router.blueprint_builder import build_blueprint_entities
from foundry.router.grid import RoutingGrid
from foundry.router.pathfinder import route_all_nets
from foundry.router.types import RouteResult, RouterNetSpec


class DualLaneMazeRouter:
    """Orchestrates 3D multi-layer routing with closed-loop verification."""

    def __init__(
        self,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
        engine: FactorioNativeEngine | None = None,
        prototypes: dict[str, Any] | None = None,
        obstacles: set[tuple[int, int]] | None = None,
        component_headings: dict[tuple[int, int], int] | None = None,
        no_hood_tiles: set[tuple[int, int]] | None = None,
    ) -> None:
        """Initialize the router with spatial bounds and optional engine."""
        self.min_x = min_x
        self.max_x = max_x
        self.min_y = min_y
        self.max_y = max_y
        self.engine = engine
        self.prototypes = prototypes
        self.obstacles = set(obstacles) if obstacles else set()
        self.component_headings = dict(component_headings) if component_headings else {}
        self.no_hood_tiles = set(no_hood_tiles) if no_hood_tiles else set()

    def route(
        self,
        nets: list[RouterNetSpec],
        max_iterations: int = 80,
        verify: bool = True,
        ticks: int = 60,
    ) -> tuple[dict[str, RouteResult], list[dict[str, Any]], VerificationResult | None]:
        """Route all specified nets and verify the resulting blueprint."""
        grid = RoutingGrid(
            min_x=self.min_x,
            max_x=self.max_x,
            min_y=self.min_y,
            max_y=self.max_y,
            prototypes=self.prototypes,
            obstacles=self.obstacles,
            component_headings=self.component_headings,
            no_hood_tiles=self.no_hood_tiles,
        )
        grid.register_terminals(nets)

        # Execute PathFinder routing
        routes = route_all_nets(nets, grid, max_iterations=max_iterations)

        # Assemble physical blueprint entities
        entities = build_blueprint_entities(routes, nets)

        verification_result: VerificationResult | None = None
        if verify and entities:
            verifier = NativeLayoutVerifier(self.engine, prototypes=self.prototypes)
            verifier_nets = [
                NetSpec(
                    name=n.name,
                    commodity=n.commodity,
                    source_pos=(n.source[0] + 0.5, n.source[1] + 0.5),
                    sink_pos=(n.sink[0] + 0.5, n.sink[1] + 0.5),
                    is_fluid=n.is_fluid,
                )
                for n in nets
            ]
            verification_result = verifier.verify_blueprint(
                entities,
                nets=verifier_nets,
                ticks=ticks,
            )

        return routes, entities, verification_result
