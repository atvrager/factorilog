"""Autonomous bot agent that synthesizes and builds the starter base from cartography.

Reads surface telemetry through MapCartographer, synthesizes the patch-to-base
starter layout and bootstrap mall, computes the obstacle pre-clearing schedule,
orders entity construction by power and material dependencies, and emits
Factorio InputAction sequences within character reach limits.
All docstrings and comments follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foundry import solve
from foundry.campaign.cartographer import MapCartographer, TerrainObstacle
from foundry.entity_helpers import DIRECTION_NORTH, entity_size, register_prototype_boxes
from foundry.mp_proxy import (
    FactorioLockstepProxy,
    PlacedSurfaceEntity,
    PlayerWorldState,
    build_begin_mining_action,
    build_clear_cursor_action,
    build_close_gui_action,
    build_fast_entity_split_action,
    build_fast_entity_transfer_action,
    build_open_gui_action,
    build_pipette_item_action,
    build_place_entity_action,
    build_selected_entity_action,
    build_setup_assembling_machine_action,
    build_start_walking_action,
    build_stop_mining_action,
    build_stop_walking_action,
)
from foundry.native_engine.engine import FactorioNativeEngine
from foundry.patch_router import (
    MapSurfaceSpec,
    PatchToBaseResult,
    TileCoord,
    resolve_tech_tier,
    route_patch_to_base,
)

PHASE_CLEAR_OBSTACLES = "clear_obstacles"
PHASE_SHORELINE_POWER = "shoreline_power"
PHASE_PATCH_MINERS = "patch_miners"
PHASE_POWER_NETWORK = "power_network"
PHASE_TRUNK_BELTS = "trunk_belts"
PHASE_STARTER_BASE_CORE = "starter_base_core"

ALL_BUILD_PHASES: tuple[str, ...] = (
    PHASE_CLEAR_OBSTACLES,
    PHASE_SHORELINE_POWER,
    PHASE_PATCH_MINERS,
    PHASE_POWER_NETWORK,
    PHASE_TRUNK_BELTS,
    PHASE_STARTER_BASE_CORE,
)

TASK_CLEAR_OBSTACLE = "clear_obstacle"
TASK_PLACE_ENTITY = "place_entity"
TASK_CONFIGURE_RECIPE = "configure_recipe"
TASK_SEED_FUEL = "seed_fuel"
TASK_COLLECT_MALL_OUTPUT = "collect_mall_output"

TILE_CENTER_OFFSET: float = 0.5
COORD_ROUND_DIGITS: int = 2
REACH_SAFETY_MARGIN: float = 3.2
SEARCH_RADIUS_PADDING: int = 2
DEFAULT_SEED_FUEL_COUNT: int = 50
DEFAULT_STOP_TOLERANCE: float = 1.6
CARDINAL_AXIS_RATIO: float = 0.65
WALK_REISSUE_TICKS: int = 24
BOX_EPSILON: float = 1e-6
HALF_DIVISOR: float = 2.0


@dataclass(frozen=True, slots=True)
class CharacterReachLimits:
    """Character build, interaction, and resource mining reach limits from prototype data."""

    build_reach: float
    entity_reach: float
    resource_reach: float
    running_rate: float


def extract_character_reach(data: solve.Data) -> CharacterReachLimits:
    """Extract character reach and movement parameters dynamically from prototype data."""
    char_table = data.prototypes.get("character")
    if not isinstance(char_table, Mapping) or "character" not in char_table:
        raise KeyError("character prototype table not found in solve.Data.prototypes")
    proto = char_table["character"]
    if not isinstance(proto, Mapping):
        raise KeyError("character.character entry is not a mapping")

    build_val = float(proto["build_distance"])
    reach_val = float(proto["reach_distance"])
    res_val = float(proto["reach_resource_distance"])
    run_val = float(proto["running_speed"])
    return CharacterReachLimits(
        build_reach=build_val,
        entity_reach=reach_val,
        resource_reach=res_val,
        running_rate=run_val,
    )


@dataclass(frozen=True, slots=True)
class BotBuildTask:
    """Single physical operation in the starter base construction schedule."""

    task_index: int
    phase: str
    task_kind: str
    target_name: str
    target_x: float
    target_y: float
    stand_x: float
    stand_y: float
    direction: int = DIRECTION_NORTH
    entity_type: str | None = None
    recipe: str | None = None
    unit_number: int | None = None
    item_count: int = 1


@dataclass(slots=True)
class StarterBaseBuildPlan:
    """Complete starter base synthesis result and dependency-ordered construction schedule."""

    surface: MapSurfaceSpec
    layout: PatchToBaseResult
    obstacles_to_clear: list[TerrainObstacle]
    bill_of_materials: dict[str, int]
    reach_limits: CharacterReachLimits
    tasks: list[BotBuildTask]
    fuel_commodity: str


@dataclass(frozen=True, slots=True)
class BomFabricationStep:
    """Single intermediate or final recipe step in the bootstrap BOM fabrication schedule."""

    recipe_name: str
    item_name: str
    craft_count: int
    total_yield: int
    ingredients: dict[str, int]


@dataclass(frozen=True, slots=True)
class BomFabricationPlan:
    """Dynamic BOM fabrication plan derived from prototype recipes and starting inventory."""

    starting_inventory: dict[str, int]
    missing_bom: dict[str, int]
    raw_materials_needed: dict[str, int]
    smelting_steps: list[BomFabricationStep]
    assembler_steps: list[BomFabricationStep]


def compute_bom_fabrication_plan(
    target_bom: Mapping[str, int],
    data: solve.Data,
    starting_inventory: Mapping[str, int] | None = None,
    fuel_commodity: str = "coal",
    seed_fuel_count: int = DEFAULT_SEED_FUEL_COUNT,
) -> BomFabricationPlan:
    """Compute recursive raw material, smelting, and assembler steps to fabricate a BOM."""
    inv: dict[str, int] = {k: int(v) for k, v in (starting_inventory or {}).items() if int(v) > 0}
    avail: dict[str, int] = dict(inv)

    missing_bom: dict[str, int] = {}
    for item_name, req_qty in sorted(target_bom.items()):
        have = avail.get(item_name, 0)
        used = min(have, int(req_qty))
        avail[item_name] = have - used
        rem = int(req_qty) - used
        if rem > 0:
            missing_bom[item_name] = rem

    smelt_categories = {"smelting"}
    craft_demands: dict[str, int] = {}
    craft_depth: dict[str, int] = {}
    raw_needed: dict[str, int] = {}

    def _select_recipe(item_name: str) -> Mapping[str, Any] | None:
        if item_name in data.recipes:
            return data.recipes[item_name]
        cands = data.producers.get(item_name, [])
        return data.recipes[cands[0]] if cands else None

    def _expand(item_name: str, needed_qty: int, depth: int) -> None:
        if needed_qty <= 0:
            return
        have = avail.get(item_name, 0)
        if have > 0:
            take = min(have, needed_qty)
            avail[item_name] = have - take
            needed_qty -= take
            if needed_qty <= 0:
                return

        if item_name in data.resources or item_name == "wood":
            raw_needed[item_name] = raw_needed.get(item_name, 0) + needed_qty
            return

        rec = _select_recipe(item_name)
        if rec is None:
            raw_needed[item_name] = raw_needed.get(item_name, 0) + needed_qty
            return

        res_list = data.results_of(rec)
        yield_per_craft = max(
            1,
            round(
                sum(solve.amount_of(r) for r in res_list if solve.name_of(r) == item_name) or 1.0
            ),
        )
        crafts = math.ceil(needed_qty / yield_per_craft)
        produced = crafts * yield_per_craft
        surplus = produced - needed_qty
        if surplus > 0:
            avail[item_name] = avail.get(item_name, 0) + surplus

        r_name = str(rec.get("name", item_name))
        craft_demands[r_name] = craft_demands.get(r_name, 0) + crafts
        craft_depth[r_name] = max(craft_depth.get(r_name, 0), depth)

        for ing in data.ingredients_of(rec):
            ing_name = solve.name_of(ing)
            ing_amt = round(solve.amount_of(ing)) * crafts
            _expand(ing_name, ing_amt, depth + 1)

    for item_name, rem_qty in sorted(missing_bom.items()):
        _expand(item_name, rem_qty, 0)

    smelting_steps: list[BomFabricationStep] = []
    assembler_steps: list[BomFabricationStep] = []

    ordered_recipes = sorted(
        craft_demands.keys(),
        key=lambda r: (-craft_depth[r], r),
    )
    for r_name in ordered_recipes:
        rec = data.recipes[r_name]
        crafts = craft_demands[r_name]
        res_list = data.results_of(rec)
        item_name = solve.name_of(res_list[0]) if res_list else r_name
        yield_per_craft = max(
            1,
            round(
                sum(solve.amount_of(r) for r in res_list if solve.name_of(r) == item_name) or 1.0
            ),
        )
        ings = {
            solve.name_of(ing): round(solve.amount_of(ing)) * crafts
            for ing in data.ingredients_of(rec)
        }
        step = BomFabricationStep(
            recipe_name=r_name,
            item_name=item_name,
            craft_count=crafts,
            total_yield=crafts * yield_per_craft,
            ingredients=ings,
        )
        cat = str(rec.get("category", "crafting"))
        cats = set(rec.get("categories", [cat]))
        if cats & smelt_categories:
            smelting_steps.append(step)
        else:
            assembler_steps.append(step)

    total_smelt_crafts = sum(s.craft_count for s in smelting_steps)
    smelt_fuel = max(5, math.ceil(total_smelt_crafts * 0.1)) if total_smelt_crafts > 0 else 0
    total_fuel_req = seed_fuel_count + smelt_fuel
    have_fuel = avail.get(fuel_commodity, 0)
    used_fuel = min(have_fuel, total_fuel_req)
    avail[fuel_commodity] = have_fuel - used_fuel
    rem_fuel = total_fuel_req - used_fuel
    if rem_fuel > 0:
        raw_needed[fuel_commodity] = raw_needed.get(fuel_commodity, 0) + rem_fuel

    return BomFabricationPlan(
        starting_inventory=inv,
        missing_bom=missing_bom,
        raw_materials_needed=dict(sorted(raw_needed.items())),
        smelting_steps=smelting_steps,
        assembler_steps=assembler_steps,
    )


def compute_bill_of_materials(entities: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Count required item quantities for all planned entities."""
    bom: dict[str, int] = {}
    for ent in entities:
        name = str(ent["name"])
        bom[name] = bom.get(name, 0) + 1
    return dict(sorted(bom.items()))


