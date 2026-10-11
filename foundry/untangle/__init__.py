"""DEF routing untangler package."""

from __future__ import annotations

try:
    from foundry.untangle.channels import (
        separate_overlapping_channel_routes,
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
        separate_overlapping_channel_routes,
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
