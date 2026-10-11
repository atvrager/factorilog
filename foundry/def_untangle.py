"""DEF routing untangler package facade.

Re-exports all untangle passes and utilities from the foundry.untangle package.
"""

from __future__ import annotations

try:
    from foundry.untangle import (
        SpatialWireIndex,
        _get_net_fluid,
        _is_fluid_net,
        eliminate_staircase_jogs,
        eliminate_u_detours,
        fix_reverse_splitter_inputs,
        get_component_box,
        relocate_blocked_splitter_inputs,
        separate_overlapping_channel_routes,
        shift_congested_pin_turns,
        shortcut_backward_splitter_detours,
        simplify_orthogonal_routes,
        straighten_collinear_routes,
        untangle_interlocking_corners,
        untangle_machine_inputs,
        untangle_merge_tree_sinks,
        untangle_merger_inputs,
        untangle_shared_merger_tracks,
        untangle_splitter_outputs,
        wires_touch,
    )
except ImportError:
    from untangle import (
        SpatialWireIndex,
        _get_net_fluid,
        _is_fluid_net,
        eliminate_staircase_jogs,
        eliminate_u_detours,
        fix_reverse_splitter_inputs,
        get_component_box,
        relocate_blocked_splitter_inputs,
        separate_overlapping_channel_routes,
        shift_congested_pin_turns,
        shortcut_backward_splitter_detours,
        simplify_orthogonal_routes,
        straighten_collinear_routes,
        untangle_interlocking_corners,
        untangle_machine_inputs,
        untangle_merge_tree_sinks,
        untangle_merger_inputs,
        untangle_shared_merger_tracks,
        untangle_splitter_outputs,
        wires_touch,
    )

__all__ = [
    "SpatialWireIndex",
    "_get_net_fluid",
    "_is_fluid_net",
    "eliminate_staircase_jogs",
    "eliminate_u_detours",
    "fix_reverse_splitter_inputs",
    "get_component_box",
    "relocate_blocked_splitter_inputs",
    "separate_overlapping_channel_routes",
    "shift_congested_pin_turns",
    "shortcut_backward_splitter_detours",
    "simplify_orthogonal_routes",
    "straighten_collinear_routes",
    "untangle_interlocking_corners",
    "untangle_machine_inputs",
    "untangle_merge_tree_sinks",
    "untangle_merger_inputs",
    "untangle_shared_merger_tracks",
    "untangle_splitter_outputs",
    "wires_touch",
]
