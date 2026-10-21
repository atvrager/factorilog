"""Routing grid representation for 3D multi-layer routing.

Maintains multi-layer occupancy, dynamic prototype layer registry, and congestion state.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

from typing import Any

from foundry.native_engine.verifier import load_game_prototypes
from foundry.router.types import (
    DIR_STEPS,
    LANE_BOTH,
    LAYER_SURFACE,
    OPPOSITE_DIR,
    PERPENDICULAR_DIRS,
    PrototypeMap,
    RouteEdge,
    RouteNode,
    RouteResult,
    RouterNetSpec,
)

HIGHWAY_SUFFIX = "_hwy"
HIERARCHY_SEPARATORS = ("_export_", "_import_", "_hwy_", "_blk_")
DEFAULT_CONGESTION_BETA = 0.5
DEFAULT_CONGESTION_GAMMA = 1.5
DEFAULT_TIER_COST_STEP = 1.5
PIPE_LAYER_BASE_ID = 10
LOOKAHEAD_STEPS = (1, 2, 3)
SURFACE_SHARING_COST = 8.0
COLLINEAR_SHARING_COST = 16.0
AXIS_DIRECTION_MODULO = 8
ORTHOGONAL_OFFSETS = ((0, 1), (0, -1), (1, 0), (-1, 0))
DUAL_LANE_CAPACITY = 2

_BASE_NET_CACHE: dict[str, str] = {}


def base_net_name(net_name: str) -> str:
    """Return the base logical net name by stripping driver/sink and hierarchy suffixes."""
    cached = _BASE_NET_CACHE.get(net_name)
    if cached is not None:
        return cached
    orig = net_name
    net_name = net_name.removesuffix(HIGHWAY_SUFFIX)
    for sep in HIERARCHY_SEPARATORS:
        if sep in net_name:
            net_name = net_name.split(sep)[0]
    idx = net_name.find("_d")
    if idx != -1 and "_s" in net_name[idx:]:
        res = net_name[:idx]
        _BASE_NET_CACHE[orig] = res
        return res
    if "_mst_" in net_name:
        res = net_name.split("_mst_")[0]
        _BASE_NET_CACHE[orig] = res
        return res
    _BASE_NET_CACHE[orig] = net_name
    return net_name


def _step_sign(delta: int) -> int:
    """Return 1, -1, or 0 for the sign of delta."""
    return 1 if delta > 0 else (-1 if delta < 0 else 0)


class RoutingGrid:
    """Manages spatial occupancy and congestion costs for 3D routing."""

    def __init__(
        self,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
        prototypes: PrototypeMap | None = None,
        obstacles: set[tuple[int, int]] | None = None,
        component_headings: dict[tuple[int, int], int] | None = None,
        no_hood_tiles: set[tuple[int, int]] | None = None,
        fluid_port_approaches: dict[tuple[int, int], set[str]] | None = None,
    ) -> None:
        """Initialize the routing grid with dimensions and prototype parameters."""
        self.min_x = min_x
        self.max_x = max_x
        self.min_y = min_y
        self.max_y = max_y

        self.prototypes: dict[str, Any] = (
            dict(prototypes) if prototypes is not None else load_game_prototypes()
        )
        self.fixed_obstacles: set[tuple[int, int]] = set(obstacles) if obstacles else set()

        self.width = self.max_x - self.min_x + 1
        self.height = self.max_y - self.min_y + 1
        self.grid_size = self.width * self.height
        self.obstacle_bytes = bytearray(self.grid_size)
        for ox, oy in self.fixed_obstacles:
            if self.min_x <= ox <= self.max_x and self.min_y <= oy <= self.max_y:
                self.obstacle_bytes[(oy - self.min_y) * self.width + (ox - self.min_x)] = 1

        # Dynamic layer configuration
        self.belt_tiers: dict[int, dict[str, Any]] = self._init_belt_tiers()
        self.pipe_tiers: dict[int, dict[str, Any]] = self._init_pipe_tiers()
        self.belt_tier_keys: tuple[int, ...] = tuple(self.belt_tiers.keys())
        self.pipe_tier_keys: tuple[int, ...] = tuple(self.pipe_tiers.keys())
        self.max_belt_span: int = max((t["max_span"] for t in self.belt_tiers.values()), default=0)
        self.max_pipe_span: int = max((t["max_span"] for t in self.pipe_tiers.values()), default=0)

        # Occupancy tracking: layer -> (x, y) -> net_name
        self.layer_occupancy: dict[int, dict[tuple[int, int], str]] = {
            LAYER_SURFACE: {},
        }
        for z in self.belt_tiers:
            self.layer_occupancy[z] = {}
        for z in self.pipe_tiers:
            self.layer_occupancy[z] = {}

        # Lane-level occupancy on surface: (x, y) -> {lane_idx: net_name}
        self.surface_lanes: dict[tuple[int, int], dict[int, str]] = {}
        self.surface_occupancy: dict[tuple[int, int], set[str]] = {}
        self.surface_headings: dict[tuple[int, int], int] = {}
        self.surface_net_headings: dict[tuple[int, int], dict[str, int]] = {}
        self.net_is_fluid: dict[str, bool] = {}
        self.net_commodity: dict[str, str] = {}
        self._norm_commodity: dict[str, str] = {}

        # Congestion tracking
        self.usage_count: dict[tuple[int, int, int], int] = {}
        self.node_occupants: dict[tuple[int, int, int], set[str]] = {}
        self.historical_cost: dict[tuple[int, int, int], float] = {}
        self.present_cost: dict[tuple[int, int, int], float] = {}
        self.congestion_gamma: float = DEFAULT_CONGESTION_GAMMA

        # Reserved terminals: (x, y) -> set of allowed net names
        self.reserved_terminals: dict[tuple[int, int], set[str]] = {}
        self.reserved_terminal_bases: dict[tuple[int, int], set[str]] = {}
        self.terminal_headings: dict[tuple[int, int], int] = {}
        self.fluid_terminal_inward_dir: dict[tuple[int, int], int] = {}
        self.fluid_port_approaches: dict[tuple[int, int], set[str]] = (
            {k: set(v) for k, v in fluid_port_approaches.items()} if fluid_port_approaches else {}
        )
        self.component_headings: dict[tuple[int, int], int] = (
            dict(component_headings) if component_headings else {}
        )
        self.no_hood_tiles: set[tuple[int, int]] = set(no_hood_tiles) if no_hood_tiles else set()
        self.belt_sink_front_tiles: dict[tuple[int, int], set[str]] = {}

        # Active underground spans and hoods to prevent DRC-07 and DRC-15 collisions
        self.active_horiz_spans: dict[tuple[int, int], str] = {}
        self.active_vert_spans: dict[tuple[int, int], str] = {}
        self.active_belt_horiz_spans: dict[tuple[int, int], str] = {}
        self.active_belt_vert_spans: dict[tuple[int, int], str] = {}
        self.active_pipe_horiz_spans: dict[tuple[int, int], str] = {}
        self.active_pipe_vert_spans: dict[tuple[int, int], str] = {}
        self.active_hoods: dict[tuple[int, int], str] = {}
        self.active_hood_types: dict[tuple[int, int], tuple[str, int]] = {}

        # Optional active bounding box for search pruning: (min_x, min_y, max_x, max_y)
        self.active_bbox: tuple[int, int, int, int] | None = None
        self.needs_wide_bbox: set[str] = set()

        # Stored net routes: net_name -> RouteResult
        self.net_routes: dict[str, RouteResult] = {}
        self.has_congestion: bool = False

    def is_net_fluid(self, net_name: str) -> bool:
        """Return True if the net is registered as a fluid commodity net."""
        res = self.net_is_fluid.get(net_name)
        if res is not None:
            return res
        res = self.net_is_fluid.get(base_net_name(net_name), False)
        self.net_is_fluid[net_name] = res
        return res

    def get_net_commodity(self, net_name: str) -> str:
        """Return the normalized commodity name for a net."""
        norm = self._norm_commodity.get(net_name)
        if norm is not None:
            return norm
        b_name = base_net_name(net_name)
        comm = self.net_commodity.get(net_name) or self.net_commodity.get(b_name)
        norm = comm.replace("_", "-") if comm else b_name.split("__")[0].replace("_", "-")
        self._norm_commodity[net_name] = norm
        return norm

    def get_surface_fluid_info(
        self, pos: tuple[int, int]
    ) -> tuple[bool, str, tuple[int, int] | None]:
        """Return (is_fluid, commodity, mouth_pos) for the surface entity at pos."""
        occ = self.surface_occupancy.get(pos)
        if not occ:
            return (False, "", None)

        fluid_net = next((n for n in occ if self.is_net_fluid(n)), None)
        if not fluid_net:
            return (False, "", None)

        comm = self.get_net_commodity(fluid_net)
        if pos in self.active_hood_types:
            h_type, h_dir = self.active_hood_types[pos]
            mouth_dir = OPPOSITE_DIR.get(h_dir, 0) if h_type == "input" else h_dir
            mdx, mdy = DIR_STEPS[mouth_dir]
            return (True, comm, (pos[0] + mdx, pos[1] + mdy))

        return (True, comm, None)

    def _register_fluid_terminal_approaches(self, net: RouterNetSpec) -> None:
        """Register inward directions and approach tiles for a fluid net's terminals."""
        my_comm = self.get_net_commodity(net.name)
        for pos, inward_dir in (
            (net.source, net.source_inward_dir),
            (net.sink, net.sink_inward_dir),
        ):
            if inward_dir is not None:
                self.fluid_terminal_inward_dir[pos] = inward_dir
                odx, ody = DIR_STEPS[inward_dir]
                out_pos = (pos[0] - odx, pos[1] - ody)
                self.fluid_port_approaches.setdefault(out_pos, set()).add(my_comm)

    def register_terminals(self, nets: list[RouterNetSpec]) -> None:
        """Register source and sink terminals for all nets to prevent cross-net encroachment."""
        for net in nets:
            b_name = base_net_name(net.name)
            self.net_is_fluid[net.name] = net.is_fluid
            self.net_is_fluid[b_name] = net.is_fluid
            self.net_commodity[net.name] = net.commodity
            self.net_commodity[b_name] = net.commodity
            self.reserved_terminals.setdefault(net.source, set()).add(net.name)
            self.reserved_terminals.setdefault(net.sink, set()).add(net.name)
            self.reserved_terminal_bases.setdefault(net.source, set()).add(b_name)
            self.reserved_terminal_bases.setdefault(net.sink, set()).add(b_name)
            if net.start_heading is not None:
                self.terminal_headings[net.source] = net.start_heading
            if net.end_heading is not None:
                self.terminal_headings[net.sink] = net.end_heading
            if net.is_fluid:
                self._register_fluid_terminal_approaches(net)

    def _has_matching_fluid_terminal(self, pos: tuple[int, int], my_comm: str) -> bool:
        """Return True if pos has at least one reserved fluid terminal matching my_comm."""
        return any(
            self.is_net_fluid(tn) and self.get_net_commodity(tn) == my_comm
            for tn in self.reserved_terminals.get(pos, ())
        )

    def _has_conflicting_fluid_terminal(self, pos: tuple[int, int], my_comm: str) -> bool:
        """Return True if pos has a reserved fluid terminal with a commodity other than my_comm."""
        return any(
            self.is_net_fluid(tn) and self.get_net_commodity(tn) != my_comm
            for tn in self.reserved_terminals.get(pos, ())
        )

    def _has_neighbor_fluid_hood_pointing_at(self, pos: tuple[int, int]) -> bool:
        """Return True if an adjacent fluid hood's surface mouth points into pos."""
        for odx, ody in ORTHOGONAL_OFFSETS:
            nb = (pos[0] + odx, pos[1] + ody)
            if nb in self.active_hood_types and self.is_net_fluid(self.active_hoods.get(nb, "")):
                nb_type, nb_dir = self.active_hood_types[nb]
                nb_mouth_dir = OPPOSITE_DIR.get(nb_dir, 0) if nb_type == "input" else nb_dir
                nmdx, nmdy = DIR_STEPS[nb_mouth_dir]
                if (nb[0] + nmdx, nb[1] + nmdy) == pos:
                    return True
        return False

    def _can_place_fluid_hood(
        self,
        pos: tuple[int, int],
        net_name: str,
        heading: int,
        is_output: bool,
    ) -> bool:
        """Check fluid-specific mouth and approach constraints for placing a hood at pos."""
        my_comm = self.get_net_commodity(net_name)
        if pos in self.fluid_port_approaches and my_comm not in self.fluid_port_approaches[pos]:
            return False
        if self._has_neighbor_fluid_hood_pointing_at(pos):
            return False
        mouth_dir = heading if is_output else OPPOSITE_DIR.get(heading, 0)
        if (
            pos in self.fluid_terminal_inward_dir
            and mouth_dir != self.fluid_terminal_inward_dir[pos]
        ):
            return False
        mdx, mdy = DIR_STEPS[mouth_dir]
        mouth_pos = (pos[0] + mdx, pos[1] + mdy)
        m_is_fluid, m_comm, _m_mouth = self.get_surface_fluid_info(mouth_pos)
        if m_is_fluid and m_comm != my_comm:
            return False
        return not self._has_conflicting_fluid_terminal(mouth_pos, my_comm)

    def _is_heading_present_at(self, tile: tuple[int, int], target_heading: int) -> bool:
        """Return True if any belt entity, terminal, or component at tile has target_heading."""
        return (
            self.surface_headings.get(tile) == target_heading
            or target_heading in self.surface_net_headings.get(tile, {}).values()
            or self.terminal_headings.get(tile) == target_heading
            or self.component_headings.get(tile) == target_heading
        )

    def _can_place_belt_hood(
        self,
        pos: tuple[int, int],
        b_name: str,
        heading: int,
        is_output: bool,
    ) -> bool:
        """Check DRC-03 side and rear clearance around a belt underground hood at pos."""
        for s_dir in PERPENDICULAR_DIRS[heading]:
            sdx, sdy = DIR_STEPS[s_dir]
            side_nb = (pos[0] - sdx, pos[1] - sdy)
            if self._is_heading_present_at(side_nb, s_dir):
                return False
        if not is_output:
            return True
        if pos in self.reserved_terminal_bases and b_name not in self.reserved_terminal_bases[pos]:
            return False
        dx, dy = DIR_STEPS[heading]
        rear_pos = (pos[0] - dx, pos[1] - dy)
        if self._is_heading_present_at(rear_pos, heading):
            return False
        return not (
            rear_pos in self.reserved_terminal_bases
            and b_name not in self.reserved_terminal_bases[rear_pos]
        )

    def _is_hood_tile_blocked(
        self,
        pos: tuple[int, int],
        net_name: str,
        is_fluid: bool,
    ) -> bool:
        """Return True if pos cannot host any underground hood for net_name."""
        if (
            pos in self.no_hood_tiles
            or self.is_obstacle(pos[0], pos[1])
            or pos in self.active_hoods
            or pos in self.surface_occupancy
        ):
            return True
        if pos in self._domain_span_map(is_fluid, True) or pos in self._domain_span_map(
            is_fluid, False
        ):
            return True
        if pos not in self.reserved_terminals:
            return False
        if is_fluid:
            return not self._has_matching_fluid_terminal(pos, self.get_net_commodity(net_name))
        return net_name not in self.reserved_terminals[pos]

    def can_place_hood(
        self,
        pos: tuple[int, int],
        net_name: str,
        heading: int | None = None,
        is_output: bool = False,
    ) -> bool:
        """Check if an underground hood can be placed without colliding with active spans."""
        is_fluid = self.is_net_fluid(net_name)
        if self._is_hood_tile_blocked(pos, net_name, is_fluid):
            return False
        if heading is None:
            return True
        if is_fluid:
            return self._can_place_fluid_hood(pos, net_name, heading, is_output)
        return self._can_place_belt_hood(pos, base_net_name(net_name), heading, is_output)

    def _is_tile_occupied_by_other_base(self, pt: tuple[int, int], b_name: str) -> bool:
        """Return True if surface_occupancy at pt contains a net from a different base net."""
        return any(base_net_name(n) != b_name for n in self.surface_occupancy.get(pt, ()))

    def _is_step_blocked_ahead(
        self,
        pt: tuple[int, int],
        b_name: str,
        net_name: str,
        lane: int,
    ) -> bool:
        """Return True if surface tile pt ahead of a belt/pipe is obstructed or congested."""
        tx, ty = pt
        idx = (ty - self.min_y) * self.width + (tx - self.min_x)
        if (
            self.obstacle_bytes[idx] == 1
            or pt in self.active_hoods
            or (
                pt in self.reserved_terminal_bases
                and b_name not in self.reserved_terminal_bases[pt]
            )
            or self._is_tile_occupied_by_other_base(pt, b_name)
        ):
            return True
        if self.has_congestion and self.historical_cost.get((tx, ty, LAYER_SURFACE), 0.0) > 0.0:
            return True
        return not self.can_occupy_surface(pt, net_name, lane)

    def _has_clear_exit_within_span(
        self,
        x: int,
        y: int,
        dx: int,
        dy: int,
        max_span: int,
    ) -> bool:
        """Return True if at least one non-obstacle exit tile exists within [2, max_span]."""
        for step in range(2, max_span + 1):
            ex = x + dx * step
            ey = y + dy * step
            if not (self.min_x <= ex <= self.max_x and self.min_y <= ey <= self.max_y):
                break
            e_idx = (ey - self.min_y) * self.width + (ex - self.min_x)
            if self.obstacle_bytes[e_idx] == 0:
                return True
        return False

    def should_consider_underground(
        self,
        x: int,
        y: int,
        dx: int,
        dy: int,
        _max_span: int,
        net_name: str,
        lane: int,
        target_pos: tuple[int, int] | None = None,
    ) -> bool:
        """Check if obstacle or blockage 1-3 steps ahead warrants diving underground."""
        b_name = base_net_name(net_name)
        blocked_ahead = False
        for s in LOOKAHEAD_STEPS:
            tx = x + dx * s
            ty = y + dy * s
            if not (self.min_x <= tx <= self.max_x and self.min_y <= ty <= self.max_y):
                return False
            pt = (tx, ty)
            if target_pos is not None and pt == target_pos:
                return False
            if self._is_step_blocked_ahead(pt, b_name, net_name, lane):
                blocked_ahead = True
                break
        if not blocked_ahead:
            return False
        return self._has_clear_exit_within_span(x, y, dx, dy, _max_span)

    def can_leap_span(
        self,
        span_coords: list[tuple[int, int]],
        is_horiz: bool,
        _net_name: str,
    ) -> bool:
        """Check if an underground leap avoids collisions with hoods and parallel spans."""
        axis_spans = self.active_horiz_spans if is_horiz else self.active_vert_spans
        return all(pt not in self.active_hoods and pt not in axis_spans for pt in span_coords)

    def _init_belt_tiers(self) -> dict[int, dict[str, Any]]:
        """Register underground belt tiers dynamically from prototypes."""
        result: dict[int, dict[str, Any]] = {}
        ug_dict = self.prototypes.get("underground-belt", {})
        belt_dict = self.prototypes.get("transport-belt", {})

        valid_ugs = [
            (name, proto)
            for name, proto in ug_dict.items()
            if "speed" in proto and "max_distance" in proto and "turbo" not in name
        ]
        sorted_ugs = sorted(valid_ugs, key=lambda kv: float(kv[1]["speed"]))

        for idx, (ug_name, ug_proto) in enumerate(sorted_ugs):
            layer_id = idx + 1
            speed = float(ug_proto["speed"])
            span = int(ug_proto["max_distance"])
            matching_belt = next(
                (
                    b_name
                    for b_name, b_proto in belt_dict.items()
                    if b_proto.get("related_underground_belt") == ug_name
                    or ("speed" in b_proto and float(b_proto["speed"]) == speed)
                ),
                None,
            )
            if not matching_belt:
                continue

            result[layer_id] = {
                "name": ug_name,
                "belt_name": matching_belt,
                "max_span": span,
                "tier_cost": idx * DEFAULT_TIER_COST_STEP,
                "speed": speed,
            }
        return result

    def _init_pipe_tiers(self) -> dict[int, dict[str, Any]]:
        """Register underground pipe tiers dynamically from prototypes."""
        result: dict[int, dict[str, Any]] = {}
        pg_dict = self.prototypes.get("pipe-to-ground", {})
        pipe_dict = self.prototypes.get("pipe", {})
        default_pipe = next(iter(pipe_dict.keys()), "pipe")

        sorted_pgs = sorted(pg_dict.items(), key=lambda kv: kv[0])
        for idx, (pg_name, pg_proto) in enumerate(sorted_pgs):
            layer_id = PIPE_LAYER_BASE_ID + idx
            fluid_box = pg_proto.get("fluid_box", {})
            conn_dist = next(
                (
                    int(conn["max_underground_distance"])
                    for conn in fluid_box.get("pipe_connections", [])
                    if conn.get("connection_type") == "underground"
                    and "max_underground_distance" in conn
                ),
                None,
            )
            if conn_dist is None:
                continue

            result[layer_id] = {
                "name": pg_name,
                "pipe_name": default_pipe,
                "max_span": conn_dist,
                "tier_cost": idx * DEFAULT_TIER_COST_STEP,
            }
        return result

    def is_in_bounds(self, x: int, y: int) -> bool:
        """Check if coordinates lie inside the grid boundaries."""
        if self.active_bbox is not None:
            bx0, by0, bx1, by1 = self.active_bbox
            return bx0 <= x <= bx1 and by0 <= y <= by1
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def is_obstacle(self, x: int, y: int) -> bool:
        """Check if a surface tile contains a fixed obstacle."""
        if self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y:
            return self.obstacle_bytes[(y - self.min_y) * self.width + (x - self.min_x)] == 1
        return False

    def get_tier_span(self, layer_id: int) -> int:
        """Return the maximum underground span for a layer."""
        if layer_id in self.belt_tiers:
            return self.belt_tiers[layer_id]["max_span"]
        if layer_id in self.pipe_tiers:
            return self.pipe_tiers[layer_id]["max_span"]
        return self.max_belt_span

    def get_tier_cost(self, layer_id: int) -> float:
        """Return the base upgrade cost for a layer."""
        if layer_id in self.belt_tiers:
            return self.belt_tiers[layer_id]["tier_cost"]
        if layer_id in self.pipe_tiers:
            return self.pipe_tiers[layer_id]["tier_cost"]
        return 0.0

    def get_node_cost(
        self, node: RouteNode, base_cost: float, net_name: str = "", lane: int = LANE_BOTH
    ) -> float:
        """Compute the dynamic traversal cost with negotiated congestion."""
        return self.get_fast_node_cost(
            node.x, node.y, node.z, base_cost, net_name, lane, heading=node.heading
        )

    def _has_collinear_other_net(
        self,
        pos: tuple[int, int],
        b_name: str,
        heading: int,
    ) -> bool:
        """Return True if another base net on pos shares the same axis as heading."""
        net_h = self.surface_net_headings.get(pos)
        if not net_h:
            return False
        h_mod = heading % AXIS_DIRECTION_MODULO
        return any(
            base_net_name(n) != b_name and (nh % AXIS_DIRECTION_MODULO) == h_mod
            for n, nh in net_h.items()
        )

    def _surface_sharing_penalty(
        self,
        pos: tuple[int, int],
        net_name: str,
        lane: int,
        heading: int | None,
    ) -> tuple[float, int]:
        """Compute (extra_cost, live_overuse) for sharing surface tile pos."""
        occ = self.surface_occupancy.get(pos)
        if not occ:
            return 0.0, 0
        b_name = base_net_name(net_name)
        if not self._is_tile_occupied_by_other_base(pos, b_name):
            return 0.0, 0
        lanes = self.surface_lanes.get(pos, {})
        if lane != LANE_BOTH and LANE_BOTH not in lanes and lane not in lanes:
            return 0.0, 0
        extra = SURFACE_SHARING_COST
        if heading is not None and self._has_collinear_other_net(pos, b_name, heading):
            extra += COLLINEAR_SHARING_COST
        return extra, len(occ)

    def get_fast_node_cost(
        self,
        x: int,
        y: int,
        z: int,
        base_cost: float,
        net_name: str = "",
        lane: int = LANE_BOTH,
        heading: int | None = None,
    ) -> float:
        """Compute traversal cost with negotiated congestion using flat lookups."""
        cost = base_cost
        live_overuse = 0
        if z == LAYER_SURFACE and net_name:
            extra, live_overuse = self._surface_sharing_penalty((x, y), net_name, lane, heading)
            cost += extra
        if not self.has_congestion:
            return cost
        key = (x, y, z)
        h = self.historical_cost.get(key, 0.0)
        if z == LAYER_SURFACE and net_name:
            p = (1.0 + self.congestion_gamma * live_overuse) if live_overuse > 0 else 1.0
        else:
            p = self.present_cost.get(key, 1.0)
        return (cost + h) * p

    def _violates_front_hood_drc(self, front_tile: tuple[int, int], heading: int) -> bool:
        """Return True if moving with heading into front_tile violates belt hood DRC-03."""
        if front_tile not in self.active_hood_types or self.is_net_fluid(
            self.active_hoods.get(front_tile, "")
        ):
            return False
        h_type, h_dir = self.active_hood_types[front_tile]
        if h_type == "output":
            return True
        if h_type == "input":
            return heading in PERPENDICULAR_DIRS[h_dir] or heading == OPPOSITE_DIR[h_dir]
        return False

    def _violates_rear_hood_drc(
        self,
        rear_nb: tuple[int, int],
        b_name: str,
        heading: int,
    ) -> bool:
        """Return True if placing a belt hood with rear_nb behind it violates DRC-03."""
        for nb_net, nb_h in self.surface_net_headings.get(rear_nb, {}).items():
            if (
                nb_h == heading
                and (self.min_x > 0 or base_net_name(nb_net) != b_name)
                and not self.is_net_fluid(nb_net)
            ):
                return True
        return False

    def _is_orthogonal_fluid_neighbor_compatible(
        self,
        pos: tuple[int, int],
        nb: tuple[int, int],
        my_comm: str,
    ) -> bool:
        """Return True if orthogonal neighbor nb does not cause fluid mixing with pos."""
        nb_is_fluid, nb_comm, nb_mouth = self.get_surface_fluid_info(nb)
        if nb_is_fluid and nb_comm != my_comm and (nb_mouth is None or nb_mouth == pos):
            return False
        return not self._has_conflicting_fluid_terminal(nb, my_comm)

    def _can_occupy_fluid_surface(
        self,
        pos: tuple[int, int],
        my_comm: str,
        current_nets: set[str] | None,
        is_hood: bool,
    ) -> bool:
        """Check fluid commodity compatibility on pos and its 4 orthogonal neighbors."""
        if current_nets and any(
            not self.is_net_fluid(cn) or self.get_net_commodity(cn) != my_comm
            for cn in current_nets
        ):
            return False
        if is_hood:
            return True
        if pos in self.fluid_port_approaches and my_comm not in self.fluid_port_approaches[pos]:
            return False
        return all(
            self._is_orthogonal_fluid_neighbor_compatible(
                pos, (pos[0] + odx, pos[1] + ody), my_comm
            )
            for odx, ody in ORTHOGONAL_OFFSETS
        )

    def _violates_belt_surface_rules(
        self,
        pos: tuple[int, int],
        b_name: str,
        heading: int | None,
        is_hood: bool,
    ) -> bool:
        """Return True if placing a belt on pos violates sink-front or DRC-03 hood rules."""
        if pos in self.belt_sink_front_tiles and any(
            sink_base != b_name for sink_base in self.belt_sink_front_tiles[pos]
        ):
            return True
        if heading is None:
            return False
        dx, dy = DIR_STEPS[heading]
        if self._violates_front_hood_drc((pos[0] + dx, pos[1] + dy), heading):
            return True
        return is_hood and self._violates_rear_hood_drc((pos[0] - dx, pos[1] - dy), b_name, heading)

    def can_occupy_surface(
        self,
        pos: tuple[int, int],
        net_name: str,
        _lane: int,
        heading: int | None = None,
        is_hood: bool = False,
    ) -> bool:
        """Return True if net_name can legally occupy surface tile pos."""
        if self.is_obstacle(pos[0], pos[1]) or pos in self.active_hoods:
            return False

        b_name = base_net_name(net_name)
        if pos in self.reserved_terminal_bases and b_name not in self.reserved_terminal_bases[pos]:
            return False

        current_nets = self.surface_occupancy.get(pos)
        if self.is_net_fluid(net_name):
            return self._can_occupy_fluid_surface(
                pos, self.get_net_commodity(net_name), current_nets, is_hood
            )
        if self._violates_belt_surface_rules(pos, b_name, heading, is_hood):
            return False
        return not (current_nets and any(self.is_net_fluid(cn) for cn in current_nets))

    def can_occupy_subterranean(
        self,
        layer_id: int,
        span_coords: list[tuple[int, int]],
        net_name: str,
    ) -> bool:
        """Check if an underground leap avoids collisions on its discrete layer."""
        b_name = base_net_name(net_name)
        occ = self.layer_occupancy.get(layer_id, {})
        for pt in span_coords:
            existing = occ.get(pt)
            if existing is not None and base_net_name(existing) != b_name:
                return False
        return True

    def _occupy_surface_tile(
        self,
        pos: tuple[int, int],
        net_name: str,
        lane: int,
        heading: int,
    ) -> None:
        """Record surface occupancy, lane, heading, and usage count at pos."""
        self.surface_occupancy.setdefault(pos, set()).add(net_name)
        self.layer_occupancy[LAYER_SURFACE][pos] = net_name
        self.surface_lanes.setdefault(pos, {})[lane] = net_name
        self.surface_net_headings.setdefault(pos, {})[net_name] = heading
        self.surface_headings[pos] = heading
        key = (pos[0], pos[1], LAYER_SURFACE)
        self.node_occupants.setdefault(key, set()).add(net_name)
        self.usage_count[key] = len(self.node_occupants[key])

    def _release_surface_tile(
        self,
        pos: tuple[int, int],
        net_name: str,
        lane: int,
    ) -> None:
        """Remove surface occupancy, lane, heading, and usage count for net_name at pos."""
        if pos in self.surface_occupancy:
            self.surface_occupancy[pos].discard(net_name)
            if not self.surface_occupancy[pos]:
                del self.surface_occupancy[pos]
                self.layer_occupancy[LAYER_SURFACE].pop(pos, None)
        if pos in self.surface_net_headings:
            self.surface_net_headings[pos].pop(net_name, None)
            if not self.surface_net_headings[pos]:
                del self.surface_net_headings[pos]
                self.surface_headings.pop(pos, None)
            else:
                self.surface_headings[pos] = next(iter(self.surface_net_headings[pos].values()))
        if pos in self.surface_lanes:
            self.surface_lanes[pos].pop(lane, None)
            if not self.surface_lanes[pos]:
                del self.surface_lanes[pos]
        key = (pos[0], pos[1], LAYER_SURFACE)
        self._discard_node_occupant(key, net_name)

    def _discard_node_occupant(self, key: tuple[int, int, int], net_name: str) -> None:
        """Remove net_name from node_occupants[key] and refresh usage_count[key]."""
        if key in self.node_occupants:
            self.node_occupants[key].discard(net_name)
            if not self.node_occupants[key]:
                del self.node_occupants[key]
        self.usage_count[key] = len(self.node_occupants.get(key, ()))

    def _domain_span_map(self, is_fluid: bool, is_horiz: bool) -> dict[tuple[int, int], str]:
        """Return the active belt or pipe span dictionary for the given axis."""
        if is_fluid:
            return self.active_pipe_horiz_spans if is_horiz else self.active_pipe_vert_spans
        return self.active_belt_horiz_spans if is_horiz else self.active_belt_vert_spans

    def _release_belt_sink_front(self, last_node: RouteNode, net_name: str) -> None:
        """Remove net_name's base net from the front tile of a belt sink node."""
        if self.is_net_fluid(net_name):
            return
        fdx, fdy = DIR_STEPS[last_node.heading]
        front_pos = (last_node.x + fdx, last_node.y + fdy)
        if front_pos in self.belt_sink_front_tiles:
            self.belt_sink_front_tiles[front_pos].discard(base_net_name(net_name))
            if not self.belt_sink_front_tiles[front_pos]:
                del self.belt_sink_front_tiles[front_pos]

    def rip_up_net(self, net_name: str) -> None:
        """Remove an existing routed net from the grid occupancy."""
        route = self.net_routes.pop(net_name, None)
        if not route:
            return

        for edge in route.edges:
            self._unmark_edge(edge, net_name)

        if route.nodes and route.nodes[-1].z == LAYER_SURFACE:
            last_node = route.nodes[-1]
            self._release_belt_sink_front(last_node, net_name)
            self._release_surface_tile(last_node.pos, net_name, last_node.lane)

    def place_net(self, net_name: str, route: RouteResult) -> None:
        """Add a routed net to the grid occupancy and update usage counts."""
        self.net_routes[net_name] = route
        for edge in route.edges:
            self._mark_edge(edge, net_name)

        if route.nodes and route.nodes[-1].z == LAYER_SURFACE:
            last_node = route.nodes[-1]
            pos = last_node.pos
            if not self.is_net_fluid(net_name):
                fdx, fdy = DIR_STEPS[last_node.heading]
                front_pos = (pos[0] + fdx, pos[1] + fdy)
                self.belt_sink_front_tiles.setdefault(front_pos, set()).add(base_net_name(net_name))
            self._occupy_surface_tile(pos, net_name, last_node.lane, last_node.heading)

    def _mark_leap_edge(
        self,
        start: RouteNode,
        end: RouteNode,
        net_name: str,
        is_fluid: bool,
    ) -> None:
        """Record hoods, spans, and subterranean occupancy for a leap edge."""
        start_pos = (start.x, start.y)
        end_pos = (end.x, end.y)
        z = start.z
        self.active_hoods[start_pos] = net_name
        self.active_hoods[end_pos] = net_name
        self.active_hood_types[start_pos] = ("input", start.heading)
        self.active_hood_types[end_pos] = ("output", end.heading)
        self._occupy_surface_tile(end_pos, net_name, LANE_BOTH, end.heading)

        dx = _step_sign(end.x - start.x)
        dy = _step_sign(end.y - start.y)
        dist = max(abs(end.x - start.x), abs(end.y - start.y))
        is_horiz = dx != 0
        axis_spans = self.active_horiz_spans if is_horiz else self.active_vert_spans
        domain_spans = self._domain_span_map(is_fluid, is_horiz)
        for s in range(1, dist):
            pt = (start.x + dx * s, start.y + dy * s)
            axis_spans[pt] = net_name
            domain_spans[pt] = net_name
        for s in range(dist + 1):
            pt = (start.x + dx * s, start.y + dy * s)
            is_endpoint_or_horiz = s in (0, dist) or is_horiz
            if is_endpoint_or_horiz:
                self.layer_occupancy[z][pt] = net_name
            key_z = z if is_endpoint_or_horiz else -z
            key = (pt[0], pt[1], key_z)
            self.node_occupants.setdefault(key, set()).add(net_name)
            self.usage_count[key] = len(self.node_occupants[key])

    def _mark_edge(self, edge: RouteEdge, net_name: str) -> None:
        """Record occupancy and increment usage count for an edge."""
        start = edge.start
        end = edge.end
        if start.z == LAYER_SURFACE:
            pos = (end.x, end.y) if edge.move_type == "dive" else start.pos
            if edge.move_type == "dive":
                self.active_hoods[pos] = net_name
                self.active_hood_types[pos] = ("input", start.heading)
            self._occupy_surface_tile(pos, net_name, start.lane, start.heading)
        elif edge.move_type == "leap":
            self._mark_leap_edge(start, end, net_name, self.is_net_fluid(net_name))

    @staticmethod
    def _clear_net_entry(
        mapping: dict[tuple[int, int], str],
        pt: tuple[int, int],
        net_name: str,
    ) -> bool:
        """Remove pt from mapping if owned by net_name and return True if removed."""
        if mapping.get(pt) == net_name:
            del mapping[pt]
            return True
        return False

    def _unmark_leap_edge(
        self,
        start: RouteNode,
        end: RouteNode,
        net_name: str,
        is_fluid: bool,
    ) -> None:
        """Release hoods, spans, and subterranean occupancy for a leap edge."""
        start_pos = (start.x, start.y)
        end_pos = (end.x, end.y)
        z = start.z
        for h_pos in (start_pos, end_pos):
            if self._clear_net_entry(self.active_hoods, h_pos, net_name):
                self.active_hood_types.pop(h_pos, None)
        self._release_surface_tile(end_pos, net_name, LANE_BOTH)

        dx = _step_sign(end.x - start.x)
        dy = _step_sign(end.y - start.y)
        dist = max(abs(end.x - start.x), abs(end.y - start.y))
        is_horiz = dx != 0
        axis_spans = self.active_horiz_spans if is_horiz else self.active_vert_spans
        domain_spans = self._domain_span_map(is_fluid, is_horiz)
        for s in range(1, dist):
            pt = (start.x + dx * s, start.y + dy * s)
            self._clear_net_entry(axis_spans, pt, net_name)
            self._clear_net_entry(domain_spans, pt, net_name)
        for s in range(dist + 1):
            pt = (start.x + dx * s, start.y + dy * s)
            self._clear_net_entry(self.layer_occupancy[z], pt, net_name)
            key_z = z if (s in (0, dist) or is_horiz) else -z
            self._discard_node_occupant((pt[0], pt[1], key_z), net_name)

    def _unmark_edge(self, edge: RouteEdge, net_name: str) -> None:
        """Release occupancy and decrement usage count for an edge."""
        start = edge.start
        end = edge.end
        if start.z == LAYER_SURFACE:
            pos = (end.x, end.y) if edge.move_type == "dive" else start.pos
            self._release_surface_tile(pos, net_name, start.lane)
            if edge.move_type == "dive" and self._clear_net_entry(self.active_hoods, pos, net_name):
                self.active_hood_types.pop(pos, None)
        elif edge.move_type == "leap":
            self._unmark_leap_edge(start, end, net_name, self.is_net_fluid(net_name))

    def get_capacity(self, node_key: tuple[int, int, int]) -> int:
        """Return the maximum legal concurrent net capacity for a grid node."""
        x, y, z = node_key
        pos = (x, y)
        if z != LAYER_SURFACE or pos in self.active_hoods:
            return 1
        occupants = self.node_occupants.get(node_key, set())
        if occupants and all(self.is_net_fluid(n) for n in occupants):
            comms = {self.get_net_commodity(n) for n in occupants}
            return len(occupants) if len(comms) == 1 else 1
        lanes = self.surface_lanes.get(pos)
        if not lanes or LANE_BOTH in lanes or any(self.is_net_fluid(n) for n in occupants):
            return 1
        net_headings = self.surface_net_headings.get(pos, {})
        headings = {net_headings[n] for n in occupants if n in net_headings}
        return 1 if len(headings) > 1 else DUAL_LANE_CAPACITY

    def update_congestion(
        self,
        beta: float = DEFAULT_CONGESTION_BETA,
        gamma: float = DEFAULT_CONGESTION_GAMMA,
    ) -> int:
        """Update historical and present congestion costs across all nodes.

        Returns total number of overused nodes.
        """
        self.congestion_gamma = gamma
        conflicts = 0
        for node_key, usage in self.usage_count.items():
            capacity = self.get_capacity(node_key)
            if usage > capacity:
                overuse = usage - capacity
                conflicts += overuse
                self.historical_cost[node_key] = (
                    self.historical_cost.get(node_key, 0.0) + beta * overuse
                )
                self.present_cost[node_key] = 1.0 + gamma * overuse
            else:
                self.present_cost[node_key] = 1.0
        self.has_congestion = conflicts > 0
        return conflicts