def _entity_pos_key(ent: Mapping[str, Any]) -> tuple[float, float]:
    """Return rounded (x, y) center coordinate key for a blueprint entity."""
    pos = ent["position"]
    return (
        round(float(pos["x"]), COORD_ROUND_DIGITS),
        round(float(pos["y"]), COORD_ROUND_DIGITS),
    )


DIRECTIONLESS_ENTITY_NAMES: frozenset[str] = frozenset(
    {
        "small-electric-pole",
        "medium-electric-pole",
        "big-electric-pole",
        "substation",
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "assembling-machine-1",
        "assembling-machine-2",
        "assembling-machine-3",
        "lab",
        "wooden-chest",
        "iron-chest",
        "steel-chest",
    }
)


SYMMETRIC_AXIS_ENTITY_NAMES: frozenset[str] = frozenset(
    {
        "steam-engine",
    }
)


def verify_placed_entities(
    planned_entities: Sequence[Mapping[str, Any]],
    surface_entities: Sequence[PlacedSurfaceEntity | Mapping[str, Any]],
) -> list[str]:
    """Verify that 100% of planned_entities exist on the Factorio surface with matching attributes."""
    by_pos: dict[tuple[float, float], PlacedSurfaceEntity] = {}
    for raw in surface_entities:
        if isinstance(raw, Mapping):
            pos = raw.get("position")
            if isinstance(pos, Mapping):
                rx = float(pos["x"])
                ry = float(pos["y"])
            else:
                rx = float(raw["x"])
                ry = float(raw["y"])
            ug_val = raw.get("type")
            rec_val = raw.get("recipe")
            ent = PlacedSurfaceEntity(
                name=str(raw["name"]),
                x=rx,
                y=ry,
                direction=int(raw.get("direction", DIRECTION_NORTH)),
                entity_type=str(ug_val) if isinstance(ug_val, str) and ug_val else None,
                recipe=str(rec_val) if isinstance(rec_val, str) and rec_val else None,
            )
        else:
            ent = raw
        key = (round(ent.x, COORD_ROUND_DIGITS), round(ent.y, COORD_ROUND_DIGITS))
        by_pos[key] = ent

    errors: list[str] = []
    for planned in planned_entities:
        key = _entity_pos_key(planned)
        exp_name = str(planned["name"])
        exp_dir = int(planned.get("direction", DIRECTION_NORTH))
        exp_ug = str(planned["type"]) if planned.get("type") else None
        exp_rec = str(planned["recipe"]) if planned.get("recipe") else None

        actual = by_pos.get(key)
        if actual is None:
            errors.append(f"Missing {exp_name} at {key}")
            continue
        if actual.name != exp_name:
            errors.append(f"Name mismatch at {key}: expected {exp_name}, got {actual.name}")
            continue
        if exp_name in SYMMETRIC_AXIS_ENTITY_NAMES:
            if (actual.direction % 8) != (exp_dir % 8):
                errors.append(
                    f"Axis mismatch for {exp_name} at {key}: expected {exp_dir}, got {actual.direction}"
                )
        elif exp_name not in DIRECTIONLESS_ENTITY_NAMES and actual.direction != exp_dir:
            errors.append(
                f"Direction mismatch for {exp_name} at {key}: expected {exp_dir}, got {actual.direction}"
            )
        if exp_ug is not None and actual.entity_type != exp_ug:
            errors.append(
                f"Underground belt type mismatch for {exp_name} at {key}: expected {exp_ug}, got {actual.entity_type}"
            )
        if exp_rec is not None and actual.recipe != exp_rec:
            errors.append(
                f"Recipe mismatch for {exp_name} at {key}: expected {exp_rec}, got {actual.recipe}"
            )
    return errors


def _entity_footprint_tiles(
    ent: Mapping[str, Any],
    data: solve.Data,
) -> set[TileCoord]:
    """Return the integer tile coordinates occupied by one entity."""
    register_prototype_boxes(data.prototypes)
    name = str(ent["name"])
    direction = int(ent.get("direction", DIRECTION_NORTH))
    pos = ent["position"]
    cx = float(pos["x"])
    cy = float(pos["y"])
    w_f, h_f = entity_size(name, direction)
    w, h = int(w_f), int(h_f)
    x0 = math.floor(cx - w / HALF_DIVISOR + BOX_EPSILON)
    y0 = math.floor(cy - h / HALF_DIVISOR + BOX_EPSILON)
    return {(x0 + dx, y0 + dy) for dx in range(w) for dy in range(h)}


