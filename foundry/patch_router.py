"""Patch-to-base router and starter base blueprint generator.

Synthesizes mining drill arrays on irregular map resource patches, places a
shoreline steam power plant and a logistics-tier starter base with a bootstrap
mall, routes trunk belts from patches to base inputs, and wires a map-wide
electric pole network.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from foundry import blueprint, solve
from foundry.entity_helpers import (
    DIR_STEPS,
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    TILE_HALF,
    check_collisions,
    check_power,
    check_underground_pairing,
    entity_bounding_box,
    entity_size,
    register_prototype_boxes,
)
from foundry.native_engine.engine import FactorioNativeEngine
from foundry.native_engine.verifier import NativeLayoutVerifier, NetSpec, VerificationResult
from foundry.pdn import MstEdge, generate_pdn, make_pdn_config
from foundry.router.maze_router import DualLaneMazeRouter
from foundry.router.types import RouteResult, RouterNetSpec

EntityDict: TypeAlias = dict[str, Any]
TileCoord: TypeAlias = tuple[int, int]
GridPos: TypeAlias = tuple[float, float]

WIRE_COPPER_ID: int = 5
CARDINAL_DIRS: tuple[int, int, int, int] = (
    DIRECTION_NORTH,
    DIRECTION_EAST,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
)
SECONDS_PER_MINUTE: float = 60.0
COORD_PRECISION: int = 2
BOX_EPSILON: float = 1e-6
HALF_DIVISOR: float = 2.0


@dataclass(frozen=True, slots=True)
class MapSurfaceSpec:
    """Spatial bounds, water tiles, resource patch tile sets, and terrain obstacles."""

    min_x: int
    max_x: int
    min_y: int
    max_y: int
    water_tiles: set[TileCoord] = field(default_factory=set)
    resource_patches: dict[str, set[TileCoord]] = field(default_factory=dict)
    obstacles: set[TileCoord] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class TechTierSpec:
    """Unlocked entity prototypes and parameters for a target technology tier."""

    unlocked_by: str
    allowed_recipes: set[str]
    drill_name: str
    drill_width: int
    drill_height: int
    drill_mining_speed: float
    drill_drop_dx: int
    drill_drop_dy: int
    drill_watts: float
    belt_name: str
    underground_belt_name: str
    splitter_name: str
    pole_name: str
    inserter_name: str
    long_inserter_name: str
    furnace_name: str
    assembler_name: str
    lab_name: str
    pump_name: str
    boiler_name: str
    generator_name: str
    fuel_commodity: str
    filtered_prototypes: dict[str, Any]


@dataclass(slots=True)
class PatchArrayResult:
    """Synthesized mining drill array and collection belt terminal for one patch."""

    commodity: str
    entities: list[EntityDict]
    occupied_tiles: set[TileCoord]
    output_terminals: list[TileCoord]
    output_heading: int
    drill_count: int


@dataclass(slots=True)
class ShorelinePowerResult:
    """Synthesized offshore pump, boiler, and steam engine array on a shoreline."""

    entities: list[EntityDict]
    occupied_tiles: set[TileCoord]
    fuel_input_terminal: TileCoord
    fuel_input_heading: int
    boiler_count: int
    engine_count: int


@dataclass(slots=True)
class StarterBaseCoreResult:
    """Synthesized smelting, intermediate, mall, and science starter base core."""

    entities: list[EntityDict]
    occupied_tiles: set[TileCoord]
    input_terminals: dict[str, TileCoord]
    input_headings: dict[str, int]
    internal_nets: list[RouterNetSpec]
    bounds: tuple[int, int, int, int]


@dataclass(slots=True)
class PatchToBaseResult:
    """Complete routed patch-to-base layout, power network, and blueprint string."""

    entities: list[EntityDict]
    wires: list[list[int]]
    blueprint_string: str
    patch_arrays: dict[str, PatchArrayResult]
    power_array: ShorelinePowerResult
    base_core: StarterBaseCoreResult
    trunk_routes: dict[str, RouteResult]
    trunk_nets: list[RouterNetSpec]
    mst_edges: list[MstEdge]
    verification: VerificationResult | None


def _rotate_offset(dx: float, dy: float, direction: int) -> tuple[float, float]:
    """Rotate a North-relative (dx, dy) offset into cardinal direction."""
    if direction == DIRECTION_NORTH:
        return dx, dy
    if direction == DIRECTION_EAST:
        return -dy, dx
    if direction == DIRECTION_SOUTH:
        return -dx, -dy
    return dy, -dx


def _is_unlocked(name: str, data: solve.Data, allowed_recipes: set[str]) -> bool:
    """Return True if prototype name is available at the start or unlocked in allowed_recipes."""
    return name not in data.unlocked_by or name in allowed_recipes


def _discover_minable_fuel_commodity(data: solve.Data) -> str:
    """Find the minable solid resource with the highest chemical fuel value from prototypes."""
    best_name = ""
    best_joules = 0.0
    items = data.prototypes.get("item", {})
    for res_name, res_proto in data.resources.items():
        minable = res_proto.get("minable", {})
        if not minable or "required_fluid" in minable:
            continue
        item_proto = items.get(res_name, {})
        fuel_cats = item_proto.get("fuel_categories") or []
        is_chem = "chemical" in fuel_cats or item_proto.get("fuel_category") == "chemical"
        if is_chem and "fuel_value" in item_proto:
            joules = solve.energy_joules(item_proto["fuel_value"])
            if joules > best_joules:
                best_joules = joules
                best_name = res_name
    return best_name


def _select_unlocked_drill(
    data: solve.Data,
    allowed: set[str],
) -> tuple[str, dict[str, Any]]:
    """Select the fastest unlocked electric solid mining drill from prototypes."""
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for name, proto in data.drills.items():
        if not _is_unlocked(name, data, allowed):
            continue
        cats = proto.get("resource_categories", [])
        if "basic-solid" not in cats:
            continue
        es = proto.get("energy_source", {})
        is_elec = es.get("type", "electric") == "electric"
        score = float(proto.get("mining_speed", 0.0)) + (10.0 if is_elec else 0.0)
        candidates.append((score, name, proto))
    if not candidates:
        raise ValueError("No unlocked solid mining drill found in prototype data")
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2]


def _select_unlocked_pole(data: solve.Data, allowed: set[str]) -> str:
    """Select the unlocked electric pole with the smallest footprint and valid supply area."""
    poles = data.prototypes.get("electric-pole", {})
    candidates: list[tuple[float, float, str]] = []
    for name, proto in poles.items():
        if not _is_unlocked(name, data, allowed):
            continue
        w, h = entity_bounding_box(proto)
        supply = float(proto.get("supply_area_distance", 0.0))
        if supply > 0.0:
            candidates.append((w * h, supply, name))
    if not candidates:
        raise ValueError("No unlocked electric pole found in prototype data")
    candidates.sort()
    return candidates[0][2]


def _select_unlocked_inserters(data: solve.Data, allowed: set[str]) -> tuple[str, str]:
    """Select standard (reach 1) and long-handed (reach 2) unlocked electric inserters."""
    inserters = data.prototypes.get("inserter", {})
    reach1: list[tuple[float, str]] = []
    reach2: list[tuple[float, str]] = []
    for name, proto in inserters.items():
        if not _is_unlocked(name, data, allowed):
            continue
        es = proto.get("energy_source", {})
        if es.get("type") == "burner":
            continue
        pickup = proto.get("pickup_position", [0.0, -1.0])
        reach = round(math.hypot(float(pickup[0]), float(pickup[1])))
        rot = float(proto.get("rotation_speed", 0.0))
        if reach == 1:
            reach1.append((rot, name))
        elif reach >= 2:
            reach2.append((rot, name))
    reach1.sort()
    reach2.sort()
    std_name = reach1[0][1] if reach1 else "inserter"
    long_name = reach2[0][1] if reach2 else std_name
    return std_name, long_name


def _filter_prototypes_by_unlock(
    raw_prototypes: Mapping[str, Any],
    data: solve.Data,
    allowed: set[str],
) -> dict[str, Any]:
    """Filter transport-belt, underground-belt, and splitter tables to unlocked prototypes."""
    filtered = dict(raw_prototypes)
    for table in ("transport-belt", "underground-belt", "splitter"):
        table_dict = raw_prototypes.get(table, {})
        if isinstance(table_dict, Mapping):
            filtered[table] = {
                k: v for k, v in table_dict.items() if _is_unlocked(k, data, allowed)
            }
    return filtered


def _allowed_recipes_for_techs(
    data: solve.Data,
    unlocked_by: str | Sequence[str],
) -> set[str]:
    """Return the union of allowed recipes for one or more technology targets."""
    tech_list = (
        [t.strip() for t in unlocked_by.split(",") if t.strip()]
        if isinstance(unlocked_by, str)
        else list(unlocked_by)
    )
    allowed: set[str] = set()
    for tech in tech_list:
        allowed |= data.allowed_recipes(["nauvis"], unlocked_by=tech)
    asm_names = set(data.prototypes.get("assembling-machine", {}).keys())
    if not (asm_names & allowed):
        for asm_name in sorted(asm_names):
            unlock_tech = data.unlocked_by.get(asm_name)
            if unlock_tech:
                allowed |= data.allowed_recipes(["nauvis"], unlocked_by=unlock_tech)
                break
    elec_drills = {
        name
        for name, proto in data.drills.items()
        if "basic-solid" in proto.get("resource_categories", [])
        and proto.get("energy_source", {}).get("type", "electric") == "electric"
    }
    if not (elec_drills & allowed):
        for d_name in sorted(elec_drills):
            unlock_tech = data.unlocked_by.get(d_name)
            if unlock_tech:
                allowed |= data.allowed_recipes(["nauvis"], unlocked_by=unlock_tech)
                break
    return allowed


def resolve_tech_tier(
    data: solve.Data,
    unlocked_by: str = "logistics",
) -> TechTierSpec:
    """Resolve unlocked machine, belt, drill, pole, and inserter prototypes for unlocked_by."""
    register_prototype_boxes(data.prototypes)
    allowed = _allowed_recipes_for_techs(data, unlocked_by)
    filtered_protos = _filter_prototypes_by_unlock(data.prototypes, data, allowed)

    drill_name, drill_proto = _select_unlocked_drill(data, allowed)
    dw_f, dh_f = entity_bounding_box(drill_proto)
    vec = drill_proto.get("vector_to_place_result", [0.0, -1.85])
    drop_dx = round(float(vec[0]))
    drop_dy = math.floor(float(vec[1]))
    drill_watts = solve.power_watts(drill_proto.get("energy_usage", 0))

    belts = filtered_protos.get("transport-belt", {})
    ugs = filtered_protos.get("underground-belt", {})
    splitters = filtered_protos.get("splitter", {})
    if not belts or not ugs or not splitters:
        raise ValueError(f"Technology '{unlocked_by}' does not unlock required belt logistics")

    belt_name = min(belts.items(), key=lambda kv: float(kv[1]["speed"]))[0]
    ug_name = min(ugs.items(), key=lambda kv: float(kv[1]["speed"]))[0]
    splitter_name = min(splitters.items(), key=lambda kv: float(kv[1]["speed"]))[0]
    pole_name = _select_unlocked_pole(data, allowed)
    ins_name, long_ins_name = _select_unlocked_inserters(data, allowed)

    smelt_recipe = next(
        r for r in data.recipes.values() if "smelting" in (r.get("categories") or [])
    )
    craft_recipe = next(
        r for r in data.recipes.values() if "crafting" in (r.get("categories") or ["crafting"])
    )
    furnace_pick = data.machine_for(smelt_recipe, allowed=allowed)
    asm_pick = data.machine_for(craft_recipe, allowed=allowed)
    if furnace_pick is None or asm_pick is None:
        raise ValueError(f"Could not resolve furnace or assembler for '{unlocked_by}'")

    lab_name = next(iter(data.prototypes.get("lab", {"lab": {}}).keys()))
    boiler_name = next(iter(data.prototypes.get("boiler", {"boiler": {}}).keys()))
    gen_name = next(iter(data.prototypes.get("generator", {"steam-engine": {}}).keys()))
    fuel_comm = _discover_minable_fuel_commodity(data)

    return TechTierSpec(
        unlocked_by=unlocked_by,
        allowed_recipes=allowed,
        drill_name=drill_name,
        drill_width=int(dw_f),
        drill_height=int(dh_f),
        drill_mining_speed=float(drill_proto["mining_speed"]),
        drill_drop_dx=drop_dx,
        drill_drop_dy=drop_dy,
        drill_watts=drill_watts,
        belt_name=belt_name,
        underground_belt_name=ug_name,
        splitter_name=splitter_name,
        pole_name=pole_name,
        inserter_name=ins_name,
        long_inserter_name=long_ins_name,
        furnace_name=furnace_pick[0],
        assembler_name=asm_pick[0],
        lab_name=lab_name,
        pump_name="offshore-pump",
        boiler_name=boiler_name,
        generator_name=gen_name,
        fuel_commodity=fuel_comm,
        filtered_prototypes=filtered_protos,
    )


def _footprint_tiles(cx: float, cy: float, width: int, height: int) -> set[TileCoord]:
    """Return integer tile coordinates covered by a width x height box centered at (cx, cy)."""
    half_w = width / HALF_DIVISOR
    half_h = height / HALF_DIVISOR
    min_tx = math.floor(cx - half_w + BOX_EPSILON)
    max_tx = math.floor(cx + half_w - BOX_EPSILON)
    min_ty = math.floor(cy - half_h + BOX_EPSILON)
    max_ty = math.floor(cy + half_h - BOX_EPSILON)
    return {(tx, ty) for tx in range(min_tx, max_tx + 1) for ty in range(min_ty, max_ty + 1)}


def _required_drill_count(
    commodity: str,
    target_rate_per_min: float,
    data: solve.Data,
    tier: TechTierSpec,
) -> int:
    """Compute the number of drills required to extract target_rate_per_min of commodity."""
    res_proto = data.resources.get(commodity, {})
    minable = res_proto.get("minable", {})
    mining_time = float(minable.get("mining_time", 1.0))
    rate_per_drill = tier.drill_mining_speed * SECONDS_PER_MINUTE / mining_time
    return max(1, math.ceil(target_rate_per_min / rate_per_drill))


def cluster_resource_tiles(patch_tiles: set[TileCoord]) -> list[set[TileCoord]]:
    """Partition patch_tiles into 8-connected components sorted by descending size."""
    remaining = set(patch_tiles)
    clusters: list[set[TileCoord]] = []
    while remaining:
        start = min(remaining)
        remaining.remove(start)
        comp = {start}
        queue = [start]
        while queue:
            cx, cy = queue.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nb = (cx + dx, cy + dy)
                    if nb in remaining:
                        remaining.remove(nb)
                        comp.add(nb)
                        queue.append(nb)
        clusters.append(comp)
    clusters.sort(key=lambda c: (-len(c), min(c)))
    return clusters


def select_primary_patch_clusters(
    resource_patches: Mapping[str, set[TileCoord]],
    reference_pos: TileCoord = (0, 0),
    min_tiles: int = 9,
) -> dict[str, set[TileCoord]]:
    """Select the primary connected resource cluster nearest reference_pos for each commodity."""
    selected: dict[str, set[TileCoord]] = {}
    rx, ry = reference_pos
    for comm, tiles in resource_patches.items():
        if not tiles:
            continue
        clusters = cluster_resource_tiles(set(tiles))
        if len(clusters) == 1:
            selected[comm] = clusters[0]
            continue
        viable = [c for c in clusters if len(c) >= min_tiles] or clusters
        best = min(
            viable,
            key=lambda c: math.hypot(
                sum(pt[0] for pt in c) / len(c) - rx,
                sum(pt[1] for pt in c) / len(c) - ry,
            ),
        )
        selected[comm] = best
    return selected


def _try_build_patch_row(
    commodity: str,
    patch_tiles: set[TileCoord],
    belt_dir: int,
    belt_line_coord: int,
    required_drills: int,
    output_fanout: int,
    obstacles: set[TileCoord],
    tier: TechTierSpec,
) -> PatchArrayResult | None:
    """Attempt to place drills on one or both sides of a straight collection belt line."""
    dw = tier.drill_width
    dh = tier.drill_height
    is_horiz = belt_dir in (DIRECTION_EAST, DIRECTION_WEST)
    step_sign = 1 if belt_dir in (DIRECTION_EAST, DIRECTION_SOUTH) else -1

    xs = [pt[0] for pt in patch_tiles]
    ys = [pt[1] for pt in patch_tiles]
    along_vals = xs if is_horiz else ys
    min_a, max_a = min(along_vals), max(along_vals)

    start_a = (max_a - dw + 1) if step_sign > 0 else min_a
    stop_a = (min_a - 1) if step_sign > 0 else (max_a - dw + 2)
    slot_starts = list(range(start_a, stop_a, -step_sign * dw))
    if not slot_starts:
        return None

    placed_drills: list[EntityDict] = []
    occupied: set[TileCoord] = set()
    drop_along_coords: list[int] = []

    if is_horiz:
        sides = (
            (-dh / HALF_DIVISOR, DIRECTION_SOUTH),
            (1.0 + dh / HALF_DIVISOR, DIRECTION_NORTH),
        )
    else:
        sides = (
            (-dw / HALF_DIVISOR, DIRECTION_EAST),
            (1.0 + dw / HALF_DIVISOR, DIRECTION_WEST),
        )

    for side_off, d_dir in sides:
        if len(placed_drills) >= required_drills:
            break
        step_x, step_y = DIR_STEPS[d_dir]
        for a0 in slot_starts:
            if len(placed_drills) >= required_drills:
                break
            if is_horiz:
                cx = a0 + dw / HALF_DIVISOR
                cy = belt_line_coord + side_off
            else:
                cx = belt_line_coord + side_off
                cy = a0 + dh / HALF_DIVISOR
            fp = _footprint_tiles(cx, cy, dw, dh)
            if fp & obstacles or fp & occupied:
                continue
            if not (fp & patch_tiles):
                continue
            if commodity == "iron-ore":
                sm_x = cx + 2.5 * step_x + (0.5 if step_x == 0 else 0.0)
                sm_y = cy + 2.5 * step_y + (0.5 if step_y == 0 else 0.0)
                sm_fp = _footprint_tiles(sm_x, sm_y, 2, 2)
                if sm_fp & obstacles or sm_fp & occupied:
                    continue
            placed_drills.append(
                {
                    "name": tier.drill_name,
                    "position": {"x": round(cx, COORD_PRECISION), "y": round(cy, COORD_PRECISION)},
                    "direction": d_dir,
                }
            )
            occupied.update(fp)
            drop_along_coords.append(a0 + dw // 2)

    if not placed_drills:
        return None

    first_a = min(drop_along_coords) if step_sign > 0 else max(drop_along_coords)
    last_drop_a = max(drop_along_coords) if step_sign > 0 else min(drop_along_coords)
    exit_a = last_drop_a + step_sign * 2

    belt_a_coords = list(range(first_a, exit_a + step_sign, step_sign))
    belt_entities: list[EntityDict] = []
    for ba in belt_a_coords:
        b_pt = (ba, belt_line_coord) if is_horiz else (belt_line_coord, ba)
        if b_pt in obstacles or b_pt in occupied:
            return None
        belt_entities.append(
            {
                "name": tier.belt_name,
                "position": {"x": b_pt[0] + TILE_HALF, "y": b_pt[1] + TILE_HALF},
                "direction": belt_dir,
            }
        )
        occupied.add(b_pt)

    end_pt = (exit_a, belt_line_coord) if is_horiz else (belt_line_coord, exit_a)
    output_terminals: list[TileCoord] = [end_pt]

    if output_fanout > 1:
        spl_a = exit_a + step_sign
        out_a = exit_a + step_sign * 2
        if is_horiz:
            spl_tiles = {(spl_a, belt_line_coord), (spl_a, belt_line_coord + 1)}
            spl_pos = (spl_a + TILE_HALF, belt_line_coord + 1.0)
            term_pts = [(out_a, belt_line_coord), (out_a, belt_line_coord + 1)]
        else:
            spl_tiles = {(belt_line_coord, spl_a), (belt_line_coord + 1, spl_a)}
            spl_pos = (belt_line_coord + 1.0, spl_a + TILE_HALF)
            term_pts = [(belt_line_coord, out_a), (belt_line_coord + 1, out_a)]

        if (
            spl_tiles & obstacles
            or spl_tiles & occupied
            or any(t in obstacles or t in occupied for t in term_pts)
        ):
            return None
        belt_entities.append(
            {
                "name": tier.splitter_name,
                "position": {
                    "x": round(spl_pos[0], COORD_PRECISION),
                    "y": round(spl_pos[1], COORD_PRECISION),
                },
                "direction": belt_dir,
            }
        )
        occupied.update(spl_tiles)
        for t_pt in term_pts:
            belt_entities.append(
                {
                    "name": tier.belt_name,
                    "position": {"x": t_pt[0] + TILE_HALF, "y": t_pt[1] + TILE_HALF},
                    "direction": belt_dir,
                }
            )
            occupied.add(t_pt)
        output_terminals = term_pts

    step_dx, step_dy = DIR_STEPS[belt_dir]
    for t_pt in output_terminals:
        for k in (1, 2):
            ahead = (t_pt[0] + k * step_dx, t_pt[1] + k * step_dy)
            if ahead in obstacles or ahead in occupied:
                return None

    return PatchArrayResult(
        commodity=commodity,
        entities=[*placed_drills, *belt_entities],
        occupied_tiles=occupied,
        output_terminals=output_terminals,
        output_heading=belt_dir,
        drill_count=len(placed_drills),
    )


def synthesize_patch_miner_array(
    commodity: str,
    patch_tiles: set[TileCoord],
    target_rate_per_min: float,
    target_anchor: TileCoord,
    obstacles: set[TileCoord],
    data: solve.Data,
    tier: TechTierSpec,
    output_fanout: int = 1,
) -> PatchArrayResult:
    """Synthesize a non-overlapping mining drill array and collection bus over patch_tiles."""
    if not patch_tiles:
        raise ValueError(f"Patch for '{commodity}' contains no resource tiles")

    required_drills = _required_drill_count(commodity, target_rate_per_min, data, tier)
    cx = sum(pt[0] for pt in patch_tiles) / len(patch_tiles)
    cy = sum(pt[1] for pt in patch_tiles) / len(patch_tiles)
    dx = target_anchor[0] - cx
    dy = target_anchor[1] - cy

    preferred_dirs = sorted(
        CARDINAL_DIRS,
        key=lambda d: -(DIR_STEPS[d][0] * dx + DIR_STEPS[d][1] * dy),
    )

    xs = [pt[0] for pt in patch_tiles]
    ys = [pt[1] for pt in patch_tiles]
    best_result: PatchArrayResult | None = None
    best_score: tuple[int, float] = (-1, -math.inf)

    for belt_dir in preferred_dirs:
        is_horiz = belt_dir in (DIRECTION_EAST, DIRECTION_WEST)
        perp_vals = ys if is_horiz else xs
        min_p, max_p = min(perp_vals), max(perp_vals)
        for line_c in range(min_p, max_p + 1):
            cand = _try_build_patch_row(
                commodity=commodity,
                patch_tiles=patch_tiles,
                belt_dir=belt_dir,
                belt_line_coord=line_c,
                required_drills=required_drills,
                output_fanout=output_fanout,
                obstacles=obstacles,
                tier=tier,
            )
            if cand is None:
                continue
            term = cand.output_terminals[0]
            dist = math.hypot(term[0] - target_anchor[0], term[1] - target_anchor[1])
            score = (cand.drill_count, -dist)
            if score > best_score:
                best_score = score
                best_result = cand
            if cand.drill_count >= required_drills:
                return cand

    if best_result is None:
        raise ValueError(f"Could not place any mining drills on patch '{commodity}'")
    return best_result


def _bootstrap_shoreline_footprint(px: float, py: float, p_dir: int) -> set[TileCoord]:
    """Return the temporary shoreline bootstrap footprint tiles around offshore-pump (px, py)."""
    po_dx, po_dy = _rotate_offset(2.0, 4.0, p_dir)
    a_dx, a_dy = _rotate_offset(2.0, 6.0, p_dir)
    l_dx, l_dy = _rotate_offset(5.0, 5.5, p_dir)
    s_dx, s_dy = _rotate_offset(-0.5, 4.5, p_dir)
    fp: set[TileCoord] = {
        (math.floor(px + po_dx), math.floor(py + po_dy)),
        (math.floor(px + s_dx), math.floor(py + s_dy)),
    }
    fp |= _footprint_tiles(px + a_dx, py + a_dy, 3, 3)
    fp |= _footprint_tiles(px + l_dx, py + l_dy, 3, 3)
    for fy_off in (2.0, 4.0, 6.0, 8.0):
        f_dx, f_dy = _rotate_offset(-4.5, fy_off, p_dir)
        fp |= _footprint_tiles(px + f_dx, py + f_dy, 2, 2)
    return fp


def synthesize_shoreline_power_array(
    water_tiles: set[TileCoord],
    required_power_watts: float,
    reference_pos: TileCoord,
    obstacles: set[TileCoord],
    data: solve.Data,
    tier: TechTierSpec,
) -> ShorelinePowerResult:
    """Place an offshore-pump, boiler, and steam-engine array along a valid shoreline."""
    se_recipe = data.recipes.get("steam-power", {})
    se_watts = float(se_recipe.get("power_output_watts", 900000.0))
    engine_count = max(1, math.ceil(required_power_watts / se_watts))
    boiler_count = max(1, math.ceil(engine_count / 2))

    best_res: ShorelinePowerResult | None = None
    best_score: tuple[int, float] = (-1, -math.inf)

    for wx, wy in sorted(water_tiles):
        for d_idx, p_dir in enumerate(CARDINAL_DIRS):
            w_dx, w_dy = _rotate_offset(0.0, -1.0, p_dir)
            px = round(wx - w_dx - TILE_HALF) + TILE_HALF
            py = round(wy - w_dy - TILE_HALF) + TILE_HALF
            pump_tile = (math.floor(px), math.floor(py))
            water_check = (math.floor(px + w_dx), math.floor(py + w_dy))
            water_deep = (math.floor(px + 2.0 * w_dx), math.floor(py + 2.0 * w_dy))
            lat_dx, lat_dy = _rotate_offset(1.0, 0.0, p_dir)
            water_lat_pos = (math.floor(px + w_dx + lat_dx), math.floor(py + w_dy + lat_dy))
            water_lat_neg = (math.floor(px + w_dx - lat_dx), math.floor(py + w_dy - lat_dy))
            ground_lat_pos = (math.floor(px + lat_dx), math.floor(py + lat_dy))
            ground_lat_neg = (math.floor(px - lat_dx), math.floor(py - lat_dy))
            if (
                water_check not in water_tiles
                or water_deep not in water_tiles
                or water_lat_pos not in water_tiles
                or water_lat_neg not in water_tiles
            ):
                continue
            if (
                pump_tile in water_tiles
                or pump_tile in obstacles
                or ground_lat_pos in water_tiles
                or ground_lat_neg in water_tiles
            ):
                continue

            dist = math.hypot(pump_tile[0] - reference_pos[0], pump_tile[1] - reference_pos[1])
            boot_fp = _bootstrap_shoreline_footprint(px, py, p_dir)
            boot_ok = 1 if not (boot_fp & water_tiles or boot_fp & obstacles) else 0
            cand_score = (boot_ok, -dist)
            if cand_score <= best_score:
                continue

            b_dir = CARDINAL_DIRS[(d_idx + 1) % 4]
            ins_dir = CARDINAL_DIRS[(d_idx + 2) % 4]
            b_dx, b_dy = _rotate_offset(0.5, 2.0, p_dir)
            e_dx, e_dy = _rotate_offset(4.0, 2.0, p_dir)
            ins_dx, ins_dy = _rotate_offset(0.0, 4.0, p_dir)
            fuel_dx, fuel_dy = _rotate_offset(0.0, 5.0, p_dir)

            bx, by = px + b_dx, py + b_dy
            ex, ey = px + e_dx, py + e_dy
            ix, iy = px + ins_dx, py + ins_dy
            fx, fy = px + fuel_dx, py + fuel_dy

            bw, bh = entity_size(tier.boiler_name, b_dir)
            ew, eh = entity_size(tier.generator_name, b_dir)
            b_fp = _footprint_tiles(bx, by, int(bw), int(bh))
            e_fp = _footprint_tiles(ex, ey, int(ew), int(eh))
            ins_tile = (math.floor(ix), math.floor(iy))
            fuel_tile = (math.floor(fx), math.floor(fy))

            b_step_dx, b_step_dy = DIR_STEPS[b_dir]
            fuel_approach = {
                (fuel_tile[0] - k * b_step_dx, fuel_tile[1] - k * b_step_dy) for k in (1, 2)
            }
            all_fp = {pump_tile, ins_tile, fuel_tile} | b_fp | e_fp
            if all_fp & water_tiles or all_fp & obstacles:
                continue
            if fuel_approach & water_tiles or fuel_approach & obstacles or fuel_approach & all_fp:
                continue

            entities: list[EntityDict] = [
                {
                    "name": tier.pump_name,
                    "position": {"x": round(px, COORD_PRECISION), "y": round(py, COORD_PRECISION)},
                    "direction": p_dir,
                },
                {
                    "name": tier.boiler_name,
                    "position": {"x": round(bx, COORD_PRECISION), "y": round(by, COORD_PRECISION)},
                    "direction": b_dir,
                },
                {
                    "name": tier.generator_name,
                    "position": {"x": round(ex, COORD_PRECISION), "y": round(ey, COORD_PRECISION)},
                    "direction": b_dir,
                },
                {
                    "name": tier.inserter_name,
                    "position": {"x": round(ix, COORD_PRECISION), "y": round(iy, COORD_PRECISION)},
                    "direction": ins_dir,
                },
                {
                    "name": tier.belt_name,
                    "position": {"x": round(fx, COORD_PRECISION), "y": round(fy, COORD_PRECISION)},
                    "direction": b_dir,
                },
            ]

            if engine_count >= 2:
                e2_dx, e2_dy = _rotate_offset(9.0, 2.0, p_dir)
                e2x, e2y = px + e2_dx, py + e2_dy
                e2_fp = _footprint_tiles(e2x, e2y, int(ew), int(eh))
                if not (e2_fp & water_tiles or e2_fp & obstacles):
                    entities.append(
                        {
                            "name": tier.generator_name,
                            "position": {
                                "x": round(e2x, COORD_PRECISION),
                                "y": round(e2y, COORD_PRECISION),
                            },
                            "direction": b_dir,
                        }
                    )
                    all_fp |= e2_fp

            best_score = cand_score
            best_res = ShorelinePowerResult(
                entities=entities,
                occupied_tiles=all_fp,
                fuel_input_terminal=fuel_tile,
                fuel_input_heading=b_dir,
                boiler_count=boiler_count,
                engine_count=min(engine_count, 2),
            )

    if best_res is None:
        raise ValueError("Could not find a valid shoreline site for the steam power array")
    return best_res


def _discover_smelting_recipes_for_closure(
    data: solve.Data,
    demand: dict[str, float],
    tier: TechTierSpec,
) -> tuple[list[tuple[str, str, str]], str]:
    """Return ordered (recipe_name, raw_input, plate_output) smelting lines and intermediate recipe."""
    recipes, _items = solve.closure(data, demand, tier.allowed_recipes)
    smelting_list: list[tuple[str, str, str]] = []
    for r_name, r_proto in sorted(recipes.items()):
        cats = r_proto.get("categories") or []
        if "smelting" not in cats:
            continue
        ings = data.ingredients_of(r_proto)
        results = data.results_of(r_proto)
        if not ings or not results:
            continue
        raw_in = solve.name_of(ings[0])
        if raw_in not in data.resources:
            continue
        plate_out = solve.name_of(results[0])
        smelting_list.append((r_name, raw_in, plate_out))

    plate_outputs = {p_out for _r, _in, p_out in smelting_list}
    inter_recipe = ""
    inter_plate = ""
    for r_name, r_proto in sorted(recipes.items()):
        if r_name in demand:
            continue
        cats = r_proto.get("categories") or ["crafting"]
        if "crafting" not in cats:
            continue
        ings = [solve.name_of(e) for e in data.ingredients_of(r_proto)]
        if len(ings) == 1 and ings[0] in plate_outputs:
            inter_recipe = r_name
            inter_plate = ings[0]
            break

    if inter_plate:
        smelting_list.sort(key=lambda item: (0 if item[2] == inter_plate else 1, item[0]))
    return smelting_list, inter_recipe


def synthesize_starter_base_core(
    origin: TileCoord,
    data: solve.Data,
    tier: TechTierSpec,
    science_recipe: str,
    mall_recipes: Sequence[str],
    furnaces_per_line: int = 2,
) -> StarterBaseCoreResult:
    """Synthesize a logistics-tier starter base core with smelting, mall, and lab."""
    ox, oy = origin
    entities: list[EntityDict] = []
    occupied: set[TileCoord] = set()
    input_terminals: dict[str, TileCoord] = {}
    input_headings: dict[str, int] = {}

    combined_demand: dict[str, float] = {science_recipe: 6.0}
    for m_rec in mall_recipes:
        combined_demand[m_rec] = combined_demand.get(m_rec, 0.0) + 3.0

    smelting_lines, gear_recipe = _discover_smelting_recipes_for_closure(
        data, combined_demand, tier
    )
    if not smelting_lines:
        raise ValueError("No smelting recipes discovered for starter base demand")

    fw_f, fh_f = entity_size(tier.furnace_name, DIRECTION_NORTH)
    fw, fh = int(fw_f), int(fh_f)
    aw_f, ah_f = entity_size(tier.assembler_name, DIRECTION_NORTH)
    aw, ah = int(aw_f), int(ah_f)
    lw_f, lh_f = entity_size(tier.lab_name, DIRECTION_NORTH)
    lw, lh = int(lw_f), int(lh_f)

    def _add_ent(
        name: str,
        cx: float,
        cy: float,
        direction: int = DIRECTION_NORTH,
        recipe: str | None = None,
        ug_type: str | None = None,
    ) -> None:
        ent: EntityDict = {
            "name": name,
            "position": {"x": round(cx, COORD_PRECISION), "y": round(cy, COORD_PRECISION)},
            "direction": direction,
        }
        if recipe is not None:
            ent["recipe"] = recipe
        if ug_type is not None:
            ent["type"] = ug_type
        entities.append(ent)
        ew, eh = entity_size(name, direction)
        occupied.update(_footprint_tiles(cx, cy, int(ew), int(eh)))

    # Place fuel input terminal and splitter feeding the smelting lines
    fuel_pin_y = oy + 1
    fuel_term = (ox, fuel_pin_y)
    input_terminals[tier.fuel_commodity] = fuel_term
    input_headings[tier.fuel_commodity] = DIRECTION_EAST
    _add_ent(tier.belt_name, ox + TILE_HALF, fuel_pin_y + TILE_HALF, DIRECTION_EAST)
    _add_ent(tier.splitter_name, ox + 1.5, fuel_pin_y + 1.0, DIRECTION_EAST)

    furnace_end_x = ox + 4 + furnaces_per_line * (fw + 1)
    turn_x = furnace_end_x + 1
    asm_start_x = turn_x + 2
    bus_end_x = asm_start_x + 8
    plate_bus_ys: dict[str, int] = {}

    for line_idx, (_r_name, raw_ore, plate_out) in enumerate(smelting_lines):
        row_y = oy + line_idx * 7
        ore_belt_y = row_y
        fuel_belt_y = row_y + 1
        ins_in_y = row_y + 2
        furnace_y0 = row_y + 3
        ins_out_y = row_y + 5
        plate_belt_y = row_y + 6
        plate_bus_ys[plate_out] = plate_belt_y

        ore_term = (ox, ore_belt_y)
        input_terminals[raw_ore] = ore_term
        input_headings[raw_ore] = DIRECTION_EAST

        # Route fuel branch from fuel splitter into this smelting row's fuel belt
        if line_idx == 0:
            for bx in range(ox + 2, furnace_end_x):
                _add_ent(tier.belt_name, bx + TILE_HALF, fuel_belt_y + TILE_HALF, DIRECTION_EAST)
            for bx in range(ox, furnace_end_x):
                _add_ent(tier.belt_name, bx + TILE_HALF, ore_belt_y + TILE_HALF, DIRECTION_EAST)
        else:
            drop_x = ox + 2
            prev_branch_y = oy + 2
            for by in range(prev_branch_y, fuel_belt_y):
                _add_ent(tier.belt_name, drop_x + TILE_HALF, by + TILE_HALF, DIRECTION_SOUTH)
            for bx in range(drop_x, furnace_end_x):
                _add_ent(tier.belt_name, bx + TILE_HALF, fuel_belt_y + TILE_HALF, DIRECTION_EAST)
            _add_ent(tier.belt_name, ox + TILE_HALF, ore_belt_y + TILE_HALF, DIRECTION_EAST)
            _add_ent(
                tier.underground_belt_name,
                ox + 1 + TILE_HALF,
                ore_belt_y + TILE_HALF,
                DIRECTION_EAST,
                ug_type="input",
            )
            _add_ent(
                tier.underground_belt_name,
                ox + 3 + TILE_HALF,
                ore_belt_y + TILE_HALF,
                DIRECTION_EAST,
                ug_type="output",
            )
            for bx in range(ox + 4, furnace_end_x):
                _add_ent(tier.belt_name, bx + TILE_HALF, ore_belt_y + TILE_HALF, DIRECTION_EAST)

        for f_idx in range(furnaces_per_line):
            fx0 = ox + 4 + f_idx * (fw + 1)
            fcx = fx0 + fw / HALF_DIVISOR
            fcy = furnace_y0 + fh / HALF_DIVISOR
            _add_ent(tier.furnace_name, fcx, fcy, DIRECTION_NORTH)
            _add_ent(
                tier.inserter_name,
                fx0 + TILE_HALF,
                ins_in_y + TILE_HALF,
                DIRECTION_NORTH,
            )
            _add_ent(
                tier.long_inserter_name,
                fx0 + 1.0 + TILE_HALF,
                ins_in_y + TILE_HALF,
                DIRECTION_NORTH,
            )
            _add_ent(
                tier.inserter_name,
                fx0 + TILE_HALF,
                ins_out_y + TILE_HALF,
                DIRECTION_NORTH,
            )

        if line_idx == 0 and len(smelting_lines) > 1:
            for bx in range(ox + 4, turn_x):
                _add_ent(tier.belt_name, bx + TILE_HALF, plate_belt_y + TILE_HALF, DIRECTION_EAST)
        else:
            for bx in range(ox + 4, bus_end_x):
                _add_ent(tier.belt_name, bx + TILE_HALF, plate_belt_y + TILE_HALF, DIRECTION_EAST)

    # Bring primary plate belt down next to secondary plate belt to form a 2-belt bus
    secondary_plate = smelting_lines[1][2] if len(smelting_lines) > 1 else smelting_lines[0][2]
    secondary_bus_y = plate_bus_ys[secondary_plate]
    primary_bus_y = (
        secondary_bus_y - 1 if len(smelting_lines) > 1 else plate_bus_ys[smelting_lines[0][2]]
    )
    if len(smelting_lines) > 1:
        top_plate_y = plate_bus_ys[smelting_lines[0][2]]
        for by in range(top_plate_y, primary_bus_y):
            _add_ent(tier.belt_name, turn_x + TILE_HALF, by + TILE_HALF, DIRECTION_SOUTH)
        for bx in range(turn_x, bus_end_x):
            _add_ent(tier.belt_name, bx + TILE_HALF, primary_bus_y + TILE_HALF, DIRECTION_EAST)

    if not gear_recipe:
        gear_recipe = smelting_lines[0][2]

    # North side of 2-belt bus: Gear Assembler A -> Bootstrap Mall Assembler
    asm_n_ins_y = primary_bus_y - 1
    asm_n_y0 = asm_n_ins_y - ah
    a0_x = asm_start_x
    _add_ent(
        tier.assembler_name,
        a0_x + aw / HALF_DIVISOR,
        asm_n_y0 + ah / HALF_DIVISOR,
        DIRECTION_NORTH,
        recipe=gear_recipe,
    )
    _add_ent(tier.inserter_name, a0_x + TILE_HALF, asm_n_ins_y + TILE_HALF, DIRECTION_SOUTH)

    mall_rec_0 = mall_recipes[0] if mall_recipes else science_recipe
    _add_ent(
        tier.inserter_name,
        a0_x + aw + TILE_HALF,
        asm_n_y0 + 1.0 + TILE_HALF,
        DIRECTION_WEST,
    )
    a1_x = a0_x + aw + 1
    _add_ent(
        tier.assembler_name,
        a1_x + aw / HALF_DIVISOR,
        asm_n_y0 + ah / HALF_DIVISOR,
        DIRECTION_NORTH,
        recipe=mall_rec_0,
    )
    _add_ent(tier.inserter_name, a1_x + TILE_HALF, asm_n_ins_y + TILE_HALF, DIRECTION_SOUTH)
    _add_ent(
        tier.inserter_name,
        a1_x + 1.0 + TILE_HALF,
        asm_n_y0 - 1 + TILE_HALF,
        DIRECTION_SOUTH,
    )
    _add_ent(
        tier.belt_name,
        a1_x + 1.0 + TILE_HALF,
        asm_n_y0 - 2 + TILE_HALF,
        DIRECTION_NORTH,
    )

    # South side of 2-belt bus: Gear Assembler B -> Science Assembler -> Lab
    sci_ins_y = secondary_bus_y + 1
    sci_y0 = secondary_bus_y + 2
    g2_x = asm_start_x
    _add_ent(
        tier.assembler_name,
        g2_x + aw / HALF_DIVISOR,
        sci_y0 + ah / HALF_DIVISOR,
        DIRECTION_NORTH,
        recipe=gear_recipe,
    )
    # Long-handed inserter picks primary_plate from primary_bus_y (2 tiles North of sci_ins_y)
    _add_ent(
        tier.long_inserter_name,
        g2_x + 1.0 + TILE_HALF,
        sci_ins_y + TILE_HALF,
        DIRECTION_NORTH,
    )
    _add_ent(
        tier.inserter_name,
        g2_x + aw + TILE_HALF,
        sci_y0 + 1.0 + TILE_HALF,
        DIRECTION_WEST,
    )

    sci_x = g2_x + aw + 1
    _add_ent(
        tier.assembler_name,
        sci_x + aw / HALF_DIVISOR,
        sci_y0 + ah / HALF_DIVISOR,
        DIRECTION_NORTH,
        recipe=science_recipe,
    )
    _add_ent(tier.inserter_name, sci_x + TILE_HALF, sci_ins_y + TILE_HALF, DIRECTION_NORTH)

    _add_ent(
        tier.inserter_name,
        sci_x + aw + TILE_HALF,
        sci_y0 + 1.0 + TILE_HALF,
        DIRECTION_WEST,
    )
    lab_x = sci_x + aw + 1
    _add_ent(
        tier.lab_name,
        lab_x + lw / HALF_DIVISOR,
        sci_y0 + lh / HALF_DIVISOR,
        DIRECTION_NORTH,
    )

    xs = [pt[0] for pt in occupied]
    ys = [pt[1] for pt in occupied]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    return StarterBaseCoreResult(
        entities=entities,
        occupied_tiles=occupied,
        input_terminals=input_terminals,
        input_headings=input_headings,
        internal_nets=[],
        bounds=bounds,
    )


def _select_base_origin(
    surface: MapSurfaceSpec,
    power_array: ShorelinePowerResult,
    base_width: int = 24,
    base_height: int = 20,
) -> TileCoord:
    """Find an obstacle-free bounding box for the starter base core near patches and power."""
    primary_patches = select_primary_patch_clusters(
        surface.resource_patches,
        reference_pos=power_array.fuel_input_terminal,
    )
    patch_centers = [
        (
            sum(pt[0] for pt in pts) / len(pts),
            sum(pt[1] for pt in pts) / len(pts),
        )
        for pts in primary_patches.values()
        if pts
    ]
    all_pts = [*patch_centers, power_array.fuel_input_terminal]
    target_x = round(sum(p[0] for p in all_pts) / len(all_pts))
    target_y = round(sum(p[1] for p in all_pts) / len(all_pts))

    blocked = (
        surface.water_tiles
        | surface.obstacles
        | power_array.occupied_tiles
        | {pt for pts in surface.resource_patches.values() for pt in pts}
    )

    best_origin: TileCoord | None = None
    best_score = math.inf
    margin = 6
    for ox in range(surface.min_x + margin, surface.max_x - base_width - margin + 1, 2):
        for oy in range(surface.min_y + margin, surface.max_y - base_height - margin + 1, 2):
            west_penalty = 0.0 if ox >= target_x + 4 else (target_x + 4 - ox) * 1.25
            score = math.hypot(ox - target_x, oy - target_y) + west_penalty
            if score >= best_score:
                continue
            box_tiles = {
                (tx, ty)
                for tx in range(ox - 5, ox + base_width + 2)
                for ty in range(oy - 2, oy + base_height + 2)
            }
            if not (box_tiles & blocked):
                best_score = score
                best_origin = (ox, oy)

    if best_origin is None:
        raise ValueError("Could not find an obstacle-free site for the starter base core")
    return best_origin


def _mst_edges_to_wires(
    entities: list[EntityDict],
    pole_entities: Mapping[GridPos, EntityDict],
    mst_edges: Sequence[MstEdge],
) -> list[list[int]]:
    """Assign entity_number to all entities and convert PDN MST edges to copper wire tuples."""
    for idx, ent in enumerate(entities, start=1):
        ent["entity_number"] = idx

    wires: list[list[int]] = []
    for p1, p2 in mst_edges:
        e1 = pole_entities.get(p1)
        e2 = pole_entities.get(p2)
        if e1 is None or e2 is None:
            continue
        n1 = int(e1["entity_number"])
        n2 = int(e2["entity_number"])
        if n1 != n2:
            wires.append([min(n1, n2), WIRE_COPPER_ID, max(n1, n2), WIRE_COPPER_ID])
    return wires


def route_patch_to_base(
    surface: MapSurfaceSpec,
    data: solve.Data,
    science_recipe: str = "automation-science-pack",
    mall_recipes: Sequence[str] = ("transport-belt",),
    unlocked_by: str = "logistics",
    base_origin: TileCoord | None = None,
    engine: FactorioNativeEngine | None = None,
    verify: bool = True,
) -> PatchToBaseResult:
    """Synthesize and route a complete patch-to-base starter layout on surface."""
    tier = resolve_tech_tier(data, unlocked_by=unlocked_by)

    demand: dict[str, float] = {science_recipe: 6.0}
    for m_rec in mall_recipes:
        demand[m_rec] = demand.get(m_rec, 0.0) + 3.0
    rates, raw, _surplus = solve.solve_rates(data, demand, tier.allowed_recipes)
    _lines, base_power_watts = solve.machine_counts(data, rates, allowed=tier.allowed_recipes)

    center_ref = (
        (surface.min_x + surface.max_x) // 2,
        (surface.min_y + surface.max_y) // 2,
    )
    initial_clusters = select_primary_patch_clusters(
        surface.resource_patches,
        reference_pos=center_ref,
    )
    ref_pt = center_ref
    if initial_clusters.get(tier.fuel_commodity):
        f_pts = initial_clusters[tier.fuel_commodity]
        ref_pt = (
            round(sum(p[0] for p in f_pts) / len(f_pts)),
            round(sum(p[1] for p in f_pts) / len(f_pts)),
        )

    all_patch_tiles = {pt for pts in surface.resource_patches.values() for pt in pts}
    power_obstacles = surface.obstacles | all_patch_tiles
    power_array = synthesize_shoreline_power_array(
        water_tiles=surface.water_tiles,
        required_power_watts=base_power_watts + 6.0 * tier.drill_watts,
        reference_pos=ref_pt,
        obstacles=power_obstacles,
        data=data,
        tier=tier,
    )

    primary_patches = select_primary_patch_clusters(
        surface.resource_patches,
        reference_pos=power_array.fuel_input_terminal,
    )
    chosen_origin = (
        base_origin if base_origin is not None else _select_base_origin(surface, power_array)
    )
    base_core = synthesize_starter_base_core(
        origin=chosen_origin,
        data=data,
        tier=tier,
        science_recipe=science_recipe,
        mall_recipes=mall_recipes,
    )

    # Synthesize miner arrays on each resource patch required by base_core + power_array
    patch_arrays: dict[str, PatchArrayResult] = {}
    placed_obstacles = (
        set(surface.water_tiles)
        | set(surface.obstacles)
        | set(power_array.occupied_tiles)
        | set(base_core.occupied_tiles)
    )

    for comm, base_term in sorted(base_core.input_terminals.items()):
        if comm not in primary_patches:
            raise ValueError(f"Required resource patch '{comm}' not present on map surface")
        is_fuel = comm == tier.fuel_commodity
        fanout = 2 if is_fuel else 1
        min_rate = 90.0 if comm == "iron-ore" else (60.0 if is_fuel else 30.0)
        target_rate = max(min_rate, float(raw.get(comm, min_rate)))
        p_array = synthesize_patch_miner_array(
            commodity=comm,
            patch_tiles=primary_patches[comm],
            target_rate_per_min=target_rate,
            target_anchor=base_term,
            obstacles=placed_obstacles,
            data=data,
            tier=tier,
            output_fanout=fanout,
        )
        patch_arrays[comm] = p_array
        placed_obstacles.update(p_array.occupied_tiles)

    # Build trunk RouterNetSpecs from patch output terminals to base and boiler input terminals
    trunk_nets: list[RouterNetSpec] = []
    router_obstacles = set(placed_obstacles)
    component_headings: dict[TileCoord, int] = {}

    for comm, p_array in sorted(patch_arrays.items()):
        src_pt = p_array.output_terminals[0]
        snk_pt = base_core.input_terminals[comm]
        snk_heading = base_core.input_headings[comm]
        router_obstacles.discard(src_pt)
        router_obstacles.discard(snk_pt)
        component_headings[src_pt] = p_array.output_heading
        component_headings[snk_pt] = snk_heading
        trunk_nets.append(
            RouterNetSpec(
                name=f"trunk_{comm}_base",
                commodity=comm,
                source=src_pt,
                sink=snk_pt,
                is_fluid=False,
                start_heading=p_array.output_heading,
                end_heading=snk_heading,
            )
        )
        if comm == tier.fuel_commodity and len(p_array.output_terminals) > 1:
            boiler_src = p_array.output_terminals[1]
            boiler_snk = power_array.fuel_input_terminal
            boiler_heading = power_array.fuel_input_heading
            router_obstacles.discard(boiler_src)
            router_obstacles.discard(boiler_snk)
            component_headings[boiler_src] = p_array.output_heading
            component_headings[boiler_snk] = boiler_heading
            trunk_nets.append(
                RouterNetSpec(
                    name=f"trunk_{comm}_boiler",
                    commodity=comm,
                    source=boiler_src,
                    sink=boiler_snk,
                    is_fluid=False,
                    start_heading=p_array.output_heading,
                    end_heading=boiler_heading,
                )
            )

    active_tiles = (
        set(power_array.occupied_tiles)
        | set(base_core.occupied_tiles)
        | {pt for pa in patch_arrays.values() for pt in pa.occupied_tiles}
    )
    act_xs = [pt[0] for pt in active_tiles]
    act_ys = [pt[1] for pt in active_tiles]
    r_margin = 28
    r_min_x = max(surface.min_x, min(act_xs) - r_margin)
    r_max_x = min(surface.max_x, max(act_xs) + r_margin)
    r_min_y = max(surface.min_y, min(act_ys) - r_margin)
    r_max_y = min(surface.max_y, max(act_ys) + r_margin)

    router = DualLaneMazeRouter(
        min_x=r_min_x,
        max_x=r_max_x,
        min_y=r_min_y,
        max_y=r_max_y,
        engine=engine,
        prototypes=tier.filtered_prototypes,
        obstacles=router_obstacles,
        component_headings=component_headings,
    )
    trunk_routes, trunk_entities, verif_res = router.route(trunk_nets, verify=verify)
    failed = [r for r in trunk_routes.values() if not r.is_success]
    if failed:
        names = ", ".join(r.net_name for r in failed)
        raise RuntimeError(f"Patch-to-base trunk routing failed on net(s): {names}")

    # Combine all entities, deduping shared terminal belt tiles
    combined_by_pos: dict[GridPos, EntityDict] = {}
    all_blocks: list[EntityDict] = [
        *power_array.entities,
        *base_core.entities,
        *(ent for pa in patch_arrays.values() for ent in pa.entities),
        *trunk_entities,
    ]
    for ent in all_blocks:
        pos = ent["position"]
        key = (round(float(pos["x"]), COORD_PRECISION), round(float(pos["y"]), COORD_PRECISION))
        combined_by_pos[key] = copy.deepcopy(ent)

    entities = list(combined_by_pos.values())

    # Build occupied positions including water tiles so PDN poles never land on water or belts
    pdn_occupied: set[GridPos] = {
        (round(wx + TILE_HALF, COORD_PRECISION), round(wy + TILE_HALF, COORD_PRECISION))
        for wx, wy in surface.water_tiles | surface.obstacles
    }
    for ent in entities:
        pos = ent["position"]
        ex, ey = float(pos["x"]), float(pos["y"])
        ew, eh = entity_size(str(ent["name"]), int(ent.get("direction", DIRECTION_NORTH)))
        for tx, ty in _footprint_tiles(ex, ey, int(ew), int(eh)):
            pdn_occupied.add(
                (round(tx + TILE_HALF, COORD_PRECISION), round(ty + TILE_HALF, COORD_PRECISION))
            )

    # Reserve the 2x2 burner smelter footprints in front of iron-ore drills so PDN poles never collide
    iron_pa = patch_arrays.get("iron-ore")
    if iron_pa is not None:
        for ent in iron_pa.entities:
            if ent["name"] == tier.drill_name:
                pos = ent["position"]
                dx_pos, dy_pos = float(pos["x"]), float(pos["y"])
                d_dir = int(ent.get("direction", DIRECTION_NORTH))
                step_x, step_y = DIR_STEPS[d_dir]
                sm_x = dx_pos + 2.5 * step_x + (0.5 if step_x == 0 else 0.0)
                sm_y = dy_pos + 2.5 * step_y + (0.5 if step_y == 0 else 0.0)
                for tx, ty in _footprint_tiles(sm_x, sm_y, 2, 2):
                    pdn_occupied.add(
                        (
                            round(tx + TILE_HALF, COORD_PRECISION),
                            round(ty + TILE_HALF, COORD_PRECISION),
                        )
                    )

    # Reserve the temporary shoreline bootstrap footprint so PDN poles never collide with lab/asm/f1..f4
    for ent in power_array.entities:
        if ent["name"] == tier.pump_name:
            pos = ent["position"]
            px_pos, py_pos = float(pos["x"]), float(pos["y"])
            p_dir = int(ent.get("direction", DIRECTION_NORTH))
            for tx, ty in _bootstrap_shoreline_footprint(px_pos, py_pos, p_dir):
                pdn_occupied.add(
                    (
                        round(tx + TILE_HALF, COORD_PRECISION),
                        round(ty + TILE_HALF, COORD_PRECISION),
                    )
                )

    pdn_cfg = make_pdn_config(tier.pole_name)
    pole_entities, mst_edges = generate_pdn(
        entities=entities,
        occupied_positions=pdn_occupied,
        pin_tiles=set(),
        valid_input_pins=set(),
        valid_output_pins=set(),
        config=pdn_cfg,
    )

    collision_pairs = check_collisions(entities)
    if collision_pairs:
        raise RuntimeError(f"Patch-to-base layout has {len(collision_pairs)} entity collision(s)")
    unpaired_ug = check_underground_pairing(entities)
    if unpaired_ug:
        raise RuntimeError(f"Patch-to-base layout has {len(unpaired_ug)} unpaired underground(s)")
    unpowered = check_power(entities, supply_radius=pdn_cfg.supply_radius)
    if unpowered:
        raise RuntimeError(f"Patch-to-base layout has {len(unpowered)} unpowered entities")

    if verify:
        verifier = NativeLayoutVerifier(engine, prototypes=tier.filtered_prototypes)
        verifier_nets = [
            NetSpec(
                name=n.name,
                commodity=n.commodity,
                source_pos=(n.source[0] + TILE_HALF, n.source[1] + TILE_HALF),
                sink_pos=(n.sink[0] + TILE_HALF, n.sink[1] + TILE_HALF),
                is_fluid=n.is_fluid,
            )
            for n in trunk_nets
        ]
        verif_res = verifier.verify_blueprint(entities, nets=verifier_nets, ticks=120)
        if not verif_res.is_valid:
            raise RuntimeError(
                f"Native layout verification failed: drc={verif_res.drc_violations}, lvs={verif_res.lvs_errors}"
            )

    wires = _mst_edges_to_wires(entities, pole_entities, mst_edges)
    bp_str = blueprint.encode(entities, label="patch_to_base_starter", wires=wires)

    return PatchToBaseResult(
        entities=entities,
        wires=wires,
        blueprint_string=bp_str,
        patch_arrays=patch_arrays,
        power_array=power_array,
        base_core=base_core,
        trunk_routes=trunk_routes,
        trunk_nets=trunk_nets,
        mst_edges=mst_edges,
        verification=verif_res,
    )
