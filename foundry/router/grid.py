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
    RouteEdge,
    RouteNode,
    RouteResult,
    RouterNetSpec,
)


def base_net_name(net_name: str) -> str:
    """Return the base logical net name by stripping driver/sink and hierarchy suffixes."""
    if net_name.endswith("_hwy"):
        net_name = net_name[:-4]
    for sep in ("_export_", "_import_", "_hwy_", "_blk_"):
        if sep in net_name:
            net_name = net_name.split(sep)[0]
    idx = net_name.find("_d")
    if idx != -1 and "_s" in net_name[idx:]:
        return net_name[:idx]
    if "_mst_" in net_name:
        return net_name.split("_mst_")[0]
    return net_name


class RoutingGrid:
    """Manages spatial occupancy and congestion costs for 3D routing."""

    def __init__(
        self,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
        prototypes: dict[str, Any] | None = None,
        obstacles: set[tuple[int, int]] | None = None,
        component_headings: dict[tuple[int, int], int] | None = None,
        no_hood_tiles: set[tuple[int, int]] | None = None,
    ) -> None:
        """Initialize the routing grid with dimensions and prototype parameters."""
        self.min_x = min_x
        self.max_x = max_x
        self.min_y = min_y
        self.max_y = max_y

        self.prototypes = prototypes if prototypes is not None else load_game_prototypes()
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
        self.max_belt_span: int = max((t["max_span"] for t in self.belt_tiers.values()), default=5)
        self.max_pipe_span: int = max((t["max_span"] for t in self.pipe_tiers.values()), default=5)

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

        # Congestion tracking
        self.usage_count: dict[tuple[int, int, int], int] = {}
        self.node_occupants: dict[tuple[int, int, int], set[str]] = {}
        self.historical_cost: dict[tuple[int, int, int], float] = {}
        self.present_cost: dict[tuple[int, int, int], float] = {}
        self.congestion_gamma: float = 1.5

        # Reserved terminals: (x, y) -> set of allowed net names
        self.reserved_terminals: dict[tuple[int, int], set[str]] = {}
        self.reserved_terminal_bases: dict[tuple[int, int], set[str]] = {}
        self.terminal_headings: dict[tuple[int, int], int] = {}
        self.fluid_terminal_inward_dir: dict[tuple[int, int], int] = {}
        self.fluid_port_approaches: dict[tuple[int, int], set[str]] = {}
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

        # Stored net routes: net_name -> RouteResult
        self.net_routes: dict[str, RouteResult] = {}
        self.has_congestion: bool = False

    def is_net_fluid(self, net_name: str) -> bool:
        """Return True if the net is registered as a fluid commodity net."""
        return self.net_is_fluid.get(net_name, False) or self.net_is_fluid.get(
            base_net_name(net_name), False
        )

    def get_net_commodity(self, net_name: str) -> str:
        """Return the normalized commodity name for a net."""
        b_name = base_net_name(net_name)
        comm = self.net_commodity.get(net_name) or self.net_commodity.get(b_name)
        if comm:
            return comm.replace("_", "-")
        return b_name.split("__")[0].replace("_", "-")

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
            mouth_pos = (pos[0] + mdx, pos[1] + mdy)
            return (True, comm, mouth_pos)

        return (True, comm, None)

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
                my_comm = self.get_net_commodity(net.name)
                if net.source_inward_dir is not None:
                    self.fluid_terminal_inward_dir[net.source] = net.source_inward_dir
                    odx, ody = DIR_STEPS[net.source_inward_dir]
                    out_pos = (net.source[0] - odx, net.source[1] - ody)
                    self.fluid_port_approaches.setdefault(out_pos, set()).add(my_comm)
                if net.sink_inward_dir is not None:
                    self.fluid_terminal_inward_dir[net.sink] = net.sink_inward_dir
                    odx, ody = DIR_STEPS[net.sink_inward_dir]
                    out_pos = (net.sink[0] - odx, net.sink[1] - ody)
                    self.fluid_port_approaches.setdefault(out_pos, set()).add(my_comm)

    def can_place_hood(
        self,
        pos: tuple[int, int],
        net_name: str,
        heading: int | None = None,
        is_output: bool = False,
    ) -> bool:
        """Check if an underground hood can be placed without colliding with active spans."""
        b_name = base_net_name(net_name)
        is_fluid = self.is_net_fluid(net_name)
        if pos in self.no_hood_tiles:
            return False
        if self.is_obstacle(pos[0], pos[1]):
            return False
        if pos in self.active_hoods:
            return False
        if is_fluid:
            if pos in self.active_pipe_horiz_spans or pos in self.active_pipe_vert_spans:
                return False
        else:
            if pos in self.active_belt_horiz_spans or pos in self.active_belt_vert_spans:
                return False
        if pos in self.surface_occupancy:
            return False
        if pos in self.reserved_terminals:
            if is_fluid:
                my_comm = self.get_net_commodity(net_name)
                if not any(
                    self.is_net_fluid(tn) and self.get_net_commodity(tn) == my_comm
                    for tn in self.reserved_terminals[pos]
                ):
                    return False
            elif net_name not in self.reserved_terminals[pos]:
                return False

        if is_fluid and heading is not None:
            my_comm = self.get_net_commodity(net_name)
            if pos in self.fluid_port_approaches and my_comm not in self.fluid_port_approaches[pos]:
                return False
            for odx, ody in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                nb = (pos[0] + odx, pos[1] + ody)
                if nb in self.active_hood_types and self.is_net_fluid(
                    self.active_hoods.get(nb, "")
                ):
                    nb_type, nb_dir = self.active_hood_types[nb]
                    nb_mouth_dir = OPPOSITE_DIR.get(nb_dir, 0) if nb_type == "input" else nb_dir
                    nmdx, nmdy = DIR_STEPS[nb_mouth_dir]
                    if (nb[0] + nmdx, nb[1] + nmdy) == pos:
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
            if mouth_pos in self.reserved_terminals:
                for tn in self.reserved_terminals[mouth_pos]:
                    if self.is_net_fluid(tn) and self.get_net_commodity(tn) != my_comm:
                        return False

        if not self.is_net_fluid(net_name) and heading is not None:
            # Rule DRC-03: clearance around underground hoods
            side_dirs = [(heading + 4) % 16, (heading + 12) % 16]
            for s_dir in side_dirs:
                sdx, sdy = DIR_STEPS[s_dir]
                side_nb = (pos[0] - sdx, pos[1] - sdy)
                if self.surface_headings.get(side_nb) == s_dir:
                    return False
                if s_dir in self.surface_net_headings.get(side_nb, {}).values():
                    return False
                if self.terminal_headings.get(side_nb) == s_dir:
                    return False
                if self.component_headings.get(side_nb) == s_dir:
                    return False

            if is_output:
                if (
                    pos in self.reserved_terminal_bases
                    and b_name not in self.reserved_terminal_bases[pos]
                ):
                    return False
                dx, dy = DIR_STEPS[heading]
                rear_pos = (pos[0] - dx, pos[1] - dy)
                if self.surface_headings.get(rear_pos) == heading:
                    return False
                if heading in self.surface_net_headings.get(rear_pos, {}).values():
                    return False
                if self.terminal_headings.get(rear_pos) == heading:
                    return False
                if self.component_headings.get(rear_pos) == heading:
                    return False
                if (
                    rear_pos in self.reserved_terminal_bases
                    and b_name not in self.reserved_terminal_bases[rear_pos]
                ):
                    return False
        return True

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
        """Check if obstacle or blockage 1-2 steps ahead warrants diving underground."""
        b_name = base_net_name(net_name)
        blocked_ahead = False
        for s in (1, 2, 3):
            tx = x + dx * s
            ty = y + dy * s
            if not (self.min_x <= tx <= self.max_x and self.min_y <= ty <= self.max_y):
                return False
            pt = (tx, ty)
            if target_pos is not None and pt == target_pos:
                return False
            idx = (ty - self.min_y) * self.width + (tx - self.min_x)
            if (
                self.obstacle_bytes[idx] == 1
                or pt in self.active_hoods
                or (
                    pt in self.reserved_terminal_bases
                    and b_name not in self.reserved_terminal_bases[pt]
                )
            ):
                blocked_ahead = True
                break
            if pt in self.surface_occupancy:
                current_nets = self.surface_occupancy[pt]
                if current_nets and any(base_net_name(n) != b_name for n in current_nets):
                    blocked_ahead = True
                    break
            if self.has_congestion and self.historical_cost.get((tx, ty, 0), 0.0) > 0.0:
                blocked_ahead = True
                break
            if not self.can_occupy_surface(pt, net_name, lane):
                blocked_ahead = True
                break
        if not blocked_ahead:
            return False
        for step in range(2, _max_span + 1):
            ex = x + dx * step
            ey = y + dy * step
            if not (self.min_x <= ex <= self.max_x and self.min_y <= ey <= self.max_y):
                break
            e_idx = (ey - self.min_y) * self.width + (ex - self.min_x)
            if self.obstacle_bytes[e_idx] == 0:
                return True
        return False

    def can_leap_span(
        self,
        span_coords: list[tuple[int, int]],
        is_horiz: bool,
        _net_name: str,
    ) -> bool:
        """Check if an underground leap avoids collisions with hoods and parallel spans."""
        axis_spans = self.active_horiz_spans if is_horiz else self.active_vert_spans
        for pt in span_coords:
            if pt in self.active_hoods:
                return False
            if pt in axis_spans:
                return False
        return True

    def _init_belt_tiers(self) -> dict[int, dict[str, Any]]:
        """Register underground belt tiers dynamically from prototypes."""
        result: dict[int, dict[str, Any]] = {}
        ug_dict = self.prototypes.get("underground-belt", {})
        belt_dict = self.prototypes.get("transport-belt", {})

        # Sort underground prototypes by speed ascending
        valid_ugs = [
            (name, proto)
            for name, proto in ug_dict.items()
            if "speed" in proto and "max_distance" in proto and "turbo" not in name
        ]
        sorted_ugs = sorted(
            valid_ugs,
            key=lambda kv: float(kv[1]["speed"]),
        )

        for idx, (ug_name, ug_proto) in enumerate(sorted_ugs):
            layer_id = idx + 1
            speed = float(ug_proto["speed"])
            span = int(ug_proto["max_distance"])

            # Match surface belt by related_underground_belt or speed
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
                "tier_cost": idx * 1.5,
                "speed": speed,
            }
        return result

    def _init_pipe_tiers(self) -> dict[int, dict[str, Any]]:
        """Register underground pipe tiers dynamically from prototypes."""
        result: dict[int, dict[str, Any]] = {}
        pg_dict = self.prototypes.get("pipe-to-ground", {})
        pipe_dict = self.prototypes.get("pipe", {})
        default_pipe = next(iter(pipe_dict.keys()), "pipe")

        # Sort pipe-to-ground prototypes by name
        sorted_pgs = sorted(pg_dict.items(), key=lambda kv: kv[0])

        for idx, (pg_name, pg_proto) in enumerate(sorted_pgs):
            layer_id = 10 + idx
            fluid_box = pg_proto.get("fluid_box", {})
            conn_dist = None
            for conn in fluid_box.get("pipe_connections", []):
                if (
                    conn.get("connection_type") == "underground"
                    and "max_underground_distance" in conn
                ):
                    conn_dist = int(conn["max_underground_distance"])
                    break
            if conn_dist is None:
                continue

            result[layer_id] = {
                "name": pg_name,
                "pipe_name": default_pipe,
                "max_span": conn_dist,
                "tier_cost": idx * 1.5,
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
        return 5

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
            occ = self.surface_occupancy.get((x, y))
            b_name = base_net_name(net_name)
            if occ and any(base_net_name(n) != b_name for n in occ):
                lanes = self.surface_lanes.get((x, y), {})
                if not (lane != LANE_BOTH and LANE_BOTH not in lanes and lane not in lanes):
                    cost += 8.0
                    live_overuse = len(occ)
                    if heading is not None:
                        net_h = self.surface_net_headings.get((x, y))
                        if net_h and any(
                            base_net_name(n) != b_name and (nh % 8) == (heading % 8)
                            for n, nh in net_h.items()
                        ):
                            cost += 16.0
        if not self.has_congestion:
            return cost
        key = (x, y, z)
        h = self.historical_cost.get(key, 0.0)
        if z == LAYER_SURFACE and net_name:
            p = (1.0 + self.congestion_gamma * live_overuse) if live_overuse > 0 else 1.0
        else:
            p = self.present_cost.get(key, 1.0)
        return (cost + h) * p

    def can_occupy_surface(
        self,
        pos: tuple[int, int],
        net_name: str,
        _lane: int,
        heading: int | None = None,
        is_hood: bool = False,
    ) -> bool:
        b_name = base_net_name(net_name)
        if self.is_obstacle(pos[0], pos[1]):
            return False

        if pos in self.reserved_terminal_bases and b_name not in self.reserved_terminal_bases[pos]:
            return False

        if pos in self.active_hoods:
            return False

        is_fluid = self.is_net_fluid(net_name)
        if not is_fluid and pos in self.belt_sink_front_tiles:
            for sink_base in self.belt_sink_front_tiles[pos]:
                if sink_base != b_name:
                    return False

        # Rule DRC-03: clearance around underground hoods (belt only)
        if not is_fluid and heading is not None:
            dx, dy = DIR_STEPS[heading]
            front_tile = (pos[0] + dx, pos[1] + dy)
            if front_tile in self.active_hood_types and not self.is_net_fluid(
                self.active_hoods.get(front_tile, "")
            ):
                h_type, h_dir = self.active_hood_types[front_tile]
                if h_type == "output":
                    return False
                if h_type == "input":
                    side_dirs = [(h_dir + 4) % 16, (h_dir + 12) % 16]
                    if heading in side_dirs or heading == (h_dir + 8) % 16:
                        return False

            if is_hood:
                rear_nb = (pos[0] - dx, pos[1] - dy)
                if rear_nb in self.surface_net_headings:
                    for nb_net, nb_h in self.surface_net_headings[rear_nb].items():
                        if nb_h == heading and not self.is_net_fluid(nb_net):
                            return False

        my_comm = self.get_net_commodity(net_name) if is_fluid else ""
        current_nets = self.surface_occupancy.get(pos)

        if is_fluid:
            if current_nets:
                for cn in current_nets:
                    if not self.is_net_fluid(cn) or self.get_net_commodity(cn) != my_comm:
                        return False

            if not is_hood:
                if (
                    pos in self.fluid_port_approaches
                    and my_comm not in self.fluid_port_approaches[pos]
                ):
                    return False
                for odx, ody in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    nb = (pos[0] + odx, pos[1] + ody)
                    nb_is_fluid, nb_comm, nb_mouth = self.get_surface_fluid_info(nb)
                    if nb_is_fluid and nb_comm != my_comm:
                        if nb_mouth is not None and nb_mouth != pos:
                            continue
                        return False
                    if nb in self.reserved_terminals:
                        for tn in self.reserved_terminals[nb]:
                            if self.is_net_fluid(tn) and self.get_net_commodity(tn) != my_comm:
                                return False
        else:
            if current_nets and any(self.is_net_fluid(cn) for cn in current_nets):
                return False

        return True

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

    def rip_up_net(self, net_name: str) -> None:
        """Remove an existing routed net from the grid occupancy."""
        route = self.net_routes.pop(net_name, None)
        if not route:
            return

        for edge in route.edges:
            self._unmark_edge(edge, net_name)

        if route.nodes:
            last_node = route.nodes[-1]
            if last_node.z == LAYER_SURFACE:
                pos = last_node.pos
                if not self.is_net_fluid(net_name):
                    fdx, fdy = DIR_STEPS[last_node.heading]
                    front_pos = (pos[0] + fdx, pos[1] + fdy)
                    if front_pos in self.belt_sink_front_tiles:
                        self.belt_sink_front_tiles[front_pos].discard(base_net_name(net_name))
                        if not self.belt_sink_front_tiles[front_pos]:
                            del self.belt_sink_front_tiles[front_pos]
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
                        self.surface_headings[pos] = next(
                            iter(self.surface_net_headings[pos].values())
                        )
                if pos in self.surface_lanes:
                    self.surface_lanes[pos].pop(last_node.lane, None)
                    if not self.surface_lanes[pos]:
                        del self.surface_lanes[pos]
                key = (last_node.x, last_node.y, 0)
                if key in self.node_occupants:
                    self.node_occupants[key].discard(net_name)
                    if not self.node_occupants[key]:
                        del self.node_occupants[key]
                self.usage_count[key] = len(self.node_occupants.get(key, ()))

    def place_net(self, net_name: str, route: RouteResult) -> None:
        """Add a routed net to the grid occupancy and update usage counts."""
        self.net_routes[net_name] = route
        for edge in route.edges:
            self._mark_edge(edge, net_name)

        if route.nodes:
            last_node = route.nodes[-1]
            if last_node.z == LAYER_SURFACE:
                pos = last_node.pos
                if not self.is_net_fluid(net_name):
                    fdx, fdy = DIR_STEPS[last_node.heading]
                    front_pos = (pos[0] + fdx, pos[1] + fdy)
                    self.belt_sink_front_tiles.setdefault(front_pos, set()).add(
                        base_net_name(net_name)
                    )
                self.surface_occupancy.setdefault(pos, set()).add(net_name)
                self.layer_occupancy[LAYER_SURFACE][pos] = net_name
                lanes = self.surface_lanes.setdefault(pos, {})
                lanes[last_node.lane] = net_name
                self.surface_net_headings.setdefault(pos, {})[net_name] = last_node.heading
                self.surface_headings[pos] = last_node.heading
                key = (last_node.x, last_node.y, 0)
                self.node_occupants.setdefault(key, set()).add(net_name)
                self.usage_count[key] = len(self.node_occupants[key])

    def _mark_edge(self, edge: RouteEdge, net_name: str) -> None:
        """Record occupancy and increment usage count for an edge."""
        start = edge.start
        end = edge.end
        z = start.z
        is_fluid = self.is_net_fluid(net_name)

        if z == LAYER_SURFACE:
            pos = (
                (end.x, end.y)
                if edge.move_type == "dive" and (start.x != end.x or start.y != end.y)
                else start.pos
            )
            self.surface_occupancy.setdefault(pos, set()).add(net_name)
            self.layer_occupancy[LAYER_SURFACE][pos] = net_name
            if edge.move_type == "dive":
                self.active_hoods[pos] = net_name
                self.active_hood_types[pos] = ("input", start.heading)
            lanes = self.surface_lanes.setdefault(pos, {})
            lanes[start.lane] = net_name
            self.surface_net_headings.setdefault(pos, {})[net_name] = start.heading
            self.surface_headings[pos] = start.heading
            key = (pos[0], pos[1], 0)
            self.node_occupants.setdefault(key, set()).add(net_name)
            self.usage_count[key] = len(self.node_occupants[key])
        elif edge.move_type == "leap":
            start_pos = (start.x, start.y)
            end_pos = (end.x, end.y)
            self.active_hoods[start_pos] = net_name
            self.active_hoods[end_pos] = net_name
            self.active_hood_types[start_pos] = ("input", start.heading)
            self.active_hood_types[end_pos] = ("output", end.heading)
            self.surface_occupancy.setdefault(end_pos, set()).add(net_name)
            self.layer_occupancy[LAYER_SURFACE][end_pos] = net_name
            end_lanes = self.surface_lanes.setdefault(end_pos, {})
            end_lanes[LANE_BOTH] = net_name
            self.surface_net_headings.setdefault(end_pos, {})[net_name] = end.heading
            self.surface_headings[end_pos] = end.heading
            dx = 1 if end.x > start.x else (-1 if end.x < start.x else 0)
            dy = 1 if end.y > start.y else (-1 if end.y < start.y else 0)
            dist = max(abs(end.x - start.x), abs(end.y - start.y))
            is_horiz = dx != 0
            axis_spans = self.active_horiz_spans if is_horiz else self.active_vert_spans
            domain_spans = (
                (self.active_pipe_horiz_spans if is_horiz else self.active_pipe_vert_spans)
                if is_fluid
                else (self.active_belt_horiz_spans if is_horiz else self.active_belt_vert_spans)
            )
            for s in range(1, dist):
                pt = (start.x + dx * s, start.y + dy * s)
                axis_spans[pt] = net_name
                domain_spans[pt] = net_name
            for s in range(dist + 1):
                pt = (start.x + dx * s, start.y + dy * s)
                if s == 0 or s == dist or is_horiz:
                    self.layer_occupancy[z][pt] = net_name
                key_z = z if (s == 0 or s == dist or is_horiz) else -z
                key = (pt[0], pt[1], key_z)
                self.node_occupants.setdefault(key, set()).add(net_name)
                self.usage_count[key] = len(self.node_occupants[key])

    def _unmark_edge(self, edge: RouteEdge, net_name: str) -> None:
        """Release occupancy and decrement usage count for an edge."""
        start = edge.start
        end = edge.end
        z = start.z
        is_fluid = self.is_net_fluid(net_name)

        if z == LAYER_SURFACE:
            pos = (
                (end.x, end.y)
                if edge.move_type == "dive" and (start.x != end.x or start.y != end.y)
                else start.pos
            )
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
            if edge.move_type == "dive" and self.active_hoods.get(pos) == net_name:
                del self.active_hoods[pos]
                self.active_hood_types.pop(pos, None)
            if pos in self.surface_lanes:
                self.surface_lanes[pos].pop(start.lane, None)
                if not self.surface_lanes[pos]:
                    del self.surface_lanes[pos]
            key = (pos[0], pos[1], 0)
            if key in self.node_occupants:
                self.node_occupants[key].discard(net_name)
                if not self.node_occupants[key]:
                    del self.node_occupants[key]
            self.usage_count[key] = len(self.node_occupants.get(key, ()))
        elif edge.move_type == "leap":
            start_pos = (start.x, start.y)
            end_pos = (end.x, end.y)
            if self.active_hoods.get(start_pos) == net_name:
                del self.active_hoods[start_pos]
                self.active_hood_types.pop(start_pos, None)
            if self.active_hoods.get(end_pos) == net_name:
                del self.active_hoods[end_pos]
                self.active_hood_types.pop(end_pos, None)
            if end_pos in self.surface_occupancy:
                self.surface_occupancy[end_pos].discard(net_name)
                if not self.surface_occupancy[end_pos]:
                    del self.surface_occupancy[end_pos]
                    self.layer_occupancy[LAYER_SURFACE].pop(end_pos, None)
            if end_pos in self.surface_net_headings:
                self.surface_net_headings[end_pos].pop(net_name, None)
                if not self.surface_net_headings[end_pos]:
                    del self.surface_net_headings[end_pos]
                    self.surface_headings.pop(end_pos, None)
                else:
                    self.surface_headings[end_pos] = next(
                        iter(self.surface_net_headings[end_pos].values())
                    )
            if end_pos in self.surface_lanes:
                self.surface_lanes[end_pos].pop(LANE_BOTH, None)
                if not self.surface_lanes[end_pos]:
                    del self.surface_lanes[end_pos]
            dx = 1 if end.x > start.x else (-1 if end.x < start.x else 0)
            dy = 1 if end.y > start.y else (-1 if end.y < start.y else 0)
            dist = max(abs(end.x - start.x), abs(end.y - start.y))
            is_horiz = dx != 0
            axis_spans = self.active_horiz_spans if is_horiz else self.active_vert_spans
            domain_spans = (
                (self.active_pipe_horiz_spans if is_horiz else self.active_pipe_vert_spans)
                if is_fluid
                else (self.active_belt_horiz_spans if is_horiz else self.active_belt_vert_spans)
            )
            for s in range(1, dist):
                pt = (start.x + dx * s, start.y + dy * s)
                if axis_spans.get(pt) == net_name:
                    del axis_spans[pt]
                if domain_spans.get(pt) == net_name:
                    del domain_spans[pt]
            for s in range(dist + 1):
                pt = (start.x + dx * s, start.y + dy * s)
                if self.layer_occupancy[z].get(pt) == net_name:
                    del self.layer_occupancy[z][pt]
                key_z = z if (s == 0 or s == dist or is_horiz) else -z
                key = (pt[0], pt[1], key_z)
                if key in self.node_occupants:
                    self.node_occupants[key].discard(net_name)
                    if not self.node_occupants[key]:
                        del self.node_occupants[key]
                self.usage_count[key] = len(self.node_occupants.get(key, ()))

    def get_capacity(self, node_key: tuple[int, int, int]) -> int:
        """Return the maximum legal concurrent net capacity for a grid node."""
        x, y, z = node_key
        if z != LAYER_SURFACE:
            return 1
        pos = (x, y)
        if pos in self.active_hoods:
            return 1
        occupants = self.node_occupants.get(node_key, set())
        if occupants and all(self.is_net_fluid(n) for n in occupants):
            comms = {self.get_net_commodity(n) for n in occupants}
            if len(comms) == 1:
                return len(occupants)
            return 1
        lanes = self.surface_lanes.get(pos)
        if not lanes or LANE_BOTH in lanes:
            return 1
        if any(self.is_net_fluid(n) for n in occupants):
            return 1
        headings = {
            self.surface_net_headings[pos][n]
            for n in occupants
            if pos in self.surface_net_headings and n in self.surface_net_headings[pos]
        }
        if len(headings) > 1:
            return 1
        return 2

    def update_congestion(self, beta: float = 0.5, gamma: float = 1.5) -> int:
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