def select_standing_position(
    target_x: float,
    target_y: float,
    reach_limit: float,
    water_tiles: set[TileCoord],
    blocked_tiles: set[TileCoord],
    reference_pos: tuple[float, float],
) -> tuple[float, float]:
    """Select a walkable tile center within reach_limit of (target_x, target_y)."""
    usable_reach = max(TILE_CENTER_OFFSET, reach_limit - REACH_SAFETY_MARGIN)
    rx, ry = reference_pos
    # If the current reference_pos is already valid and within reach, keep standing there
    ref_tile = (math.floor(rx), math.floor(ry))
    if (
        ref_tile not in water_tiles
        and ref_tile not in blocked_tiles
        and math.hypot(target_x - rx, target_y - ry) <= usable_reach
    ):
        return (round(rx, COORD_ROUND_DIGITS), round(ry, COORD_ROUND_DIGITS))

    center_tx = math.floor(target_x)
    center_ty = math.floor(target_y)
    search_rad = math.ceil(usable_reach) + SEARCH_RADIUS_PADDING

    best_pos: tuple[float, float] | None = None
    best_score: tuple[float, float, float, float] | None = None

    for tx in range(center_tx - search_rad, center_tx + search_rad + 1):
        for ty in range(center_ty - search_rad, center_ty + search_rad + 1):
            tile = (tx, ty)
            if tile in water_tiles or tile in blocked_tiles:
                continue
            cand_x = tx + TILE_CENTER_OFFSET
            cand_y = ty + TILE_CENTER_OFFSET
            dist_to_target = math.hypot(target_x - cand_x, target_y - cand_y)
            if dist_to_target > usable_reach or dist_to_target < TILE_CENTER_OFFSET:
                continue
            dist_from_ref = math.hypot(cand_x - rx, cand_y - ry)
            score = (
                round(dist_from_ref, COORD_ROUND_DIGITS),
                round(dist_to_target, COORD_ROUND_DIGITS),
                cand_x,
                cand_y,
            )
            if best_score is None or score < best_score:
                best_score = score
                best_pos = (cand_x, cand_y)

    if best_pos is not None:
        return best_pos

    # Fallback when entities occupy all nearby tiles: stand adjacent to target outside water
    for tx in range(center_tx - search_rad, center_tx + search_rad + 1):
        for ty in range(center_ty - search_rad, center_ty + search_rad + 1):
            if (tx, ty) in water_tiles:
                continue
            cand_x = tx + TILE_CENTER_OFFSET
            cand_y = ty + TILE_CENTER_OFFSET
            if math.hypot(target_x - cand_x, target_y - cand_y) <= reach_limit:
                return (cand_x, cand_y)

    return (round(target_x, COORD_ROUND_DIGITS), round(target_y, COORD_ROUND_DIGITS))


def _power_entity_sort_key(ent: Mapping[str, Any]) -> tuple[int, float, float]:
    """Order shoreline power entities: pump first, boiler second, engine third, then others."""
    name = str(ent["name"])
    pos = ent["position"]
    x = float(pos["x"])
    y = float(pos["y"])
    if name == "offshore-pump":
        rank = 0
    elif name == "boiler":
        rank = 1
    elif name == "steam-engine":
        rank = 2
    elif name in ("inserter", "long-handed-inserter"):
        rank = 3
    else:
        rank = 4
    return (rank, x, y)


def _patch_entity_sort_key(ent: Mapping[str, Any]) -> tuple[int, float, float]:
    """Order patch array entities: mining drills first, then collection belts and splitters."""
    name = str(ent["name"])
    pos = ent["position"]
    x = float(pos["x"])
    y = float(pos["y"])
    rank = 0 if "drill" in name else 1
    return (rank, x, y)


def _trunk_entity_sort_key(ent: Mapping[str, Any]) -> tuple[int, float, float]:
    """Order trunk belt entities so underground input hoods precede output hoods."""
    name = str(ent["name"])
    ug_type = str(ent.get("type", ""))
    pos = ent["position"]
    x = float(pos["x"])
    y = float(pos["y"])
    if "underground" in name and ug_type == "output":
        rank = 2
    elif "underground" in name:
        rank = 1
    else:
        rank = 0
    return (rank, x, y)


def _base_core_entity_sort_key(ent: Mapping[str, Any]) -> tuple[int, float, float]:
    """Order starter base core entities: furnaces, assemblers, labs, belts, then inserters."""
    name = str(ent["name"])
    ug_type = str(ent.get("type", ""))
    pos = ent["position"]
    x = float(pos["x"])
    y = float(pos["y"])
    if "furnace" in name:
        rank = 0
    elif "assembling-machine" in name:
        rank = 1
    elif name == "lab":
        rank = 2
    elif "underground" in name and ug_type == "output":
        rank = 4
    elif "belt" in name or "splitter" in name:
        rank = 3
    else:
        rank = 5
    return (rank, x, y)


