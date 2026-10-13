"""Factorio native simulation engine integration package."""

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

__all__ = [
    "FactorioNativeEngine",
    "FactorioSymbolFinder",
    "make_fluid_buffer",
    "make_transport_line_with_items",
    "read_fluid_buffer",
    "read_item_distances",
    "read_transport_line",
    "resolve_factorio_binary",
    "simulate_pipe_network",
    "simulate_splitter_step",
    "validate_underground_belt_span",
]
