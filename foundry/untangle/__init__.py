"""DEF routing untangler package."""

from __future__ import annotations

from typing import Any

JsonDict = dict[str, Any]
DEFAULT_MAX_ITERATIONS = 3

try:
    from foundry.untangle.channels import (
        route_unrouted_signal_nets,
        separate_overlapping_channel_routes,
        shift_m2_segment,
        untangle_congested_turn_crossings,
        untangle_interlocking_corners,
        untangle_merge_tree_sinks,
    )
    from foundry.untangle.jogs import (
        eliminate_staircase_jogs,
        eliminate_u_detours,
        shift_congested_pin_turns,
        shortcut_backward_splitter_detours,
        simplify_orthogonal_routes,
        straighten_collinear_routes,
    )
    from foundry.untangle.pins import (
        fix_reverse_splitter_inputs,
        relocate_blocked_splitter_inputs,
        untangle_machine_inputs,
        untangle_merger_inputs,
        untangle_shared_merger_tracks,
        untangle_splitter_outputs,
    )
    from foundry.untangle.spatial import (
        SpatialWireIndex,
        _get_net_fluid,
        _is_fluid_net,
        get_component_box,
        wires_touch,
    )
except ImportError:
    from untangle.channels import (
        route_unrouted_signal_nets,
        separate_overlapping_channel_routes,
        shift_m2_segment,
        untangle_congested_turn_crossings,
        untangle_interlocking_corners,
        untangle_merge_tree_sinks,
    )
    from untangle.jogs import (
        eliminate_staircase_jogs,
        eliminate_u_detours,
        shift_congested_pin_turns,
        shortcut_backward_splitter_detours,
        simplify_orthogonal_routes,
        straighten_collinear_routes,
    )
    from untangle.pins import (
        fix_reverse_splitter_inputs,
        relocate_blocked_splitter_inputs,
        untangle_machine_inputs,
        untangle_merger_inputs,
        untangle_shared_merger_tracks,
        untangle_splitter_outputs,
    )
    from untangle.spatial import (
        SpatialWireIndex,
        _get_net_fluid,
        _is_fluid_net,
        get_component_box,
        wires_touch,
    )


def untangle_def(
    def_data: JsonDict,
    netlist_data: JsonDict | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    pins_only: bool = False,
) -> None:
    """Execute the three-phase DEF routing untangle pipeline.

    Phase 1: Pin and Port Untangling (topological)
        Swap symmetric splitter outputs, merger/balancer inputs, and commutative
        machine inputs before altering wire geometry.
    Phase 2: Geometric Route Simplification (jog elimination)
        Straighten collinear routes, simplify doglegs, and eliminate staircase
        and U-detours.
    Phase 3: Physical Congestion and Clearance (track shifting)
        Relocate blocked splitter inputs, un-interlock corners, shift congested
        pin turns, and un-congest turn crossings.

    Phases 2 and 3 iterate for max_iterations until wire adjustments converge.
    """
    if netlist_data is not None:
        def_data["_netlist_data"] = netlist_data
    else:
        netlist_data = def_data.get("_netlist_data")

    # Route any unrouted point-to-point signal nets left by OpenROAD
    route_unrouted_signal_nets(def_data, netlist_data)

    # Phase 1: Topological pin and port untangling
    untangle_shared_merger_tracks(def_data)
    untangle_merger_inputs(def_data)
    untangle_splitter_outputs(def_data)
    if netlist_data is not None:
        untangle_machine_inputs(def_data, netlist_data)

    if pins_only:
        fix_reverse_splitter_inputs(def_data)
        relocate_blocked_splitter_inputs(def_data)
        return

    # Phase 2 & 3: Iterative geometric simplification and physical clearance loop
    for _ in range(max_iterations):
        simplify_orthogonal_routes(def_data)
        straighten_collinear_routes(def_data)
        eliminate_staircase_jogs(def_data)
        eliminate_u_detours(def_data)
        untangle_merger_inputs(def_data)
        untangle_splitter_outputs(def_data)
        fix_reverse_splitter_inputs(def_data)
        relocate_blocked_splitter_inputs(def_data)
        untangle_interlocking_corners(def_data)
        shift_congested_pin_turns(def_data)
        separate_overlapping_channel_routes(def_data, netlist_data)
        untangle_congested_turn_crossings(def_data, netlist_data)


__all__ = [
    "SpatialWireIndex",
    "_get_net_fluid",
    "_is_fluid_net",
    "eliminate_staircase_jogs",
    "eliminate_u_detours",
    "fix_reverse_splitter_inputs",
    "get_component_box",
    "relocate_blocked_splitter_inputs",
    "route_unrouted_signal_nets",
    "separate_overlapping_channel_routes",
    "shift_congested_pin_turns",
    "shift_m2_segment",
    "shortcut_backward_splitter_detours",
    "simplify_orthogonal_routes",
    "straighten_collinear_routes",
    "untangle_congested_turn_crossings",
    "untangle_def",
    "untangle_interlocking_corners",
    "untangle_machine_inputs",
    "untangle_merge_tree_sinks",
    "untangle_merger_inputs",
    "untangle_shared_merger_tracks",
    "untangle_splitter_outputs",
    "wires_touch",
]