def compile_starter_base_tasks(
    surface: MapSurfaceSpec,
    layout: PatchToBaseResult,
    obstacles_to_clear: Sequence[TerrainObstacle],
    reach_limits: CharacterReachLimits,
    data: solve.Data,
    fuel_commodity: str,
    initial_pos: tuple[float, float] = (0.0, 0.0),
) -> list[BotBuildTask]:
    """Compile a dependency-ordered list of physical bot tasks for the starter base."""
    tasks: list[BotBuildTask] = []
    cur_pos = initial_pos
    placed_tiles: set[TileCoord] = set(surface.obstacles)

    # Phase 0: Pre-clear clearable obstacles (trees/rocks/unlocked cliffs) on planned footprints
    for obs in obstacles_to_clear:
        stand_x, stand_y = select_standing_position(
            target_x=obs.x,
            target_y=obs.y,
            reach_limit=reach_limits.resource_reach,
            water_tiles=surface.water_tiles,
            blocked_tiles=placed_tiles,
            reference_pos=cur_pos,
        )
        cur_pos = (stand_x, stand_y)
        tasks.append(
            BotBuildTask(
                task_index=len(tasks),
                phase=PHASE_CLEAR_OBSTACLES,
                task_kind=TASK_CLEAR_OBSTACLE,
                target_name=obs.name,
                target_x=obs.x,
                target_y=obs.y,
                stand_x=stand_x,
                stand_y=stand_y,
                entity_type=obs.entity_type,
                unit_number=obs.unit_number,
            )
        )

    # Index deduplicated final entities by rounded position
    entities_by_pos: dict[tuple[float, float], Mapping[str, Any]] = {
        _entity_pos_key(ent): ent for ent in layout.entities
    }
    scheduled_positions: set[tuple[float, float]] = set()

    def _schedule_entity(ent: Mapping[str, Any], phase: str) -> None:
        nonlocal cur_pos
        key = _entity_pos_key(ent)
        if key in scheduled_positions or key not in entities_by_pos:
            return
        final_ent = entities_by_pos[key]
        scheduled_positions.add(key)

        tx, ty = key
        name = str(final_ent["name"])
        direction = int(final_ent.get("direction", DIRECTION_NORTH))
        ug_type = str(final_ent["type"]) if "type" in final_ent else None
        recipe = str(final_ent["recipe"]) if "recipe" in final_ent else None

        footprint = _entity_footprint_tiles(final_ent, data)
        stand_x, stand_y = select_standing_position(
            target_x=tx,
            target_y=ty,
            reach_limit=reach_limits.build_reach,
            water_tiles=surface.water_tiles,
            blocked_tiles=placed_tiles | footprint,
            reference_pos=cur_pos,
        )
        cur_pos = (stand_x, stand_y)
        tasks.append(
            BotBuildTask(
                task_index=len(tasks),
                phase=phase,
                task_kind=TASK_PLACE_ENTITY,
                target_name=name,
                target_x=tx,
                target_y=ty,
                stand_x=stand_x,
                stand_y=stand_y,
                direction=direction,
                entity_type=ug_type,
                recipe=recipe,
            )
        )
        placed_tiles.update(footprint)

        if recipe is not None:
            tasks.append(
                BotBuildTask(
                    task_index=len(tasks),
                    phase=phase,
                    task_kind=TASK_CONFIGURE_RECIPE,
                    target_name=name,
                    target_x=tx,
                    target_y=ty,
                    stand_x=stand_x,
                    stand_y=stand_y,
                    recipe=recipe,
                )
            )
        if name == "boiler":
            tasks.append(
                BotBuildTask(
                    task_index=len(tasks),
                    phase=phase,
                    task_kind=TASK_SEED_FUEL,
                    target_name=fuel_commodity,
                    target_x=tx,
                    target_y=ty,
                    stand_x=stand_x,
                    stand_y=stand_y,
                    item_count=DEFAULT_SEED_FUEL_COUNT,
                )
            )

    def _schedule_nn(ents: Sequence[Mapping[str, Any]], phase: str) -> None:
        remaining = list(ents)
        while remaining:
            best_idx = min(
                range(len(remaining)),
                key=lambda idx: (
                    math.hypot(
                        float(remaining[idx]["position"]["x"]) - cur_pos[0],
                        float(remaining[idx]["position"]["y"]) - cur_pos[1],
                    ),
                    idx,
                ),
            )
            _schedule_entity(remaining.pop(best_idx), phase)

    # Phase 1: Shoreline steam power array
    power_ents = sorted(layout.power_array.entities, key=_power_entity_sort_key)
    for ent in power_ents:
        _schedule_entity(ent, PHASE_SHORELINE_POWER)

    # Phase 2: Resource patch miner arrays (fuel patch first, then nearest patch)
    remaining_patches = list(layout.patch_arrays.keys())
    while remaining_patches:
        best_p_idx = min(
            range(len(remaining_patches)),
            key=lambda idx: (
                0 if remaining_patches[idx] == fuel_commodity else 1,
                math.hypot(
                    float(layout.patch_arrays[remaining_patches[idx]].entities[0]["position"]["x"])
                    - cur_pos[0],
                    float(layout.patch_arrays[remaining_patches[idx]].entities[0]["position"]["y"])
                    - cur_pos[1],
                )
                if layout.patch_arrays[remaining_patches[idx]].entities
                else 0.0,
                remaining_patches[idx],
            ),
        )
        comm = remaining_patches.pop(best_p_idx)
        p_ents = sorted(layout.patch_arrays[comm].entities, key=_patch_entity_sort_key)
        for ent in p_ents:
            _schedule_entity(ent, PHASE_PATCH_MINERS)

    # Phase 3: Power distribution network (poles ordered by nearest-neighbor from cur_pos)
    pole_ents = [
        ent
        for ent in layout.entities
        if _entity_pos_key(ent) not in scheduled_positions and "pole" in str(ent["name"])
    ]
    _schedule_nn(pole_ents, PHASE_POWER_NETWORK)

    # Phase 4: Trunk belts (boiler fuel trunk first, then nearest trunk route)
    remaining_trunks = list(layout.trunk_routes.keys())
    while remaining_trunks:
        best_t_idx = min(
            range(len(remaining_trunks)),
            key=lambda idx: (
                0 if "boiler" in remaining_trunks[idx] else 1,
                min(
                    (
                        math.hypot(node.x - cur_pos[0], node.y - cur_pos[1])
                        for node in layout.trunk_routes[remaining_trunks[idx]].nodes
                    ),
                    default=0.0,
                ),
                remaining_trunks[idx],
            ),
        )
        net_name = remaining_trunks.pop(best_t_idx)
        route_res = layout.trunk_routes[net_name]
        route_ents: list[Mapping[str, Any]] = []
        for node in route_res.nodes:
            n_key = (
                round(node.x + TILE_CENTER_OFFSET, COORD_ROUND_DIGITS),
                round(node.y + TILE_CENTER_OFFSET, COORD_ROUND_DIGITS),
            )
            if n_key in entities_by_pos:
                route_ents.append(entities_by_pos[n_key])
        for rank_group in (0, 1, 2):
            grp = [e for e in route_ents if _trunk_entity_sort_key(e)[0] == rank_group]
            _schedule_nn(grp, PHASE_TRUNK_BELTS)

    # Phase 5: Starter base core (furnaces, assemblers + recipes, lab, belts, inserters)
    core_ents = sorted(layout.base_core.entities, key=_base_core_entity_sort_key)
    for rank_group in range(6):
        grp = [e for e in core_ents if _base_core_entity_sort_key(e)[0] == rank_group]
        _schedule_nn(grp, PHASE_STARTER_BASE_CORE)

    # Schedule any remaining unscheduled entities from layout.entities
    rem_ents = [ent for ent in layout.entities if _entity_pos_key(ent) not in scheduled_positions]
    _schedule_nn(rem_ents, PHASE_STARTER_BASE_CORE)

    return tasks


def plan_starter_base_from_cartographer(
    cartographer: MapCartographer,
    data: solve.Data,
    researched_techs: set[str] | Sequence[str] = ("automation", "logistics"),
    science_recipe: str = "automation-science-pack",
    mall_recipes: Sequence[str] = ("transport-belt",),
    unlocked_by: str = "logistics",
    reference_pos: tuple[float, float] = (0.0, 0.0),
    bounds: tuple[int, int, int, int] | None = None,
    base_origin: TileCoord | None = None,
    engine: FactorioNativeEngine | None = None,
    verify: bool = False,
) -> StarterBaseBuildPlan:
    """Synthesize a complete starter base layout and construction plan from cartographer data."""
    if cartographer.data is None:
        cartographer.data = data

    tech_set = set(researched_techs)
    surface = cartographer.to_map_surface_spec(researched_techs=tech_set, bounds=bounds)
    tier = resolve_tech_tier(data, unlocked_by=unlocked_by)
    reach_limits = extract_character_reach(data)

    layout = route_patch_to_base(
        surface=surface,
        data=data,
        science_recipe=science_recipe,
        mall_recipes=mall_recipes,
        unlocked_by=unlocked_by,
        base_origin=base_origin,
        engine=engine,
        verify=verify,
    )

    obstacles_to_clear = cartographer.obstacles_to_clear_for_entities(
        entities=layout.entities,
        researched_techs=tech_set,
        reference_pos=reference_pos,
    )

    bom = compute_bill_of_materials(layout.entities)
    tasks = compile_starter_base_tasks(
        surface=surface,
        layout=layout,
        obstacles_to_clear=obstacles_to_clear,
        reach_limits=reach_limits,
        data=data,
        fuel_commodity=tier.fuel_commodity,
        initial_pos=reference_pos,
    )

    return StarterBaseBuildPlan(
        surface=surface,
        layout=layout,
        obstacles_to_clear=obstacles_to_clear,
        bill_of_materials=bom,
        reach_limits=reach_limits,
        tasks=tasks,
        fuel_commodity=tier.fuel_commodity,
    )


