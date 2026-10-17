"""Inline pump insertion pass for physical fluid pipelines.

Splits fluid networks exceeding the maximum pipeline extent limit (320 tiles in Factorio 2.0)
into smaller segments using inline powered pumps.
"""

from __future__ import annotations

import collections
import math
from typing import Any

try:
    from foundry.entity_helpers import (
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        OPPOSITE_DIR,
        get_underground_span,
    )
    from foundry.jumper import RouteResult
    from foundry.lef import get_macro_port_geometry
    from foundry.placement import PlacementResult
except ImportError:
    from entity_helpers import (
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        OPPOSITE_DIR,
        get_underground_span,
    )
    from jumper import RouteResult
    from lef import get_macro_port_geometry
    from placement import PlacementResult


def _get_pump_tiles(
    cx: float, cy: float, direction: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return the (in_tile, out_tile) for a pump entity."""
    if direction == DIRECTION_NORTH:
        in_tile = (math.floor(cx), math.floor(cy + 0.5))
        out_tile = (math.floor(cx), math.floor(cy - 0.5))
    elif direction == DIRECTION_SOUTH:
        in_tile = (math.floor(cx), math.floor(cy - 0.5))
        out_tile = (math.floor(cx), math.floor(cy + 0.5))
    elif direction == DIRECTION_EAST:
        in_tile = (math.floor(cx - 0.5), math.floor(cy))
        out_tile = (math.floor(cx + 0.5), math.floor(cy))
    else:  # DIRECTION_WEST
        in_tile = (math.floor(cx + 0.5), math.floor(cy))
        out_tile = (math.floor(cx - 0.5), math.floor(cy))
    return in_tile, out_tile


def build_conduit_graph(
    entities: list[dict[str, Any]],
) -> tuple[
    dict[Any, set[Any]],
    dict[tuple[int, int], list[dict[str, Any]]],
    dict[tuple[int, int], dict[str, Any]],
]:
    """Build the undirected graph of fluid conduit connections.

    Pumps separate upstream and downstream networks by creating distinct
    pump_in and pump_out nodes with no connecting edge between them.
    """
    tile_map: dict[tuple[int, int], list[dict[str, Any]]] = {}
    pipe_entities: dict[tuple[int, int], dict[str, Any]] = {}

    for ent in entities:
        pos = ent.get("position", {})
        cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
        cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
        name = ent.get("name", "")

        if name == "pipe":
            t = (math.floor(cx), math.floor(cy))
            tile_map.setdefault(t, []).append(ent)
            pipe_entities[t] = ent
        elif name == "pipe-to-ground":
            t = (math.floor(cx), math.floor(cy))
            tile_map.setdefault(t, []).append(ent)
        elif name == "pump":
            d = ent.get("direction", DIRECTION_NORTH)
            in_t, out_t = _get_pump_tiles(cx, cy, d)
            tile_map.setdefault(in_t, []).append(ent)
            tile_map.setdefault(out_t, []).append(ent)

    adj: dict[Any, set[Any]] = collections.defaultdict(set)

    # 1. Connect surface pipes
    for px, py in pipe_entities:
        node = ("pipe", px, py)
        adj.setdefault(node, set())
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nb_tile = (px + dx, py + dy)
            for nb in tile_map.get(nb_tile, []):
                nb_name = nb.get("name", "")
                if nb_name == "pipe":
                    nb_node = ("pipe", nb_tile[0], nb_tile[1])
                    adj[node].add(nb_node)
                    adj[nb_node].add(node)
                elif nb_name == "pipe-to-ground":
                    nb_d = nb.get("direction", DIRECTION_NORTH)
                    surf_dx, surf_dy = DIR_STEPS.get(nb_d, (0, 0))
                    surf_pos = (nb_tile[0] + surf_dx, nb_tile[1] + surf_dy)
                    if surf_pos == (px, py):
                        nb_node = ("pipe-to-ground", nb_tile[0], nb_tile[1])
                        adj[node].add(nb_node)
                        adj[nb_node].add(node)
                elif nb_name == "pump":
                    pump_d = nb.get("direction", DIRECTION_NORTH)
                    pos_nb = nb.get("position", {})
                    pcx = (
                        float(pos_nb.get("x", 0)) if isinstance(pos_nb, dict) else float(pos_nb[0])
                    )
                    pcy = (
                        float(pos_nb.get("y", 0)) if isinstance(pos_nb, dict) else float(pos_nb[1])
                    )
                    in_t, out_t = _get_pump_tiles(pcx, pcy, pump_d)

                    p_dx, p_dy = DIR_STEPS.get(pump_d, (0, 0))
                    # Check if (px, py) supplies pump input
                    if nb_tile == in_t and (px, py) == (in_t[0] - p_dx, in_t[1] - p_dy):
                        in_node = ("pump_in", id(nb), in_t[0], in_t[1])
                        adj[node].add(in_node)
                        adj[in_node].add(node)
                    # Check if (px, py) receives pump output
                    elif nb_tile == out_t and (px, py) == (out_t[0] + p_dx, out_t[1] + p_dy):
                        out_node = ("pump_out", id(nb), out_t[0], out_t[1])
                        adj[node].add(out_node)
                        adj[out_node].add(node)

    # 2. Connect underground pipe-to-ground pairs
    p2gs = [e for e in entities if e.get("name") == "pipe-to-ground"]
    for p in p2gs:
        pos = p.get("position", {})
        px = math.floor(float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0]))
        py = math.floor(float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1]))
        d = p.get("direction", DIRECTION_NORTH)
        ug_d = OPPOSITE_DIR.get(d, d)
        dx, dy = DIR_STEPS.get(ug_d, (0, 0))
        node = ("pipe-to-ground", px, py)
        adj.setdefault(node, set())

        # Surface connection to facing pipe-to-ground mouth-to-mouth or pump
        surf_dx, surf_dy = DIR_STEPS.get(d, (0, 0))
        surf_pos = (px + surf_dx, py + surf_dy)
        for c in tile_map.get(surf_pos, []):
            if c.get("name") == "pipe-to-ground":
                cd = c.get("direction", DIRECTION_NORTH)
                c_surf_dx, c_surf_dy = DIR_STEPS.get(cd, (0, 0))
                if (surf_pos[0] + c_surf_dx, surf_pos[1] + c_surf_dy) == (px, py):
                    c_node = ("pipe-to-ground", surf_pos[0], surf_pos[1])
                    adj[node].add(c_node)
                    adj[c_node].add(node)
            elif c.get("name") == "pump":
                pump_d = c.get("direction", DIRECTION_NORTH)
                pos_c = c.get("position", {})
                pcx = float(pos_c.get("x", 0)) if isinstance(pos_c, dict) else float(pos_c[0])
                pcy = float(pos_c.get("y", 0)) if isinstance(pos_c, dict) else float(pos_c[1])
                in_t, out_t = _get_pump_tiles(pcx, pcy, pump_d)
                p_dx, p_dy = DIR_STEPS.get(pump_d, (0, 0))
                if surf_pos == in_t and (px, py) == (in_t[0] - p_dx, in_t[1] - p_dy):
                    in_node = ("pump_in", id(c), in_t[0], in_t[1])
                    adj[node].add(in_node)
                    adj[in_node].add(node)
                elif surf_pos == out_t and (px, py) == (out_t[0] + p_dx, out_t[1] + p_dy):
                    out_node = ("pump_out", id(c), out_t[0], out_t[1])
                    adj[node].add(out_node)
                    adj[out_node].add(node)

        # Underground connection to facing pair within maximum span
        max_pipe_span = get_underground_span("pipe-to-ground", 10)
        for span in range(1, max_pipe_span + 1):
            chk_pos = (px + dx * span, py + dy * span)
            candidates = tile_map.get(chk_pos, [])
            for c in candidates:
                if c.get("name") == "pipe-to-ground" and c.get("direction") == ug_d:
                    c_node = ("pipe-to-ground", chk_pos[0], chk_pos[1])
                    adj[node].add(c_node)
                    adj[c_node].add(node)
                    break
            else:
                continue
            break

    return adj, tile_map, pipe_entities


def find_fluid_components(adj: dict[Any, set[Any]]) -> list[set[Any]]:
    """Return all connected components of conduit nodes."""
    visited: set[Any] = set()
    components: list[set[Any]] = []

    for node in adj:
        if node in visited:
            continue
        comp: set[Any] = set()
        queue = collections.deque([node])
        visited.add(node)
        while queue:
            curr = queue.popleft()
            comp.add(curr)
            for nb in adj.get(curr, ()):
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        components.append(comp)

    return components


def component_extent(comp: set[Any]) -> tuple[float, float, float, float, float]:
    """Calculate (extent, min_x, max_x, min_y, max_y) of a component."""
    xs = [node[1] if len(node) == 3 else node[2] for node in comp]
    ys = [node[2] if len(node) == 3 else node[3] for node in comp]
    if not xs:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    min_x, max_x = float(min(xs)), float(max(xs))
    min_y, max_y = float(min(ys)), float(max(ys))
    extent = max(max_x - min_x, max_y - min_y)
    return extent, min_x, max_x, min_y, max_y


def _compress_straight_pipes(
    placement: PlacementResult,
    protected_tiles: set[tuple[int, int]],
) -> None:
    """Convert interior tiles of long straight surface pipe runs into pipe-to-ground pairs."""
    adj, _tile_map, pipe_entities = build_conduit_graph(placement.entities)
    max_span = get_underground_span("pipe-to-ground", 10)

    blocked_ug_tiles: set[tuple[int, int]] = set()
    for ent in placement.entities:
        if ent.get("name") != "pipe-to-ground":
            continue
        pos = ent.get("position", {})
        px = math.floor(float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0]))
        py = math.floor(float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1]))
        d = ent.get("direction", DIRECTION_NORTH)
        dx, dy = DIR_STEPS.get(d, (0, 0))
        for s in range(-max_span, max_span + 1):
            blocked_ug_tiles.add((px + dx * s, py + dy * s))

    def _nb_tile(nb: Any) -> tuple[int, int]:
        return (int(nb[2]), int(nb[3])) if len(nb) == 4 else (int(nb[1]), int(nb[2]))

    to_remove_ids: set[int] = set()
    axes = (
        (1, 0, DIRECTION_WEST, DIRECTION_EAST),
        (0, 1, DIRECTION_NORTH, DIRECTION_SOUTH),
    )
    for dx, dy, dir_neg, dir_pos in axes:
        eligible: set[tuple[int, int]] = set()
        for x, y in pipe_entities:
            if (x, y) in protected_tiles or (x, y) in blocked_ug_tiles:
                continue
            nbs = adj.get(("pipe", x, y), set())
            if len(nbs) != 2:
                continue
            if {_nb_tile(nb) for nb in nbs} == {(x - dx, y - dy), (x + dx, y + dy)}:
                eligible.add((x, y))

        for x, y in sorted(eligible):
            if (x - dx, y - dy) in eligible:
                continue
            run: list[tuple[int, int]] = []
            cx, cy = x, y
            while (cx, cy) in eligible:
                run.append((cx, cy))
                cx += dx
                cy += dy
            if len(run) < 8:
                continue
            inner = run[1:-1]
            m = len(inner)
            i = 0
            while m - i >= 4:
                span = min(max_span, m - 1 - i)
                t_start = inner[i]
                t_end = inner[i + span]
                ent_start = pipe_entities.get(t_start)
                ent_end = pipe_entities.get(t_end)
                if ent_start is not None and ent_end is not None:
                    ent_start["name"] = "pipe-to-ground"
                    ent_start["direction"] = dir_neg
                    ent_end["name"] = "pipe-to-ground"
                    ent_end["direction"] = dir_pos
                    for j in range(i + 1, i + span):
                        t_mid = inner[j]
                        ent_mid = pipe_entities.get(t_mid)
                        if ent_mid is not None:
                            to_remove_ids.add(id(ent_mid))
                            placement.grid_entities.pop(
                                (round(t_mid[0] + 0.5, 2), round(t_mid[1] + 0.5, 2)),
                                None,
                            )
                i += span + 1

    if to_remove_ids:
        placement.entities[:] = [e for e in placement.entities if id(e) not in to_remove_ids]


def insert_pipeline_pumps(
    placement: PlacementResult,
    routes: RouteResult,
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
    max_segment_extent: int = 300,
    max_pumps: int = 50,
) -> int:
    """Insert powered inline pumps to split pipelines exceeding max_segment_extent.

    Returns the number of pumps inserted.
    """
    scale = 1.0 / float(def_data.get("units", 1000))
    pumps_inserted = 0

    raw_fluids = set(netlist_data.get("fluids") or []) | set(placement.fluids or [])
    fluids = (
        {f.replace("-", "_") for f in raw_fluids}
        | {f.replace("_", "-") for f in raw_fluids}
        | raw_fluids
    )
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    cell_map = netlist_data.get("cells", {})

    # 1. Collect all known fluid sources (input pins + machine fluid outputs)
    fluid_source_tiles: set[tuple[int, int]] = set()
    fluid_sink_tiles: set[tuple[int, int]] = set()

    for pin in def_data.get("pins", []):
        pname = pin.get("name", pin.get("net", ""))
        if pname in fluids or any(f in pname for f in fluids):
            px = math.floor(pin["x"] * scale)
            py = math.floor(pin["y"] * scale)
            if pin.get("direction") == "INPUT":
                fluid_source_tiles.add((px, py))
            elif pin.get("direction") == "OUTPUT":
                fluid_sink_tiles.add((px, py))

    for comp in def_data.get("components", []):
        cname = comp["name"]
        inst = inst_map.get(cname, {})
        cell_id = inst.get("cell", comp.get("macro", ""))
        cell = cell_map.get(cell_id, {})
        gx = comp["x"] * scale
        gy = comp["y"] * scale

        for port in cell.get("outputs", []):
            if port in fluids or any(f in port for f in fluids):
                geom = get_macro_port_geometry(cell, port, is_output=True)
                ax, ay = geom["inserter_pos"]
                fluid_source_tiles.add((math.floor(gx + ax), math.floor(gy + ay)))

        for port in cell.get("inputs", []):
            if port in fluids or any(f in port for f in fluids):
                geom = get_macro_port_geometry(cell, port, is_output=False)
                ax, ay = geom["inserter_pos"]
                fluid_sink_tiles.add((math.floor(gx + ax), math.floor(gy + ay)))

    protected_tiles: set[tuple[int, int]] = set(fluid_source_tiles) | set(fluid_sink_tiles)
    for px_f, py_f in routes.pin_tiles | routes.valid_input_pins | routes.valid_output_pins:
        protected_tiles.add((math.floor(px_f), math.floor(py_f)))
    macro_fp = def_data.get("_macro_floorplan")
    if macro_fp is not None:
        for bport in getattr(macro_fp, "block_ports", []):
            protected_tiles.add((int(bport.core_pos[0]), int(bport.core_pos[1])))
            protected_tiles.add((int(bport.channel_pos[0]), int(bport.channel_pos[1])))

    for _iteration in range(max_pumps):
        adj, _tile_map, pipe_entities = build_conduit_graph(placement.entities)
        components = find_fluid_components(adj)

        overextended: list[tuple[float, set[Any]]] = []
        for comp in components:
            ext, _, _, _, _ = component_extent(comp)
            if ext > float(max_segment_extent):
                overextended.append((ext, comp))

        if not overextended:
            break  # All components are within max_segment_extent

        overextended.sort(key=lambda item: item[0], reverse=True)

        candidates: list[
            tuple[
                tuple[int, float, float, int],
                tuple[int, int],
                tuple[int, int],
                int,
                float,
                float,
            ]
        ] = []
        comp_pipes: dict[tuple[int, int], dict[str, Any]] = {}

        for worst_extent, worst_comp in overextended:
            # Identify sources for this component
            sources: list[Any] = []
            sinks: list[Any] = []
            for node in worst_comp:
                ntype = node[0]
                if ntype == "pump_out":
                    sources.append(node)
                elif ntype == "pump_in":
                    sinks.append(node)
                elif ntype in ("pipe", "pipe-to-ground"):
                    pt = (node[1], node[2])
                    if pt in fluid_source_tiles:
                        sources.append(node)
                    if pt in fluid_sink_tiles:
                        sinks.append(node)

            use_sources = bool(sources)
            ref_nodes = sources if use_sources else sinks

            if not ref_nodes:
                # Fallback: pick node with minimum coordinate as source
                ref_nodes = [
                    min(
                        worst_comp,
                        key=lambda n: (
                            n[2] if len(n) == 4 else n[1],
                            n[3] if len(n) == 4 else n[2],
                        ),
                    )
                ]
                use_sources = True

            # Compute BFS distances from reference nodes
            dist: dict[Any, int] = {}
            queue: collections.deque[tuple[Any, int]] = collections.deque()
            for s in ref_nodes:
                dist[s] = 0
                queue.append((s, 0))

            while queue:
                curr, d = queue.popleft()
                for nb in adj.get(curr, ()):
                    if nb in worst_comp and nb not in dist:
                        dist[nb] = d + 1
                        queue.append((nb, d + 1))

            # Target distance along path for inserting a pump
            target_dist = min(max_segment_extent - 40, int(worst_extent // 2))
            target_dist = max(target_dist, 50)

            comp_pipes = {
                (n[1], n[2]): pipe_entities[(n[1], n[2])]
                for n in worst_comp
                if n[0] == "pipe" and (n[1], n[2]) in pipe_entities
            }
            src_set = set(sources)
            sink_set = set(sinks)

            for ux, uy in comp_pipes:
                u_node = ("pipe", ux, uy)
                u_dist = dist.get(u_node)
                if u_dist is None:
                    continue

                # Check neighbors: Vertical and Horizontal
                for vx, vy, is_vertical in ((ux, uy + 1, True), (ux + 1, uy, False)):
                    if (vx, vy) not in comp_pipes:
                        continue
                    v_node = ("pipe", vx, vy)
                    v_dist = dist.get(v_node)
                    if v_dist is None or u_dist == v_dist:
                        continue

                    # Ensure neither tile is a protected pin, port, or machine approach tile
                    if (ux, uy) in protected_tiles or (vx, vy) in protected_tiles:
                        continue

                    # Check degree in component: each pipe must connect to the other and one collinear neighbor
                    u_nbs = [nb for nb in adj.get(u_node, ()) if nb in worst_comp]
                    v_nbs = [nb for nb in adj.get(v_node, ()) if nb in worst_comp]
                    if len(u_nbs) != 2 or len(v_nbs) != 2:
                        continue

                    dx, dy = vx - ux, vy - uy
                    nb_u = u_nbs[0] if u_nbs[1] == v_node else u_nbs[1]
                    nb_v = v_nbs[0] if v_nbs[1] == u_node else v_nbs[1]
                    if nb_u[0] not in ("pipe", "pipe-to-ground") or nb_v[0] not in (
                        "pipe",
                        "pipe-to-ground",
                    ):
                        continue
                    if (nb_u[1], nb_u[2]) != (ux - dx, uy - dy) or (nb_v[1], nb_v[2]) != (
                        vx + dx,
                        vy + dy,
                    ):
                        continue

                    # Partition worst_comp by removing edge (u_node, v_node)
                    comp_u: set[Any] = {u_node}
                    q_u: collections.deque[Any] = collections.deque([u_node])
                    while q_u:
                        cur = q_u.popleft()
                        for nb in adj.get(cur, ()):
                            if nb in worst_comp and nb not in comp_u:
                                if (cur == u_node and nb == v_node) or (
                                    cur == v_node and nb == u_node
                                ):
                                    continue
                                comp_u.add(nb)
                                q_u.append(nb)

                    sep_penalty = 0
                    if v_node in comp_u:
                        split_ext = worst_extent
                        over_limit = worst_extent
                        primary_metric = round(worst_extent, 2)
                        is_u_in = (u_dist < v_dist) if use_sources else (u_dist > v_dist)
                        sep_penalty = 1
                    else:
                        comp_v = worst_comp - comp_u
                        u_has_src = bool(comp_u & src_set)
                        v_has_src = bool(comp_v & src_set)
                        u_has_sink = bool(comp_u & sink_set)
                        v_has_sink = bool(comp_v & sink_set)

                        if src_set and sink_set:
                            if u_has_src and v_has_sink and not (v_has_src and not u_has_src):
                                if not v_has_src or not u_has_sink:
                                    is_u_in = True
                                else:
                                    is_u_in = (
                                        (u_dist < v_dist) if use_sources else (u_dist > v_dist)
                                    )
                            elif v_has_src and u_has_sink and not (u_has_src and not v_has_src):
                                if not u_has_src or not v_has_sink:
                                    is_u_in = False
                                else:
                                    is_u_in = (
                                        (u_dist < v_dist) if use_sources else (u_dist > v_dist)
                                    )
                            else:
                                is_u_in = (u_dist < v_dist) if use_sources else (u_dist > v_dist)
                                sep_penalty = 1
                        else:
                            is_u_in = (u_dist < v_dist) if use_sources else (u_dist > v_dist)

                        ext_u, _, _, _, _ = component_extent(comp_u)
                        ext_v, _, _, _, _ = component_extent(comp_v)
                        split_ext = max(ext_u, ext_v)
                        ext_out = ext_v if is_u_in else ext_u
                        in_has_sink = u_has_sink if is_u_in else v_has_sink
                        out_has_src = v_has_src if is_u_in else u_has_src
                        over_limit = max(0.0, split_ext - float(max_segment_extent))
                        if over_limit == 0.0 and in_has_sink and not out_has_src:
                            primary_metric = round(ext_out, 2)
                        else:
                            primary_metric = round(split_ext, 2)

                    if is_u_in:
                        in_t, out_t = (ux, uy), (vx, vy)
                        base_dist = u_dist
                    else:
                        in_t, out_t = (vx, vy), (ux, uy)
                        base_dist = v_dist

                    if is_vertical:
                        if out_t[1] < in_t[1]:  # Flow North
                            pump_dir = DIRECTION_NORTH
                            center_x = in_t[0] + 0.5
                            center_y = float(in_t[1])
                        else:  # Flow South
                            pump_dir = DIRECTION_SOUTH
                            center_x = in_t[0] + 0.5
                            center_y = float(in_t[1] + 1.0)
                    else:
                        if out_t[0] > in_t[0]:  # Flow East
                            pump_dir = DIRECTION_EAST
                            center_x = float(in_t[0] + 1.0)
                            center_y = in_t[1] + 0.5
                        else:  # Flow West
                            pump_dir = DIRECTION_WEST
                            center_x = float(in_t[0])
                            center_y = in_t[1] + 0.5

                    score = (
                        sep_penalty,
                        round(over_limit, 2),
                        primary_metric,
                        abs(base_dist - target_dist),
                    )
                    candidates.append((score, in_t, out_t, pump_dir, center_x, center_y))

            if candidates:
                break

        if not candidates:
            break

        # Pick candidate with best score
        candidates.sort(key=lambda c: c[0])
        _, best_in, best_out, best_dir, best_cx, best_cy = candidates[0]

        # Replace the two pipes with the pump
        ent_in = comp_pipes.get(best_in)
        ent_out = comp_pipes.get(best_out)
        if ent_in in placement.entities:
            placement.entities.remove(ent_in)
        if ent_out in placement.entities:
            placement.entities.remove(ent_out)

        pump_ent = {
            "name": "pump",
            "position": {"x": best_cx, "y": best_cy},
            "direction": best_dir,
        }
        placement.entities.append(pump_ent)

        # Update placement grid_entities and occupied_positions
        pos_in_key = (round(best_in[0] + 0.5, 2), round(best_in[1] + 0.5, 2))
        pos_out_key = (round(best_out[0] + 0.5, 2), round(best_out[1] + 0.5, 2))
        placement.grid_entities[pos_in_key] = pump_ent
        placement.grid_entities[pos_out_key] = pump_ent
        placement.occupied_positions.add(pos_in_key)
        placement.occupied_positions.add(pos_out_key)

        pumps_inserted += 1

    _compress_straight_pipes(placement, protected_tiles)
    return pumps_inserted
