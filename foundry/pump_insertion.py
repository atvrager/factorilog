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

        # Underground connection to facing pair within span 10 (step 1..11)
        for span in range(1, 12):
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


def insert_pipeline_pumps(
    placement: PlacementResult,
    routes: RouteResult,
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
    max_segment_extent: int = 250,
    max_pumps: int = 50,
) -> int:
    """Insert powered inline pumps to split pipelines exceeding max_segment_extent.

    Returns the number of pumps inserted.
    """
    scale = 1.0 / float(def_data.get("units", 1000))
    pumps_inserted = 0

    fluids = set(netlist_data.get("fluids") or [])
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    cell_map = netlist_data.get("cells", {})

    # 1. Collect all known fluid sources (input pins + machine fluid outputs)
    fluid_source_tiles: set[tuple[int, int]] = set()
    fluid_sink_tiles: set[tuple[int, int]] = set()

    for pin in def_data.get("pins", []):
        pname = pin.get("name", "")
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
                ax, ay = geom["approach_pos"]
                fluid_source_tiles.add((math.floor(gx + ax), math.floor(gy + ay)))

        for port in cell.get("inputs", []):
            if port in fluids or any(f in port for f in fluids):
                geom = get_macro_port_geometry(cell, port, is_output=False)
                ax, ay = geom["approach_pos"]
                fluid_sink_tiles.add((math.floor(gx + ax), math.floor(gy + ay)))

    for _iteration in range(max_pumps):
        adj, _tile_map, pipe_entities = build_conduit_graph(placement.entities)
        components = find_fluid_components(adj)

        # Find overextended component with largest extent
        worst_comp: set[Any] | None = None
        worst_extent = float(max_segment_extent)

        for comp in components:
            ext, _, _, _, _ = component_extent(comp)
            if ext > worst_extent:
                worst_extent = ext
                worst_comp = comp

        if worst_comp is None:
            break  # All components are within max_segment_extent

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
                else:
                    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                        if (pt[0] + dx, pt[1] + dy) in fluid_source_tiles:
                            sources.append(node)
                            break
                if pt in fluid_sink_tiles:
                    sinks.append(node)
                else:
                    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                        if (pt[0] + dx, pt[1] + dy) in fluid_sink_tiles:
                            sinks.append(node)
                            break

        use_sources = bool(sources)
        ref_nodes = sources if use_sources else sinks

        if not ref_nodes:
            # Fallback: pick node with minimum coordinate as source
            ref_nodes = [
                min(
                    worst_comp,
                    key=lambda n: (n[2] if len(n) == 4 else n[1], n[3] if len(n) == 4 else n[2]),
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

        # Search for candidate straight pipe pairs (u, v) in worst_comp
        candidates: list[tuple[int, tuple[int, int], tuple[int, int], int, float, float]] = []

        comp_pipes = {
            (n[1], n[2]): pipe_entities[(n[1], n[2])]
            for n in worst_comp
            if n[0] == "pipe" and (n[1], n[2]) in pipe_entities
        }

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

                # Ensure neither tile is an I/O pin or machine approach tile
                if (ux + 0.5, uy + 0.5) in routes.pin_tiles or (
                    vx + 0.5,
                    vy + 0.5,
                ) in routes.pin_tiles:
                    continue
                if (ux, uy) in fluid_source_tiles or (vx, vy) in fluid_source_tiles:
                    continue
                if (ux, uy) in fluid_sink_tiles or (vx, vy) in fluid_sink_tiles:
                    continue

                # Flow direction depends on whether BFS measured from sources or sinks
                # Fluid flows from smaller distance if from sources, else towards sinks
                is_u_in = (u_dist < v_dist) if use_sources else (u_dist > v_dist)

                if is_u_in:
                    in_t, out_t = (ux, uy), (vx, vy)
                    in_node, out_node = u_node, v_node
                    base_dist = u_dist
                else:
                    in_t, out_t = (vx, vy), (ux, uy)
                    in_node, out_node = v_node, u_node
                    base_dist = v_dist

                # Check degree in component: each pipe should connect to the other and exactly one opposite pipe
                u_nbs = [nb for nb in adj.get(in_node, ()) if nb in worst_comp]
                v_nbs = [nb for nb in adj.get(out_node, ()) if nb in worst_comp]

                # We require no orthogonal branches at the pump site
                if len(u_nbs) != 2 or len(v_nbs) != 2:
                    continue

                # Check collinearity of connections
                if is_vertical:
                    # In-line must connect along Y
                    if out_t[1] < in_t[1]:  # Flow North
                        pump_dir = DIRECTION_NORTH
                        center_x = in_t[0] + 0.5
                        center_y = float(in_t[1])
                    else:  # Flow South
                        pump_dir = DIRECTION_SOUTH
                        center_x = in_t[0] + 0.5
                        center_y = float(in_t[1] + 1.0)
                else:
                    # In-line must connect along X
                    if out_t[0] > in_t[0]:  # Flow East
                        pump_dir = DIRECTION_EAST
                        center_x = float(in_t[0] + 1.0)
                        center_y = in_t[1] + 0.5
                    else:  # Flow West
                        pump_dir = DIRECTION_WEST
                        center_x = float(in_t[0])
                        center_y = in_t[1] + 0.5

                score = abs(base_dist - target_dist)
                candidates.append((score, in_t, out_t, pump_dir, center_x, center_y))

        if not candidates:
            # Relax degree requirement if no candidates found
            for ux, uy in comp_pipes:
                u_node = ("pipe", ux, uy)
                u_dist = dist.get(u_node)
                if u_dist is None:
                    continue
                for vx, vy, is_vertical in ((ux, uy + 1, True), (ux + 1, uy, False)):
                    if (vx, vy) not in comp_pipes:
                        continue
                    v_node = ("pipe", vx, vy)
                    v_dist = dist.get(v_node)
                    if v_dist is None or u_dist == v_dist:
                        continue
                    if (ux, uy) in fluid_source_tiles or (vx, vy) in fluid_source_tiles:
                        continue
                    if (ux, uy) in fluid_sink_tiles or (vx, vy) in fluid_sink_tiles:
                        continue

                    is_u_in = u_dist < v_dist if use_sources else u_dist > v_dist

                    if is_u_in:
                        in_t, out_t = (ux, uy), (vx, vy)
                        base_dist = u_dist
                    else:
                        in_t, out_t = (vx, vy), (ux, uy)
                        base_dist = v_dist

                    if is_vertical:
                        if out_t[1] < in_t[1]:
                            pump_dir = DIRECTION_NORTH
                            center_x = in_t[0] + 0.5
                            center_y = float(in_t[1])
                        else:
                            pump_dir = DIRECTION_SOUTH
                            center_x = in_t[0] + 0.5
                            center_y = float(in_t[1] + 1.0)
                    else:
                        if out_t[0] > in_t[0]:
                            pump_dir = DIRECTION_EAST
                            center_x = float(in_t[0] + 1.0)
                            center_y = in_t[1] + 0.5
                        else:
                            pump_dir = DIRECTION_WEST
                            center_x = float(in_t[0])
                            center_y = in_t[1] + 0.5

                    score = abs(base_dist - target_dist)
                    candidates.append((score, in_t, out_t, pump_dir, center_x, center_y))

        if not candidates:
            break

        # Pick candidate with best score (closest to target distance)
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

    return pumps_inserted