def encode_assembler_batch_actions(
    asm_x: float,
    asm_y: float,
    recipe_name: str,
    ingredients: Sequence[str],
    id_mappings: Mapping[str, Mapping[str, int]],
    extract_first: bool = True,
    quality_id: int = 1,
) -> list[tuple[int, bytes]]:
    """Encode real UDP InputActions to extract output, set recipe, and load ingredients."""
    item_map = id_mappings.get("item", {})
    recipe_map = id_mappings.get("recipe", {})
    if recipe_name not in recipe_map:
        raise KeyError(f"Recipe '{recipe_name}' not found in id_mappings['recipe']")
    recipe_id = int(recipe_map[recipe_name])

    actions: list[tuple[int, bytes]] = [build_clear_cursor_action()]
    if extract_first:
        actions.append(build_selected_entity_action(asm_x, asm_y))
        actions.append(build_fast_entity_transfer_action(into_entity=False))

    actions.extend(
        [
            build_selected_entity_action(asm_x, asm_y),
            build_open_gui_action(),
            build_setup_assembling_machine_action(recipe_id, quality_id=quality_id),
            build_close_gui_action(),
        ]
    )
    for ing_name in ingredients:
        if ing_name not in item_map:
            raise KeyError(f"Ingredient '{ing_name}' not found in id_mappings['item']")
        ing_id = int(item_map[ing_name])
        actions.extend(
            [
                build_selected_entity_action(asm_x, asm_y),
                build_pipette_item_action(ing_id),
                build_fast_entity_transfer_action(into_entity=True),
                build_clear_cursor_action(),
            ]
        )
    return actions


def encode_task_input_actions(
    task: BotBuildTask,
    id_mappings: Mapping[str, Mapping[str, int]],
) -> list[tuple[int, bytes]]:
    """Encode a BotBuildTask into Factorio UDP InputAction (action_type, payload) pairs."""
    item_map = id_mappings.get("item", {})
    recipe_map = id_mappings.get("recipe", {})

    if task.task_kind == TASK_CLEAR_OBSTACLE:
        return [
            build_selected_entity_action(task.target_x, task.target_y),
            build_begin_mining_action(),
        ]

    if task.task_kind == TASK_PLACE_ENTITY:
        if task.target_name not in item_map:
            raise KeyError(f"Item prototype '{task.target_name}' not found in id_mappings['item']")
        item_id = int(item_map[task.target_name])
        actions: list[tuple[int, bytes]] = [
            build_pipette_item_action(item_id),
            build_place_entity_action(
                task.target_x,
                task.target_y,
                direction=task.direction,
            ),
            build_clear_cursor_action(),
        ]
        return actions

    if task.task_kind == TASK_CONFIGURE_RECIPE:
        if task.recipe is None or task.recipe not in recipe_map:
            raise KeyError(f"Recipe '{task.recipe}' not found in id_mappings['recipe']")
        recipe_id = int(recipe_map[task.recipe])
        quality_id = int(id_mappings.get("quality", {}).get("normal", 1))
        return [
            build_selected_entity_action(task.target_x, task.target_y),
            build_open_gui_action(),
            build_setup_assembling_machine_action(recipe_id, quality_id=quality_id),
            build_close_gui_action(),
        ]

    if task.task_kind == TASK_SEED_FUEL:
        if task.target_name not in item_map:
            raise KeyError(f"Fuel item '{task.target_name}' not found in id_mappings['item']")
        fuel_id = int(item_map[task.target_name])
        transfer_action = (
            build_fast_entity_split_action(into_entity=True)
            if task.item_count == 0
            else build_fast_entity_transfer_action(into_entity=True)
        )
        return [
            build_clear_cursor_action(),
            build_selected_entity_action(task.target_x, task.target_y),
            build_pipette_item_action(fuel_id),
            transfer_action,
            build_clear_cursor_action(),
        ]

    if task.task_kind == TASK_COLLECT_MALL_OUTPUT:
        return [
            build_clear_cursor_action(),
            build_selected_entity_action(task.target_x, task.target_y),
            build_fast_entity_transfer_action(into_entity=False),
        ]

    raise ValueError(f"Unsupported task_kind: {task.task_kind!r}")


def compute_8way_walk_vector(
    player_x: float,
    player_y: float,
    target_x: float,
    target_y: float,
    stop_tolerance: float = DEFAULT_STOP_TOLERANCE,
) -> tuple[float, float] | None:
    """Compute a unit cardinal walking vector toward (target_x, target_y), or None if within tolerance."""
    dx = target_x - player_x
    dy = target_y - player_y
    dist = math.hypot(dx, dy)
    if dist <= stop_tolerance:
        return None
    axis_tol = stop_tolerance * CARDINAL_AXIS_RATIO
    if abs(dx) > axis_tol:
        return (1.0 if dx > 0.0 else -1.0, 0.0)
    if abs(dy) > axis_tol:
        return (0.0, 1.0 if dy > 0.0 else -1.0)
    if abs(dx) >= abs(dy):
        return (1.0 if dx > 0.0 else -1.0, 0.0)
    return (0.0, 1.0 if dy > 0.0 else -1.0)


class BotAgent:
    """Stateful autonomous agent that scans cartography and executes starter base construction."""

    def __init__(
        self,
        data: solve.Data,
        cartographer: MapCartographer | None = None,
        researched_techs: set[str] | Sequence[str] = ("automation", "logistics"),
    ) -> None:
        self.data = data
        self.cartographer = cartographer if cartographer is not None else MapCartographer(data=data)
        if self.cartographer.data is None:
            self.cartographer.data = data
        self.researched_techs: set[str] = set(researched_techs)
        self.plan: StarterBaseBuildPlan | None = None
        self.completed_task_indices: set[int] = set()
        self._active_mining_task: int | None = None
        self._last_walk_vec: tuple[float, float] | None = None
        self._last_walk_tick: int = -1000
        self._last_action_tick: int = -1000
        self._last_walk_pos: tuple[float, float] | None = None
        self._walk_stuck_retries: int = 0

    def ingest_cartography(self, source: str | Path | Mapping[str, Any]) -> bool:
        """Ingest cartography telemetry from a JSON file path or parsed dictionary."""
        if isinstance(source, Mapping):
            self.cartographer.ingest_telemetry_dict(source)
            return True
        return self.cartographer.ingest_telemetry_file(source)

    def synthesize_plan(
        self,
        science_recipe: str = "automation-science-pack",
        mall_recipes: Sequence[str] = ("transport-belt",),
        unlocked_by: str = "logistics",
        reference_pos: tuple[float, float] = (0.0, 0.0),
        bounds: tuple[int, int, int, int] | None = None,
        base_origin: TileCoord | None = None,
        engine: FactorioNativeEngine | None = None,
        verify: bool = False,
    ) -> StarterBaseBuildPlan:
        """Synthesize the starter base layout and construction schedule from cartographer state."""
        self.plan = plan_starter_base_from_cartographer(
            cartographer=self.cartographer,
            data=self.data,
            researched_techs=self.researched_techs,
            science_recipe=science_recipe,
            mall_recipes=mall_recipes,
            unlocked_by=unlocked_by,
            reference_pos=reference_pos,
            bounds=bounds,
            base_origin=base_origin,
            engine=engine,
            verify=verify,
        )
        self.completed_task_indices.clear()
        self._active_mining_task = None
        self._last_walk_vec = None
        self._last_walk_tick = -1000
        self._last_action_tick = -1000
        self._last_walk_pos = None
        self._walk_stuck_retries = 0
        return self.plan

    def current_task(self) -> BotBuildTask | None:
        """Return the next uncompleted BotBuildTask in the construction schedule."""
        if self.plan is None:
            return None
        for task in self.plan.tasks:
            if task.task_index not in self.completed_task_indices:
                return task
        return None

    def mark_task_complete(self, task_index: int) -> None:
        """Mark task_index as completed and update cartographer obstacle state if applicable."""
        self.completed_task_indices.add(task_index)
        if self._active_mining_task == task_index:
            self._active_mining_task = None
        if self.plan is not None and 0 <= task_index < len(self.plan.tasks):
            task = self.plan.tasks[task_index]
            if task.task_kind == TASK_CLEAR_OBSTACLE:
                self.cartographer.remove_obstacle_at(task.target_x, task.target_y)

    def find_mall_producer_task(self, item_name: str) -> BotBuildTask | None:
        """Find a placed assembler task whose configured recipe produces item_name."""
        if self.plan is None:
            return None
        for task in self.plan.tasks:
            if (
                task.task_kind == TASK_PLACE_ENTITY
                and task.recipe == item_name
                and task.task_index in self.completed_task_indices
            ):
                return task
        return None

    def step_lockstep_proxy(
        self,
        proxy: FactorioLockstepProxy,
        world_state: PlayerWorldState,
        id_mappings: Mapping[str, Mapping[str, int]],
        player_index: int = 1,
        stop_tolerance: float = DEFAULT_STOP_TOLERANCE,
    ) -> str:
        """Advance construction by one control step over a FactorioLockstepProxy."""
        task = self.current_task()
        if task is None:
            return "completed"

        if proxy.pending_action_count() > 0 or world_state.tick == self._last_action_tick:
            return "waiting_for_queue"

        if task.task_kind in (TASK_PLACE_ENTITY, TASK_SEED_FUEL):
            required_item = task.target_name
            available_qty = int(world_state.inventory.get(required_item, 0))
            if available_qty < 1:
                mall_task = self.find_mall_producer_task(required_item)
                if mall_task is not None:
                    mall_walk = compute_8way_walk_vector(
                        world_state.player_x,
                        world_state.player_y,
                        mall_task.stand_x,
                        mall_task.stand_y,
                        stop_tolerance=stop_tolerance,
                    )
                    if mall_walk is not None:
                        if mall_walk != self._last_walk_vec or (
                            not world_state.walking
                            and (world_state.tick - self._last_walk_tick) >= WALK_REISSUE_TICKS
                        ):
                            act_type, payload = build_start_walking_action(
                                mall_walk[0], mall_walk[1]
                            )
                            proxy.queue_action(act_type, player_index, payload)
                            self._last_walk_vec = mall_walk
                            self._last_walk_tick = world_state.tick
                        return f"walking_to_mall_for_{required_item}"
                    if world_state.walking or self._last_walk_vec is not None:
                        act_type, payload = build_stop_walking_action()
                        proxy.queue_action(act_type, player_index, payload)
                        self._last_walk_vec = None
                    collect_task = BotBuildTask(
                        task_index=task.task_index,
                        phase=task.phase,
                        task_kind=TASK_COLLECT_MALL_OUTPUT,
                        target_name=mall_task.target_name,
                        target_x=mall_task.target_x,
                        target_y=mall_task.target_y,
                        stand_x=mall_task.stand_x,
                        stand_y=mall_task.stand_y,
                    )
                    for act_type, payload in encode_task_input_actions(collect_task, id_mappings):
                        proxy.queue_action(act_type, player_index, payload)
                    self._last_action_tick = world_state.tick
                    return f"collecting_mall_{required_item}"
                if world_state.walking or self._last_walk_vec is not None:
                    act_type, payload = build_stop_walking_action()
                    proxy.queue_action(act_type, player_index, payload)
                    self._last_walk_vec = None
                return f"waiting_for_inventory_{required_item}"

        walk_vec = compute_8way_walk_vector(
            world_state.player_x,
            world_state.player_y,
            task.stand_x,
            task.stand_y,
            stop_tolerance=stop_tolerance,
        )
        if walk_vec is not None:
            reissue_due = (world_state.tick - self._last_walk_tick) >= WALK_REISSUE_TICKS
            stuck_blocked = (
                reissue_due
                and self._last_walk_pos is not None
                and math.hypot(
                    world_state.player_x - self._last_walk_pos[0],
                    world_state.player_y - self._last_walk_pos[1],
                )
                < 0.15
            )
            if (
                (walk_vec != self._last_walk_vec and self._walk_stuck_retries == 0)
                or (not world_state.walking and reissue_due)
                or stuck_blocked
            ):
                if stuck_blocked:
                    self._walk_stuck_retries += 1
                    dx = task.stand_x - world_state.player_x
                    dy = task.stand_y - world_state.player_y
                    if self._walk_stuck_retries % 2 == 1:
                        if abs(walk_vec[0]) > 0.0:
                            walk_vec = (0.0, 1.0 if dy >= 0.0 else -1.0)
                        else:
                            walk_vec = (1.0 if dx >= 0.0 else -1.0, 0.0)
                    else:
                        sx = (1.0 if dx >= 0.0 else -1.0) * 0.707
                        sy = (1.0 if dy >= 0.0 else -1.0) * 0.707
                        walk_vec = (sx, sy)
                else:
                    self._walk_stuck_retries = 0
                act_type, payload = build_start_walking_action(walk_vec[0], walk_vec[1])
                proxy.queue_action(act_type, player_index, payload)
                self._last_walk_vec = walk_vec
                self._last_walk_tick = world_state.tick
                self._last_walk_pos = (world_state.player_x, world_state.player_y)
            elif reissue_due:
                self._walk_stuck_retries = 0
                self._last_walk_tick = world_state.tick
                self._last_walk_pos = (world_state.player_x, world_state.player_y)
                if walk_vec != self._last_walk_vec:
                    act_type, payload = build_start_walking_action(walk_vec[0], walk_vec[1])
                    proxy.queue_action(act_type, player_index, payload)
                    self._last_walk_vec = walk_vec
            return f"walking_to_task_{task.task_index}"

        self._walk_stuck_retries = 0
        self._last_walk_pos = None
        if world_state.walking or self._last_walk_vec is not None:
            act_type, payload = build_stop_walking_action()
            proxy.queue_action(act_type, player_index, payload)
            self._last_walk_vec = None

        if task.task_kind == TASK_CLEAR_OBSTACLE:
            if self._active_mining_task != task.task_index:
                for act_type, payload in encode_task_input_actions(task, id_mappings):
                    proxy.queue_action(act_type, player_index, payload)
                self._active_mining_task = task.task_index
                self._last_action_tick = world_state.tick
                return f"mining_obstacle_{task.task_index}"
            if not world_state.mining:
                stop_type, stop_payload = build_stop_mining_action()
                proxy.queue_action(stop_type, player_index, stop_payload)
                self.mark_task_complete(task.task_index)
                self._last_action_tick = world_state.tick
                return f"cleared_obstacle_{task.task_index}"
            return f"mining_obstacle_{task.task_index}"

        reach_limit = (
            max(self.plan.reach_limits.build_reach, stop_tolerance + 2.0)
            if self.plan is not None
            else 10.0
        )
        inv_remaining: dict[str, int] = dict(world_state.inventory)
        last_executed_idx = task.task_index
        while True:
            cur_t = self.current_task()
            if cur_t is None or cur_t.task_kind == TASK_CLEAR_OBSTACLE:
                break
            if (
                math.hypot(
                    cur_t.stand_x - world_state.player_x,
                    cur_t.stand_y - world_state.player_y,
                )
                > stop_tolerance
                and math.hypot(
                    cur_t.target_x - world_state.player_x,
                    cur_t.target_y - world_state.player_y,
                )
                > reach_limit
            ):
                break
            if cur_t.task_kind in (TASK_PLACE_ENTITY, TASK_SEED_FUEL):
                req_item = cur_t.target_name
                need_qty = max(1, cur_t.item_count) if cur_t.task_kind == TASK_SEED_FUEL else 1
                have_qty = int(inv_remaining.get(req_item, 0))
                if have_qty < 1:
                    break
                inv_remaining[req_item] = max(0, have_qty - need_qty)
            for act_type, payload in encode_task_input_actions(cur_t, id_mappings):
                proxy.queue_action(act_type, player_index, payload)
            self.mark_task_complete(cur_t.task_index)
            last_executed_idx = cur_t.task_index

        self._last_action_tick = world_state.tick
        return f"executed_task_{last_executed_idx}"

    def build_lua_plan_executor_snippet(self, check_inventory: bool = False) -> str:
        """Generate a Lua snippet that executes every task in self.plan on surface 1."""
        if self.plan is None:
            raise RuntimeError("Cannot generate Lua executor snippet before synthesize_plan()")

        lines: list[str] = [
            "  local surface = game.surfaces[1]",
            "  local force = game.forces['player']",
            "  local char_ent = surface.find_entities_filtered({name = 'character'})[1]",
            "  local cleared_count = 0",
            "  local placed_count = 0",
            "  local configured_count = 0",
            "  local seeded_count = 0",
            "  local consumed_items = {}",
            "  local ent = nil",
        ]

        for task in self.plan.tasks:
            lines.append(
                f"  if char_ent and char_ent.valid then char_ent.teleport({{{task.stand_x}, {task.stand_y}}}) end"
            )
            if task.task_kind == TASK_CLEAR_OBSTACLE:
                lines.append(
                    f"  for _, obs in pairs(surface.find_entities_filtered({{position = {{{task.target_x}, {task.target_y}}}, radius = 1.5}})) do"
                )
                lines.append(
                    "    if obs.valid and obs.name ~= 'character' and obs.type ~= 'resource' then"
                )
                lines.append("      obs.destroy()")
                lines.append("      cleared_count = cleared_count + 1")
                lines.append("    end")
                lines.append("  end")
            elif task.task_kind == TASK_PLACE_ENTITY:
                extra = ""
                if task.entity_type in ("input", "output"):
                    extra += f", type = '{task.entity_type}'"
                if task.recipe is not None:
                    extra += f", recipe = '{task.recipe}'"
                if check_inventory:
                    lines.append(
                        f"  if not char_ent or char_ent.get_item_count('{task.target_name}') >= 1 then"
                    )
                    lines.append(
                        f"    ent = surface.create_entity({{name = '{task.target_name}', position = {{{task.target_x}, {task.target_y}}}, direction = {task.direction}, force = force{extra}}})"
                    )
                    lines.append("    if ent then")
                    lines.append("      placed_count = placed_count + 1")
                    lines.append(
                        f"      if char_ent then char_ent.remove_item({{name = '{task.target_name}', count = 1}}) end"
                    )
                    lines.append(
                        f"      consumed_items['{task.target_name}'] = (consumed_items['{task.target_name}'] or 0) + 1"
                    )
                    lines.append("    end")
                    lines.append("  end")
                else:
                    lines.append(
                        f"  ent = surface.create_entity({{"
                        f"name = '{task.target_name}', "
                        f"position = {{{task.target_x}, {task.target_y}}}, "
                        f"direction = {task.direction}, "
                        f"force = force{extra}}})"
                    )
                    lines.append("  if ent then placed_count = placed_count + 1 end")
            elif task.task_kind == TASK_CONFIGURE_RECIPE and task.recipe is not None:
                lines.append(
                    f"  ent = surface.find_entity('{task.target_name}', {{{task.target_x}, {task.target_y}}})"
                )
                lines.append(
                    f"  if ent then ent.set_recipe('{task.recipe}') configured_count = configured_count + 1 end"
                )
            elif task.task_kind == TASK_SEED_FUEL:
                lines.append(
                    f"  ent = surface.find_entity('boiler', {{{task.target_x}, {task.target_y}}})"
                )
                if check_inventory:
                    lines.append(
                        f"  if ent and (not char_ent or char_ent.get_item_count('{task.target_name}') >= {task.item_count}) then"
                    )
                    lines.append(
                        f"    local ins_cnt = ent.insert({{name = '{task.target_name}', count = {task.item_count}}})"
                    )
                    lines.append("    if ins_cnt > 0 then")
                    lines.append("      seeded_count = seeded_count + 1")
                    lines.append(
                        f"      if char_ent then char_ent.remove_item({{name = '{task.target_name}', count = ins_cnt}}) end"
                    )
                    lines.append(
                        f"      consumed_items['{task.target_name}'] = (consumed_items['{task.target_name}'] or 0) + ins_cnt"
                    )
                    lines.append("    end")
                    lines.append("  end")
                else:
                    lines.append(
                        f"  if ent then ent.insert({{name = '{task.target_name}', count = {task.item_count}}}) seeded_count = seeded_count + 1 end"
                    )

        lines.append("  storage.bot_execution = {")
        lines.append("    cleared_count = cleared_count,")
        lines.append("    placed_count = placed_count,")
        lines.append("    configured_count = configured_count,")
        lines.append("    seeded_count = seeded_count,")
        lines.append("    consumed_items = consumed_items,")
        lines.append("  }")
        return "\n".join(lines) + "\n"

    def compute_bom_fabrication(
        self,
        starting_inventory: Mapping[str, int] | None = None,
    ) -> BomFabricationPlan:
        """Compute recursive BOM fabrication steps from self.plan and starting_inventory."""
        if self.plan is None:
            raise RuntimeError("Cannot compute BOM fabrication before synthesize_plan()")
        return compute_bom_fabrication_plan(
            target_bom=self.plan.bill_of_materials,
            data=self.data,
            starting_inventory=starting_inventory,
            fuel_commodity=self.plan.fuel_commodity,
        )

    def build_lua_progressive_plan_executor_snippet(
        self,
        step_stride: float = 0.15,
        check_inventory: bool = False,
    ) -> str:
        """Generate a Lua snippet that walks the bot and executes tasks progressively across ticks."""
        if self.plan is None:
            raise RuntimeError("Cannot generate progressive Lua snippet before synthesize_plan()")

        bx0, by0, bx1, by1 = self.plan.layout.base_core.bounds
        park_x = round((bx0 + bx1) / 2.0 - 1.5, 1)
        park_y = round((by0 + by1) / 2.0 + 0.5, 1)
        check_inv_str = "true" if check_inventory else "false"

        task_entries: list[str] = []
        for task in self.plan.tasks:
            ug_str = f"'{task.entity_type}'" if task.entity_type in ("input", "output") else "nil"
            rec_str = f"'{task.recipe}'" if task.recipe is not None else "nil"
            if task.task_kind == TASK_CLEAR_OBSTACLE:
                wait_ticks = 20
            elif task.task_kind in (TASK_CONFIGURE_RECIPE, TASK_SEED_FUEL):
                wait_ticks = 5
            elif task.target_name == "transport-belt":
                wait_ticks = 3
            elif (
                "pole" in task.target_name
                or "inserter" in task.target_name
                or "underground" in task.target_name
                or "splitter" in task.target_name
            ):
                wait_ticks = 6
            else:
                wait_ticks = 10
            task_entries.append(
                f"    {{kind = '{task.task_kind}', name = '{task.target_name}', "
                f"tx = {task.target_x}, ty = {task.target_y}, "
                f"sx = {task.stand_x}, sy = {task.stand_y}, "
                f"dir = {task.direction}, ug = {ug_str}, rec = {rec_str}, "
                f"cnt = {task.item_count}, wait = {wait_ticks}}}"
            )

        tasks_lua = ",\n".join(task_entries)
        stride_val = float(step_stride)
        return (
            "local BOT_TASKS = {\n"
            f"{tasks_lua}\n"
            "}\n\n"
            f"local CHECK_INVENTORY = {check_inv_str}\n\n"
            "local function vec_to_dir(dx, dy)\n"
            "  if math.abs(dx) < 1e-4 and math.abs(dy) < 1e-4 then return defines.direction.south end\n"
            "  local angle = math.atan2(dy, dx)\n"
            "  local octant = math.floor(((angle + math.pi / 2) / (2 * math.pi)) * 8 + 0.5) % 8\n"
            "  return octant * 2\n"
            "end\n\n"
            "local function step_bot_plan(surface, force)\n"
            "  if not storage.bot_execution then\n"
            "    storage.bot_execution = {\n"
            "      cleared_count = 0,\n"
            "      placed_count = 0,\n"
            "      configured_count = 0,\n"
            "      seeded_count = 0,\n"
            "      consumed_items = {},\n"
            "      task_idx = 1,\n"
            "      wait_left = nil,\n"
            "      completed = false,\n"
            "    }\n"
            "  end\n"
            "  local exec = storage.bot_execution\n"
            "  if exec.completed then return end\n"
            "  local p1 = game.get_player(1)\n"
            "  local char_ent = (p1 and p1.valid and p1.character and p1.character.valid and p1.character) or surface.find_entities_filtered({name = 'character'})[1]\n"
            "  local idx = exec.task_idx\n"
            "  if idx > #BOT_TASKS then\n"
            "    if char_ent and char_ent.valid then\n"
            f"      local dx = {park_x} - char_ent.position.x\n"
            f"      local dy = {park_y} - char_ent.position.y\n"
            "      local dist = math.sqrt(dx * dx + dy * dy)\n"
            f"      if dist > {stride_val} then\n"
            f"        char_ent.direction = vec_to_dir(dx, dy)\n"
            f"        char_ent.teleport({{char_ent.position.x + (dx / dist) * {stride_val}, char_ent.position.y + (dy / dist) * {stride_val}}})\n"
            "        return\n"
            "      else\n"
            f"        char_ent.teleport({{{park_x}, {park_y}}})\n"
            "        char_ent.direction = defines.direction.east\n"
            "      end\n"
            "    end\n"
            "    exec.completed = true\n"
            "    return\n"
            "  end\n"
            "  local t = BOT_TASKS[idx]\n"
            "  if char_ent and char_ent.valid then\n"
            "    local dx = t.sx - char_ent.position.x\n"
            "    local dy = t.sy - char_ent.position.y\n"
            "    local dist = math.sqrt(dx * dx + dy * dy)\n"
            f"    if dist > {stride_val} then\n"
            f"      local nx = char_ent.position.x + (dx / dist) * {stride_val}\n"
            f"      local ny = char_ent.position.y + (dy / dist) * {stride_val}\n"
            "      char_ent.direction = vec_to_dir(dx, dy)\n"
            "      char_ent.teleport({nx, ny})\n"
            "      return\n"
            "    else\n"
            "      char_ent.teleport({t.sx, t.sy})\n"
            "      char_ent.direction = vec_to_dir(t.tx - t.sx, t.ty - t.sy)\n"
            "    end\n"
            "  end\n"
            "  if exec.wait_left == nil then\n"
            "    exec.wait_left = t.wait or 4\n"
            "  end\n"
            "  if exec.wait_left > 0 then\n"
            "    exec.wait_left = exec.wait_left - 1\n"
            "    return\n"
            "  end\n"
            "  exec.wait_left = nil\n"
            "  if t.kind == 'clear_obstacle' then\n"
            "    for _, obs in pairs(surface.find_entities_filtered({position = {t.tx, t.ty}, radius = 1.5})) do\n"
            "      if obs.valid and obs.name ~= 'character' and obs.type ~= 'resource' then\n"
            "        obs.destroy()\n"
            "        exec.cleared_count = exec.cleared_count + 1\n"
            "      end\n"
            "    end\n"
            "  elseif t.kind == 'place_entity' then\n"
            "    local existing = surface.find_entity(t.name, {t.tx, t.ty})\n"
            "    if not existing then\n"
            "      if CHECK_INVENTORY and char_ent and char_ent.valid and char_ent.get_item_count(t.name) < 1 then\n"
            "        return\n"
            "      end\n"
            "      local params = {name = t.name, position = {t.tx, t.ty}, direction = t.dir, force = force, raise_built = true}\n"
            "      if t.ug then params.type = t.ug end\n"
            "      if t.rec then params.recipe = t.rec end\n"
            "      if t.name == 'offshore-pump' and not surface.can_place_entity({name = t.name, position = {t.tx, t.ty}, direction = t.dir, force = force, build_check_type = defines.build_check_type.manual}) then\n"
            "        return\n"
            "      end\n"
            "      local ent = surface.create_entity(params)\n"
            "      if ent then\n"
            "        exec.placed_count = exec.placed_count + 1\n"
            "        if CHECK_INVENTORY and char_ent and char_ent.valid then\n"
            "          char_ent.remove_item({name = t.name, count = 1})\n"
            "          exec.consumed_items[t.name] = (exec.consumed_items[t.name] or 0) + 1\n"
            "        end\n"
            "      end\n"
            "    end\n"
            "  elseif t.kind == 'configure_recipe' and t.rec then\n"
            "    local ent = surface.find_entity(t.name, {t.tx, t.ty})\n"
            "    if ent then ent.set_recipe(t.rec) exec.configured_count = configured_count and (configured_count + 1) or (exec.configured_count + 1) end\n"
            "  elseif t.kind == 'seed_fuel' then\n"
            "    local ent = surface.find_entity('boiler', {t.tx, t.ty})\n"
            "    if ent then\n"
            "      if CHECK_INVENTORY and char_ent and char_ent.valid and char_ent.get_item_count(t.name) < t.cnt then\n"
            "        return\n"
            "      end\n"
            "      local ins_cnt = ent.insert({name = t.name, count = t.cnt})\n"
            "      if ins_cnt > 0 then\n"
            "        exec.seeded_count = exec.seeded_count + 1\n"
            "        if CHECK_INVENTORY and char_ent and char_ent.valid then\n"
            "          char_ent.remove_item({name = t.name, count = ins_cnt})\n"
            "          exec.consumed_items[t.name] = (exec.consumed_items[t.name] or 0) + ins_cnt\n"
            "        end\n"
            "      end\n"
            "    end\n"
            "  end\n"
            "  exec.task_idx = idx + 1\n"
            "end\n"
        )
