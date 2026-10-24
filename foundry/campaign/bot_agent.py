"""Autonomous bot agent that synthesizes and builds the starter base from cartography.

Reads surface telemetry through MapCartographer, synthesizes the patch-to-base
starter layout and bootstrap mall, computes the obstacle pre-clearing schedule,
orders entity construction by power and material dependencies, and emits
Factorio InputAction sequences within character reach limits.
All docstrings and comments follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import heapq
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
    ShorelineSite,
    build_begin_mining_action,
    build_clear_cursor_action,
    build_close_gui_action,
    build_craft_action,
    build_fast_entity_split_action,
    build_fast_entity_transfer_action,
    build_open_gui_action,
    build_pipette_item_action,
    build_place_entity_action,
    build_selected_entity_action,
    build_setup_assembling_machine_action,
    build_start_research_action,
    build_start_walking_action,
    build_stop_mining_action,
    build_stop_walking_action,
)
from foundry.native_engine.engine import FactorioNativeEngine
from foundry.patch_router import (
    CARDINAL_DIRS,
    DIR_STEPS,
    MapSurfaceSpec,
    PatchToBaseResult,
    TileCoord,
    _bootstrap_shoreline_footprint,
    _rotate_offset,
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
    PHASE_POWER_NETWORK,
    PHASE_PATCH_MINERS,
    PHASE_STARTER_BASE_CORE,
    PHASE_TRUNK_BELTS,
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
    for ent in layout.power_array.entities:
        if ent["name"] == "offshore-pump":
            pos = ent["position"]
            px_pos, py_pos = float(pos["x"]), float(pos["y"])
            p_dir = int(ent.get("direction", DIRECTION_NORTH))
            placed_tiles.update(_bootstrap_shoreline_footprint(px_pos, py_pos, p_dir))
            s_dx, s_dy = _rotate_offset(-0.5, 4.5, p_dir)
            placed_tiles.discard((math.floor(px_pos + s_dx), math.floor(py_pos + s_dy)))

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

    # Phase 2: Power distribution network (poles ordered by nearest-neighbor from cur_pos)
    pole_ents = [
        ent
        for ent in layout.entities
        if _entity_pos_key(ent) not in scheduled_positions and "pole" in str(ent["name"])
    ]
    _schedule_nn(pole_ents, PHASE_POWER_NETWORK)

    # Phase 3: Resource patch miner arrays (fuel patch first, then iron-ore, then copper-ore)
    remaining_patches = list(layout.patch_arrays.keys())
    while remaining_patches:
        best_p_idx = min(
            range(len(remaining_patches)),
            key=lambda idx: (
                0
                if remaining_patches[idx] == fuel_commodity
                else (1 if remaining_patches[idx] == "iron-ore" else 2),
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

    # Phase 4: Starter base core (furnaces, assemblers + recipes, lab, belts, inserters)
    core_ents = sorted(layout.base_core.entities, key=_base_core_entity_sort_key)
    for rank_group in range(6):
        grp = [e for e in core_ents if _base_core_entity_sort_key(e)[0] == rank_group]
        _schedule_nn(grp, PHASE_STARTER_BASE_CORE)

    # Phase 5: Trunk belts in dependency order (boiler fuel -> base fuel -> iron-ore -> copper-ore)
    def _trunk_priority(net_name: str) -> int:
        if "boiler" in net_name:
            return 0
        if fuel_commodity in net_name:
            return 1
        if "iron-ore" in net_name:
            return 2
        return 3

    remaining_trunks = list(layout.trunk_routes.keys())
    while remaining_trunks:
        best_t_idx = min(
            range(len(remaining_trunks)),
            key=lambda idx: (
                _trunk_priority(remaining_trunks[idx]),
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

    # Schedule any remaining unscheduled entities from layout.entities
    rem_ents = [ent for ent in layout.entities if _entity_pos_key(ent) not in scheduled_positions]
    _schedule_nn(rem_ents, PHASE_TRUNK_BELTS)

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

    smelter_dummy_entities: list[dict[str, Any]] = []
    iron_pa = layout.patch_arrays.get("iron-ore")
    if iron_pa is not None:
        for ent in iron_pa.entities:
            if ent["name"] == tier.drill_name:
                pos = ent["position"]
                dx_pos, dy_pos = float(pos["x"]), float(pos["y"])
                d_dir = int(ent.get("direction", DIRECTION_NORTH))
                step_x, step_y = DIR_STEPS[d_dir]
                sm_x = dx_pos + 2.5 * step_x + (0.5 if step_x == 0 else 0.0)
                sm_y = dy_pos + 2.5 * step_y + (0.5 if step_y == 0 else 0.0)
                smelter_dummy_entities.append(
                    {
                        "name": tier.furnace_name,
                        "position": {"x": sm_x, "y": sm_y},
                        "direction": DIRECTION_NORTH,
                    }
                )

    for ent in layout.power_array.entities:
        if ent["name"] == tier.pump_name:
            pos = ent["position"]
            px_pos, py_pos = float(pos["x"]), float(pos["y"])
            p_dir = int(ent.get("direction", DIRECTION_NORTH))
            po_dx, po_dy = _rotate_offset(2.0, 4.0, p_dir)
            a_dx, a_dy = _rotate_offset(2.0, 6.0, p_dir)
            l_dx, l_dy = _rotate_offset(5.0, 5.5, p_dir)
            smelter_dummy_entities.extend(
                [
                    {
                        "name": tier.pole_name,
                        "position": {"x": px_pos + po_dx, "y": py_pos + po_dy},
                        "direction": DIRECTION_NORTH,
                    },
                    {
                        "name": tier.assembler_name,
                        "position": {"x": px_pos + a_dx, "y": py_pos + a_dy},
                        "direction": DIRECTION_NORTH,
                    },
                    {
                        "name": "lab",
                        "position": {"x": px_pos + l_dx, "y": py_pos + l_dy},
                        "direction": DIRECTION_NORTH,
                    },
                ]
            )
            for fy_off in (2.0, 4.0, 6.0, 8.0):
                f_dx, f_dy = _rotate_offset(-4.5, fy_off, p_dir)
                smelter_dummy_entities.append(
                    {
                        "name": tier.furnace_name,
                        "position": {"x": px_pos + f_dx, "y": py_pos + f_dy},
                        "direction": DIRECTION_NORTH,
                    }
                )

    obstacles_to_clear = cartographer.obstacles_to_clear_for_entities(
        entities=[*layout.entities, *smelter_dummy_entities],
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
        build_dir = (task.direction + 8) % 16 if task.entity_type == "output" else task.direction
        actions: list[tuple[int, bytes]] = [
            build_pipette_item_action(item_id),
            build_place_entity_action(
                task.target_x,
                task.target_y,
                direction=build_dir,
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


WALKABLE_ENTITY_NAMES: frozenset[str] = frozenset(
    {
        "transport-belt",
        "fast-transport-belt",
        "express-transport-belt",
    }
)

DIAG_UNIT: float = math.sqrt(0.5)
OCTANT_TAN_THRESHOLD: float = math.sqrt(2.0) - 1.0
WAYPOINT_PASS_TOLERANCE: float = 0.55
APPROACH_SLOW_RADIUS: float = 1.25
MIN_WALK_SCALE: float = 0.25
STUCK_TICKS_THRESHOLD: int = 18

CARDINAL_AND_DIAGONAL_STEPS: tuple[tuple[int, int, float], ...] = (
    (1, 0, 1.0),
    (-1, 0, 1.0),
    (0, 1, 1.0),
    (0, -1, 1.0),
    (1, 1, math.sqrt(2.0)),
    (1, -1, math.sqrt(2.0)),
    (-1, 1, math.sqrt(2.0)),
    (-1, -1, math.sqrt(2.0)),
)


def build_character_blocked_tiles(
    cartographer: MapCartographer | None = None,
    placed_entities: Sequence[PlacedSurfaceEntity | Mapping[str, Any]] = (),
    data: solve.Data | None = None,
    extra_blocked: set[TileCoord] | None = None,
    start_pos: tuple[float, float] | None = None,
    goal_pos: tuple[float, float] | None = None,
) -> set[TileCoord]:
    """Build the set of blocked 2D integer tiles for character navigation."""
    blocked: set[TileCoord] = set()
    if cartographer is not None:
        blocked.update(cartographer.water_tiles)
        for obs in cartographer.obstacles:
            blocked.update(obs.occupied_tiles)
    if extra_blocked is not None:
        blocked.update(extra_blocked)

    if data is not None:
        register_prototype_boxes(data.prototypes)

    for raw in placed_entities:
        if isinstance(raw, Mapping):
            name = str(raw.get("name", ""))
            pos = raw.get("position")
            if isinstance(pos, Mapping):
                ex = float(pos["x"])
                ey = float(pos["y"])
            else:
                ex = float(raw.get("x", 0.0))
                ey = float(raw.get("y", 0.0))
            ed = int(raw.get("direction", DIRECTION_NORTH))
        else:
            name = raw.name
            ex = raw.x
            ey = raw.y
            ed = raw.direction
        if not name or name in WALKABLE_ENTITY_NAMES:
            continue
        if data is not None:
            try:
                w_f, h_f = entity_size(name, ed)
                w, h = max(1, int(w_f)), max(1, int(h_f))
                x0 = math.floor(ex - w / HALF_DIVISOR + BOX_EPSILON)
                y0 = math.floor(ey - h / HALF_DIVISOR + BOX_EPSILON)
                for dx in range(w):
                    for dy in range(h):
                        blocked.add((x0 + dx, y0 + dy))
                continue
            except KeyError:
                pass
        blocked.add((math.floor(ex), math.floor(ey)))

    if start_pos is not None:
        blocked.discard((math.floor(start_pos[0]), math.floor(start_pos[1])))
    if goal_pos is not None:
        blocked.discard((math.floor(goal_pos[0]), math.floor(goal_pos[1])))
    return blocked


def _octile_heuristic(tile_a: TileCoord, tile_b: TileCoord) -> float:
    """Compute the 8-way octile distance between two tile coordinates."""
    dx = abs(tile_a[0] - tile_b[0])
    dy = abs(tile_a[1] - tile_b[1])
    return max(dx, dy) + OCTANT_TAN_THRESHOLD * min(dx, dy)


def find_character_walk_path(
    start_x: float,
    start_y: float,
    goal_x: float,
    goal_y: float,
    blocked_tiles: set[TileCoord] | frozenset[TileCoord] = frozenset(),
    max_expansions: int = 8192,
) -> list[tuple[float, float]]:
    """Find a collision-free 8-way waypoint path from (start_x, start_y) to (goal_x, goal_y)."""
    start_tile: TileCoord = (math.floor(start_x), math.floor(start_y))
    goal_tile: TileCoord = (math.floor(goal_x), math.floor(goal_y))
    final_pt = (round(goal_x, COORD_ROUND_DIGITS), round(goal_y, COORD_ROUND_DIGITS))
    if start_tile == goal_tile:
        return [final_pt]

    g_score: dict[TileCoord, float] = {start_tile: 0.0}
    came_from: dict[TileCoord, TileCoord] = {}
    open_heap: list[tuple[float, float, TileCoord]] = [
        (_octile_heuristic(start_tile, goal_tile), 0.0, start_tile)
    ]
    closed: set[TileCoord] = set()

    best_tile = start_tile
    best_h = _octile_heuristic(start_tile, goal_tile)
    expansions = 0

    while open_heap and expansions < max_expansions:
        _f, cur_g, cur = heapq.heappop(open_heap)
        if cur in closed:
            continue
        closed.add(cur)
        expansions += 1

        if cur == goal_tile:
            best_tile = cur
            break

        cur_h = _octile_heuristic(cur, goal_tile)
        if cur_h < best_h:
            best_h = cur_h
            best_tile = cur

        cx, cy = cur
        for dx, dy, step_cost in CARDINAL_AND_DIAGONAL_STEPS:
            nx, ny = cx + dx, cy + dy
            nxt = (nx, ny)
            if nxt in closed:
                continue
            if nxt != goal_tile and nxt in blocked_tiles:
                continue
            # Prevent diagonal corner-cutting across blocked cardinal neighbors
            if (
                dx != 0
                and dy != 0
                and ((cx + dx, cy) in blocked_tiles or (cx, cy + dy) in blocked_tiles)
            ):
                continue
            tentative_g = cur_g + step_cost
            if tentative_g < g_score.get(nxt, float("inf")):
                g_score[nxt] = tentative_g
                came_from[nxt] = cur
                f_val = tentative_g + _octile_heuristic(nxt, goal_tile)
                heapq.heappush(open_heap, (f_val, tentative_g, nxt))

    target_tile = goal_tile if goal_tile in came_from else best_tile
    if target_tile == start_tile:
        return [final_pt]

    rev_tiles: list[TileCoord] = [target_tile]
    cur_t = target_tile
    while cur_t in came_from:
        cur_t = came_from[cur_t]
        rev_tiles.append(cur_t)
    tiles = list(reversed(rev_tiles))

    # Compress colinear 8-way segments into turning-point waypoints
    waypoints: list[tuple[float, float]] = []
    for idx in range(1, len(tiles) - 1):
        prev_t = tiles[idx - 1]
        curr_t = tiles[idx]
        next_t = tiles[idx + 1]
        dir1 = (curr_t[0] - prev_t[0], curr_t[1] - prev_t[1])
        dir2 = (next_t[0] - curr_t[0], next_t[1] - curr_t[1])
        if dir1 != dir2:
            waypoints.append((curr_t[0] + TILE_CENTER_OFFSET, curr_t[1] + TILE_CENTER_OFFSET))

    if target_tile == goal_tile:
        waypoints.append(final_pt)
    else:
        waypoints.append((target_tile[0] + TILE_CENTER_OFFSET, target_tile[1] + TILE_CENTER_OFFSET))
    return waypoints


def compute_8way_walk_vector(
    player_x: float,
    player_y: float,
    target_x: float,
    target_y: float,
    stop_tolerance: float = DEFAULT_STOP_TOLERANCE,
    scale_near_target: bool = False,
) -> tuple[float, float] | None:
    """Compute an 8-way walking vector toward (target_x, target_y), or None if within tolerance."""
    dx = target_x - player_x
    dy = target_y - player_y
    dist = math.hypot(dx, dy)
    if dist <= stop_tolerance:
        return None

    if abs(dy) <= abs(dx) * OCTANT_TAN_THRESHOLD:
        ux, uy = (1.0 if dx > 0.0 else -1.0), 0.0
    elif abs(dx) <= abs(dy) * OCTANT_TAN_THRESHOLD:
        ux, uy = 0.0, (1.0 if dy > 0.0 else -1.0)
    else:
        ux = DIAG_UNIT if dx > 0.0 else -DIAG_UNIT
        uy = DIAG_UNIT if dy > 0.0 else -DIAG_UNIT

    if scale_near_target and dist < stop_tolerance + APPROACH_SLOW_RADIUS:
        scale = max(
            MIN_WALK_SCALE,
            min(1.0, (dist - stop_tolerance * 0.5) / APPROACH_SLOW_RADIUS),
        )
        return (round(ux * scale, 4), round(uy * scale, 4))
    return (round(ux, 4), round(uy, 4))


class CharacterNavigator:
    """Zero-inertia 8-way character movement controller backed by 2D A* pathfinding."""

    def __init__(self) -> None:
        self.goal: tuple[float, float] | None = None
        self.waypoints: list[tuple[float, float]] = []
        self.dynamic_blocked_tiles: set[TileCoord] = set()
        self._last_walk_vec: tuple[float, float] | None = None
        self._last_walk_tick: int = -1000
        self._last_pos: tuple[float, float] | None = None
        self._stuck_ticks: int = 0
        self._stuck_retries: int = 0

    def reset(self) -> None:
        """Reset current path and walking state."""
        self.goal = None
        self.waypoints.clear()
        self._last_walk_vec = None
        self._last_pos = None
        self._stuck_ticks = 0
        self._stuck_retries = 0

    def stop(
        self,
        proxy: FactorioLockstepProxy,
        world_state: PlayerWorldState,
        player_index: int = 0,
    ) -> None:
        """Queue a single StopWalking action if the character is walking."""
        if world_state.walking or self._last_walk_vec is not None:
            act_type, payload = build_stop_walking_action()
            proxy.queue_action(act_type, player_index, payload)
        self.reset()

    def step(
        self,
        proxy: FactorioLockstepProxy,
        world_state: PlayerWorldState,
        target_x: float,
        target_y: float,
        stop_tolerance: float = DEFAULT_STOP_TOLERANCE,
        cartographer: MapCartographer | None = None,
        data: solve.Data | None = None,
        player_index: int = 0,
    ) -> bool:
        """Advance character navigation toward (target_x, target_y). Return True on arrival."""
        px = world_state.player_x
        py = world_state.player_y
        dist_to_goal = math.hypot(target_x - px, target_y - py)
        if dist_to_goal <= stop_tolerance:
            self.stop(proxy, world_state, player_index=player_index)
            return True

        goal_key = (round(target_x, COORD_ROUND_DIGITS), round(target_y, COORD_ROUND_DIGITS))
        if self.goal != goal_key or not self.waypoints:
            blocked = build_character_blocked_tiles(
                cartographer=cartographer,
                placed_entities=world_state.placed_entities,
                data=data,
                extra_blocked=self.dynamic_blocked_tiles,
                start_pos=(px, py),
                goal_pos=goal_key,
            )
            self.waypoints = find_character_walk_path(
                start_x=px,
                start_y=py,
                goal_x=target_x,
                goal_y=target_y,
                blocked_tiles=blocked,
            )
            self.goal = goal_key
            self._stuck_ticks = 0

        wp_pass_tol = max(WAYPOINT_PASS_TOLERANCE, min(stop_tolerance, 0.85))
        while (
            len(self.waypoints) > 1
            and math.hypot(self.waypoints[0][0] - px, self.waypoints[0][1] - py) <= wp_pass_tol
        ):
            self.waypoints.pop(0)

        wx, wy = self.waypoints[0]
        is_final_wp = len(self.waypoints) == 1
        cur_tol = stop_tolerance if is_final_wp else WAYPOINT_PASS_TOLERANCE
        walk_vec = compute_8way_walk_vector(
            player_x=px,
            player_y=py,
            target_x=wx,
            target_y=wy,
            stop_tolerance=cur_tol,
            scale_near_target=is_final_wp,
        )
        if walk_vec is None:
            if is_final_wp:
                self.stop(proxy, world_state, player_index=player_index)
                return True
            self.waypoints.pop(0)
            wx, wy = self.waypoints[0]
            walk_vec = compute_8way_walk_vector(
                player_x=px,
                player_y=py,
                target_x=wx,
                target_y=wy,
                stop_tolerance=stop_tolerance,
                scale_near_target=True,
            )
            if walk_vec is None:
                self.stop(proxy, world_state, player_index=player_index)
                return True

        reissue_due = (world_state.tick - self._last_walk_tick) >= WALK_REISSUE_TICKS
        stuck_blocked = False
        if reissue_due and self._last_pos is not None:
            moved_dist = math.hypot(px - self._last_pos[0], py - self._last_pos[1])
            if moved_dist < 0.15:
                self._stuck_ticks += max(1, world_state.tick - self._last_walk_tick)
                if self._stuck_ticks >= STUCK_TICKS_THRESHOLD:
                    stuck_blocked = True
            else:
                self._stuck_ticks = 0
                self._stuck_retries = 0

        if stuck_blocked:
            self._stuck_retries += 1
            self._stuck_ticks = 0
            if self._last_walk_vec is not None:
                ahead_tile = (
                    math.floor(px + self._last_walk_vec[0] * 0.9),
                    math.floor(py + self._last_walk_vec[1] * 0.9),
                )
                if ahead_tile != (math.floor(px), math.floor(py)):
                    self.dynamic_blocked_tiles.add(ahead_tile)
                if self._stuck_retries >= 4:
                    self.dynamic_blocked_tiles.clear()
                    self._stuck_retries = 0
            blocked = build_character_blocked_tiles(
                cartographer=cartographer,
                placed_entities=world_state.placed_entities,
                data=data,
                extra_blocked=self.dynamic_blocked_tiles,
                start_pos=(px, py),
                goal_pos=goal_key,
            )
            self.waypoints = find_character_walk_path(
                start_x=px,
                start_y=py,
                goal_x=target_x,
                goal_y=target_y,
                blocked_tiles=blocked,
            )
            wx, wy = self.waypoints[0]
            new_vec = compute_8way_walk_vector(
                player_x=px,
                player_y=py,
                target_x=wx,
                target_y=wy,
                stop_tolerance=stop_tolerance
                if len(self.waypoints) == 1
                else WAYPOINT_PASS_TOLERANCE,
                scale_near_target=len(self.waypoints) == 1,
            )
            if new_vec is not None and new_vec != self._last_walk_vec:
                walk_vec = new_vec
            else:
                dx = target_x - px
                dy = target_y - py
                if self._stuck_retries % 2 == 1:
                    if abs(walk_vec[0]) > 0.0:
                        walk_vec = (0.0, 1.0 if dy >= 0.0 else -1.0)
                    else:
                        walk_vec = (1.0 if dx >= 0.0 else -1.0, 0.0)
                else:
                    walk_vec = (
                        round((1.0 if dx >= 0.0 else -1.0) * DIAG_UNIT, 4),
                        round((1.0 if dy >= 0.0 else -1.0) * DIAG_UNIT, 4),
                    )

        vec_changed = (
            self._last_walk_vec is None
            or math.hypot(
                walk_vec[0] - self._last_walk_vec[0], walk_vec[1] - self._last_walk_vec[1]
            )
            >= 0.08
        )
        if (
            (vec_changed and self._stuck_retries == 0)
            or (not world_state.walking and reissue_due)
            or stuck_blocked
        ):
            act_type, payload = build_start_walking_action(walk_vec[0], walk_vec[1])
            proxy.queue_action(act_type, player_index, payload)
            self._last_walk_vec = walk_vec
            self._last_walk_tick = world_state.tick
            self._last_pos = (px, py)
        elif reissue_due:
            self._last_walk_tick = world_state.tick
            self._last_pos = (px, py)
            if vec_changed:
                act_type, payload = build_start_walking_action(walk_vec[0], walk_vec[1])
                proxy.queue_action(act_type, player_index, payload)
                self._last_walk_vec = walk_vec
        return False


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
        self.navigator = CharacterNavigator()
        self._active_mining_task: int | None = None
        self._last_action_tick: int = -1000
        self._collecting_mall_item: str | None = None
        self._collecting_mall_target_qty: int = 0
        self._last_mall_collect_tick: int = -1000

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
        self.navigator.reset()
        self._active_mining_task = None
        self._last_action_tick = -1000
        self._collecting_mall_item = None
        self._collecting_mall_target_qty = 0
        self._last_mall_collect_tick = -1000
        return self.plan

    def current_task(self, allowed_phases: set[str] | None = None) -> BotBuildTask | None:
        """Return the next uncompleted BotBuildTask in the construction schedule."""
        if self.plan is None:
            return None
        for task in self.plan.tasks:
            if task.task_index not in self.completed_task_indices:
                if allowed_phases is not None and task.phase not in allowed_phases:
                    continue
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
        allowed_phases: set[str] | None = None,
    ) -> str:
        """Advance construction by one control step over a FactorioLockstepProxy."""
        task = self.current_task(allowed_phases=allowed_phases)
        if task is None:
            return "completed"

        if proxy.pending_action_count() > 0 or world_state.tick == self._last_action_tick:
            return "waiting_for_queue"

        if task.task_kind == TASK_PLACE_ENTITY and any(
            ent.name == task.target_name
            and math.hypot(ent.x - task.target_x, ent.y - task.target_y) < 0.25
            for ent in world_state.placed_entities
        ):
            self.mark_task_complete(task.task_index)
            return f"already_placed_task_{task.task_index}"

        if task.task_kind in (TASK_PLACE_ENTITY, TASK_SEED_FUEL):
            required_item = task.target_name
            available_qty = int(world_state.inventory.get(required_item, 0))
            if self._collecting_mall_item == required_item:
                if available_qty >= self._collecting_mall_target_qty:
                    self._collecting_mall_item = None
                    self._collecting_mall_target_qty = 0
                else:
                    mall_task = self.find_mall_producer_task(required_item)
                    if mall_task is not None:
                        arrived_mall = self.navigator.step(
                            proxy=proxy,
                            world_state=world_state,
                            target_x=mall_task.stand_x,
                            target_y=mall_task.stand_y,
                            stop_tolerance=stop_tolerance,
                            cartographer=self.cartographer,
                            data=self.data,
                            player_index=player_index,
                        )
                        if not arrived_mall:
                            return f"walking_to_mall_for_{required_item}"
                        if world_state.tick - self._last_mall_collect_tick >= 120:
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
                            for act_type, payload in encode_task_input_actions(
                                collect_task, id_mappings
                            ):
                                proxy.queue_action(act_type, player_index, payload)
                            self._last_action_tick = world_state.tick
                            self._last_mall_collect_tick = world_state.tick
                        return f"collecting_mall_{required_item}"
                    self._collecting_mall_item = None
                    self._collecting_mall_target_qty = 0
            elif available_qty < 1:
                mall_task = self.find_mall_producer_task(required_item)
                if mall_task is not None:
                    rem_needed = (
                        sum(
                            1
                            for t in self.plan.tasks
                            if t.task_index not in self.completed_task_indices
                            and t.task_kind == TASK_PLACE_ENTITY
                            and t.target_name == required_item
                        )
                        if self.plan is not None
                        else 1
                    )
                    self._collecting_mall_item = required_item
                    self._collecting_mall_target_qty = max(1, min(rem_needed, 200))
                    arrived_mall = self.navigator.step(
                        proxy=proxy,
                        world_state=world_state,
                        target_x=mall_task.stand_x,
                        target_y=mall_task.stand_y,
                        stop_tolerance=stop_tolerance,
                        cartographer=self.cartographer,
                        data=self.data,
                        player_index=player_index,
                    )
                    if not arrived_mall:
                        return f"walking_to_mall_for_{required_item}"
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
                    self._last_mall_collect_tick = world_state.tick
                    return f"collecting_mall_{required_item}"
                self.navigator.stop(proxy, world_state, player_index=player_index)
                return f"waiting_for_inventory_{required_item}"

        arrived = self.navigator.step(
            proxy=proxy,
            world_state=world_state,
            target_x=task.stand_x,
            target_y=task.stand_y,
            stop_tolerance=stop_tolerance,
            cartographer=self.cartographer,
            data=self.data,
            player_index=player_index,
        )
        if not arrived:
            return f"walking_to_task_{task.task_index}"

        if (
            task.task_kind == TASK_PLACE_ENTITY
            and task.target_name not in WALKABLE_ENTITY_NAMES
            and math.hypot(
                task.target_x - world_state.player_x,
                task.target_y - world_state.player_y,
            )
            < 1.0
        ):
            arrived_exact = self.navigator.step(
                proxy=proxy,
                world_state=world_state,
                target_x=task.stand_x,
                target_y=task.stand_y,
                stop_tolerance=0.45,
                cartographer=self.cartographer,
                data=self.data,
                player_index=player_index,
            )
            if not arrived_exact:
                return f"walking_to_task_{task.task_index}"

        if task.task_kind == TASK_CLEAR_OBSTACLE:
            dist_obs = math.hypot(
                task.target_x - world_state.player_x,
                task.target_y - world_state.player_y,
            )
            if dist_obs > 2.35 and self._active_mining_task != task.task_index:
                arrived_close = self.navigator.step(
                    proxy=proxy,
                    world_state=world_state,
                    target_x=task.target_x,
                    target_y=task.target_y,
                    stop_tolerance=1.9,
                    cartographer=self.cartographer,
                    data=self.data,
                    player_index=player_index,
                )
                if not arrived_close:
                    return f"walking_to_task_{task.task_index}"
            if self._active_mining_task != task.task_index:
                for act_type, payload in encode_task_input_actions(task, id_mappings):
                    proxy.queue_action(act_type, player_index, payload)
                self._active_mining_task = task.task_index
                self._last_action_tick = world_state.tick
                return f"mining_obstacle_{task.task_index}"
            if not world_state.mining and (
                len(world_state.placed_entities) == 0
                or (world_state.tick - self._last_action_tick) >= 12
            ):
                stop_type, stop_payload = build_stop_mining_action()
                proxy.queue_action(stop_type, player_index, stop_payload)
                self.mark_task_complete(task.task_index)
                self._last_action_tick = world_state.tick
                return f"cleared_obstacle_{task.task_index}"
            return f"mining_obstacle_{task.task_index}"

        reach_limit = (
            max(
                stop_tolerance + 2.0,
                self.plan.reach_limits.build_reach - REACH_SAFETY_MARGIN,
            )
            if self.plan is not None
            else 9.0
        )
        if (
            math.hypot(
                task.target_x - world_state.player_x,
                task.target_y - world_state.player_y,
            )
            > reach_limit
        ):
            arrived_within_reach = self.navigator.step(
                proxy=proxy,
                world_state=world_state,
                target_x=task.stand_x,
                target_y=task.stand_y,
                stop_tolerance=min(0.65, stop_tolerance),
                cartographer=self.cartographer,
                data=self.data,
                player_index=player_index,
            )
            if not arrived_within_reach:
                return f"walking_to_task_{task.task_index}"

        inv_remaining: dict[str, int] = dict(world_state.inventory)
        last_executed_idx = task.task_index
        while True:
            cur_t = self.current_task(allowed_phases=allowed_phases)
            if cur_t is None or cur_t.task_kind == TASK_CLEAR_OBSTACLE:
                break
            cur_dist_target = math.hypot(
                cur_t.target_x - world_state.player_x,
                cur_t.target_y - world_state.player_y,
            )
            if (
                cur_t.task_index != task.task_index
                and cur_dist_target < 1.0
                and cur_t.target_name not in WALKABLE_ENTITY_NAMES
            ):
                break
            if cur_dist_target > reach_limit and math.hypot(
                cur_t.stand_x - world_state.player_x,
                cur_t.stand_y - world_state.player_y,
            ) > min(0.65, stop_tolerance):
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


class BootstrapController:
    """Declarative controller for Phase 0 Lazy Bastard bootstrap and Phase 1 base construction."""

    def __init__(
        self,
        bot_agent: BotAgent,
        id_mappings: Mapping[str, Mapping[str, int]],
        mining_targets: Sequence[tuple[str, int]] = (
            ("iron-ore", 320),
            ("stone", 40),
            ("wood", 40),
            ("coal", 350),
            ("copper-ore", 220),
        ),
        player_index: int = 0,
    ) -> None:
        self.bot_agent = bot_agent
        self.id_mappings = id_mappings
        self.mining_targets = tuple(mining_targets)
        self.player_index = player_index
        self.navigator = CharacterNavigator()

        self.stage: str = "prime_latency"
        self.substep: int = 0
        self.patch_idx: int = 0
        self.mined_ore: int = 0
        self.mined_resources: dict[str, int] = {name: 0 for name, _goal in self.mining_targets}
        self.res_name: str | None = None
        self.res_x: float = 0.0
        self.res_y: float = 0.0
        self.site: ShorelineSite | None = None

        self.recording_ready: bool = False
        self.final_science_load_queued: bool = False
        self.phase1_planned: bool = False
        self.phase1_collected_queued: bool = False
        self.phase1_inventory_ready: bool = False
        self.phase1_target_task_indices: set[int] = (
            {t.task_index for t in bot_agent.plan.tasks} if bot_agent.plan is not None else set()
        )

        self._primed_walk: bool = False
        self._primed_stop: bool = False
        self._mining_active: bool = False
        self._last_mine_tick: int = -1000
        self._last_action_tick: int = -1000
        self._smelt_last_tick: int = 0
        self._mc_target: int = 0
        self._batch_idx: int = 0
        self._batch_reload_idx: int = 0
        self._batch_start_tick: int = 0
        self._deconstruct_idx: int = 0
        self._deconstruct_mining: bool = False
        self._iron_drill_tasks: tuple[tuple[BotBuildTask, tuple[float, float]], ...] = ()
        self._iron_drill_idx: int = 0
        self._temp_furnace_pts: tuple[tuple[float, float], ...] = ()
        self._temp_furnace_idx: int = 0
        self._temp_furnace_mining: bool = False
        self._core_mall_seeded: bool = False

        # Initial drill + pole fabrication batches before running patch electric drills
        self.drill_batches: tuple[tuple[str, tuple[str, ...], int, int], ...] = (
            ("stone-furnace", ("stone",), 3, 1),
            ("iron-gear-wheel", ("iron-plate",), 40, 1),
            ("automation-science-pack", ("copper-plate", "iron-gear-wheel"), 25, 1),
            ("copper-cable", ("copper-plate",), 50, 1),
            ("electronic-circuit", ("copper-cable", "iron-plate"), 12, 1),
            ("small-electric-pole", ("wood", "copper-cable"), 16, 1),
            (
                "electric-mining-drill",
                ("electronic-circuit", "iron-gear-wheel", "iron-plate"),
                3,
                1,
            ),
        )

        # Declarative BOM fabrication recipe batches: (recipe, ingredients, crafts_per_batch, batches)
        self._iron_plate_target: int = 730
        self.bom_batches: tuple[tuple[str, tuple[str, ...], int, int], ...] = ()
        self._configure_bom_batches_from_plan()

    def _configure_bom_batches_from_plan(self) -> None:
        """Compute bom_batches and required iron-plate target dynamically from bot_agent.plan."""
        bom = self.bot_agent.plan.bill_of_materials if self.bot_agent.plan is not None else {}
        need_ug_crafts = max(1, math.ceil(bom.get("underground-belt", 2) / 2))
        need_splitters = max(2, bom.get("splitter", 2))
        need_long_ins = max(5, bom.get("long-handed-inserter", 5))
        need_reg_ins = max(16, bom.get("inserter", 16))
        need_ins_crafts = need_reg_ins + need_long_ins
        need_edrills = max(3, bom.get("electric-mining-drill", 6) - 3)
        need_asms = max(3, bom.get("assembling-machine-1", 4) - 1)

        belt_batches = 5
        min_belt_items = 350 + 2 * need_ug_crafts + 4 * need_splitters
        belt_crafts_per_batch = math.ceil(min_belt_items / (2 * belt_batches))
        total_belt_crafts = belt_crafts_per_batch * belt_batches

        total_circuits = 5 * need_splitters + need_ins_crafts + 3 * need_edrills + 3 * need_asms
        need_circuit_crafts = max(1, total_circuits - 3)
        total_cable_crafts = math.ceil(need_circuit_crafts * 3 / 2)
        cable_batches = 2 if total_cable_crafts > 50 else 1
        cable_crafts_per_batch = math.ceil(total_cable_crafts / cable_batches)

        total_gears = (
            total_belt_crafts + need_ins_crafts + need_long_ins + 5 * need_edrills + 5 * need_asms
        )
        gear_batches = 5
        gear_crafts_per_batch = math.ceil(total_gears / gear_batches)

        total_iron_needed = (
            2 * gear_crafts_per_batch * gear_batches
            + need_circuit_crafts
            + total_belt_crafts
            + 10 * need_ug_crafts
            + 5 * need_splitters
            + need_ins_crafts
            + need_long_ins
            + 10 * need_edrills
            + 9 * need_asms
        )
        self._iron_plate_target = max(730, total_iron_needed - 85)
        self.bom_batches = (
            ("iron-gear-wheel", ("iron-plate",), gear_crafts_per_batch, gear_batches),
            ("copper-cable", ("copper-plate",), cable_crafts_per_batch, cable_batches),
            ("electronic-circuit", ("copper-cable", "iron-plate"), need_circuit_crafts, 1),
            (
                "transport-belt",
                ("iron-plate", "iron-gear-wheel"),
                belt_crafts_per_batch,
                belt_batches,
            ),
            ("underground-belt", ("iron-plate", "transport-belt"), need_ug_crafts, 1),
            ("splitter", ("electronic-circuit", "iron-plate", "transport-belt"), need_splitters, 1),
            (
                "inserter",
                ("electronic-circuit", "iron-gear-wheel", "iron-plate"),
                need_ins_crafts,
                1,
            ),
            (
                "long-handed-inserter",
                ("iron-gear-wheel", "iron-plate", "inserter"),
                need_long_ins,
                1,
            ),
            (
                "electric-mining-drill",
                ("electronic-circuit", "iron-gear-wheel", "iron-plate"),
                need_edrills,
                1,
            ),
            (
                "assembling-machine-1",
                ("electronic-circuit", "iron-gear-wheel", "iron-plate"),
                need_asms,
                1,
            ),
        )

    def status_dict(self, state: PlayerWorldState) -> dict[str, Any]:
        """Return a JSON-serializable status snapshot for live test monitoring."""
        cur_batch_rec: str | None = None
        if (
            self.stage == "fabricate_drills"
            and self.substep >= 1
            and self._batch_idx < len(self.drill_batches)
        ):
            cur_batch_rec = self.drill_batches[self._batch_idx][0]
        elif (
            self.stage == "fabricate_bom"
            and self.substep >= 1
            and self._batch_idx < len(self.bom_batches)
        ):
            cur_batch_rec = self.bom_batches[self._batch_idx][0]
        cur_task = self.bot_agent.current_task()
        return {
            "tick": state.tick,
            "stage": self.stage,
            "substep": self.substep,
            "hand_crafted_count": state.hand_crafted_count,
            "machine_crafted_count": state.machine_crafted_count,
            "patch_idx": self.patch_idx,
            "batch_idx": self._batch_idx,
            "batch_reload_idx": self._batch_reload_idx,
            "batch_recipe": cur_batch_rec,
            "mc_target": self._mc_target,
            "deconstruct_idx": self._deconstruct_idx,
            "completed_tasks": len(self.bot_agent.completed_task_indices),
            "total_tasks": len(self.phase1_target_task_indices),
            "current_task": (
                {
                    "index": cur_task.task_index,
                    "phase": cur_task.phase,
                    "kind": cur_task.task_kind,
                    "target": cur_task.target_name,
                    "x": cur_task.target_x,
                    "y": cur_task.target_y,
                }
                if cur_task is not None
                else None
            ),
        }

    def _queue_all(
        self,
        proxy: FactorioLockstepProxy,
        actions: Sequence[tuple[int, bytes]],
        tick: int,
    ) -> None:
        """Queue a sequence of (action_type, payload) pairs and record the tick."""
        for act_type, payload in actions:
            proxy.queue_action(act_type, self.player_index, payload)
        self._last_action_tick = tick

    def mark_bootstrap_shoreline_power_complete(self) -> None:
        """Mark the permanent offshore-pump, boiler, and steam-engine tasks completed."""
        if self.bot_agent.plan is None:
            return
        for t_task in self.bot_agent.plan.tasks:
            if t_task.phase == PHASE_SHORELINE_POWER and t_task.target_name in (
                "offshore-pump",
                "boiler",
                "steam-engine",
                "coal",
            ):
                self.bot_agent.mark_task_complete(t_task.task_index)

    def _derive_plan_shoreline_site(self) -> ShorelineSite | None:
        """Derive ShorelineSite from self.bot_agent.plan.layout.power_array."""
        if self.bot_agent.plan is None:
            return None
        for ent in self.bot_agent.plan.layout.power_array.entities:
            if ent["name"] == "offshore-pump":
                pos = ent["position"]
                px, py = float(pos["x"]), float(pos["y"])
                p_dir = int(ent.get("direction", DIRECTION_NORTH))
                d_idx = CARDINAL_DIRS.index(p_dir) if p_dir in CARDINAL_DIRS else 0
                b_dir = CARDINAL_DIRS[(d_idx + 1) % 4]
                b_dx, b_dy = _rotate_offset(0.5, 2.0, p_dir)
                e_dx, e_dy = _rotate_offset(4.0, 2.0, p_dir)
                po_dx, po_dy = _rotate_offset(2.0, 4.0, p_dir)
                a_dx, a_dy = _rotate_offset(2.0, 6.0, p_dir)
                l_dx, l_dy = _rotate_offset(5.0, 5.5, p_dir)
                f_dx, f_dy = _rotate_offset(-4.5, 6.0, p_dir)
                f2_dx, f2_dy = _rotate_offset(-4.5, 4.0, p_dir)
                f3_dx, f3_dy = _rotate_offset(-4.5, 2.0, p_dir)
                s_dx, s_dy = _rotate_offset(-0.5, 4.5, p_dir)
                return ShorelineSite(
                    pump_x=px,
                    pump_y=py,
                    pump_dir=p_dir,
                    boiler_x=px + b_dx,
                    boiler_y=py + b_dy,
                    boiler_dir=b_dir,
                    engine_x=px + e_dx,
                    engine_y=py + e_dy,
                    engine_dir=b_dir,
                    pole_x=px + po_dx,
                    pole_y=py + po_dy,
                    lab_x=px + l_dx,
                    lab_y=py + l_dy,
                    asm_x=px + a_dx,
                    asm_y=py + a_dy,
                    furnace_x=px + f_dx,
                    furnace_y=py + f_dy,
                    furnace2_x=px + f2_dx,
                    furnace2_y=py + f2_dy,
                    furnace3_x=px + f3_dx,
                    furnace3_y=py + f3_dy,
                    stand_x=px + s_dx,
                    stand_y=py + s_dy,
                )
        return None

    def _furnace_positions(self, site: ShorelineSite) -> tuple[tuple[float, float], ...]:
        """Return the 4 shoreline bootstrap furnace positions (f1, f2, f3, f4)."""
        f4_x = 2.0 * site.furnace_x - site.furnace2_x
        f4_y = 2.0 * site.furnace_y - site.furnace2_y
        return (
            (site.furnace_x, site.furnace_y),
            (site.furnace2_x, site.furnace2_y),
            (site.furnace3_x, site.furnace3_y),
            (f4_x, f4_y),
        )

    def _select_iron_patch_drill_tasks(self) -> None:
        """Select the planned iron-ore electric-mining-drill tasks and temporary smelter positions."""
        if self.bot_agent.plan is None:
            self._iron_drill_tasks = ()
            self._iron_drill_idx = 0
            return
        iron_pa = self.bot_agent.plan.layout.patch_arrays.get("iron-ore")
        if iron_pa is None:
            self._iron_drill_tasks = ()
            self._iron_drill_idx = 0
            return
        iron_drill_keys = {
            (
                round(float(ent["position"]["x"]), COORD_ROUND_DIGITS),
                round(float(ent["position"]["y"]), COORD_ROUND_DIGITS),
            )
            for ent in iron_pa.entities
            if ent["name"] == "electric-mining-drill"
        }
        selected: list[tuple[BotBuildTask, tuple[float, float]]] = []
        for task in self.bot_agent.plan.tasks:
            if (
                task.phase == PHASE_PATCH_MINERS
                and task.task_kind == TASK_PLACE_ENTITY
                and task.target_name == "electric-mining-drill"
                and (
                    round(task.target_x, COORD_ROUND_DIGITS),
                    round(task.target_y, COORD_ROUND_DIGITS),
                )
                in iron_drill_keys
            ):
                step_x, step_y = DIR_STEPS[task.direction]
                sm_x = task.target_x + 2.5 * step_x + (0.5 if step_x == 0 else 0.0)
                sm_y = task.target_y + 2.5 * step_y + (0.5 if step_y == 0 else 0.0)
                selected.append((task, (sm_x, sm_y)))
        self._iron_drill_tasks = tuple(selected)
        self._iron_drill_idx = 0

    def _extract_furnaces_actions(self, site: ShorelineSite) -> list[tuple[int, bytes]]:
        """Build actions to extract output from all 4 shoreline furnaces."""
        f1, f2, f3, f4 = self._furnace_positions(site)
        clr = build_clear_cursor_action()
        ft_out = build_fast_entity_transfer_action(into_entity=False)
        return [
            clr,
            build_selected_entity_action(*f1),
            ft_out,
            build_selected_entity_action(*f2),
            ft_out,
            build_selected_entity_action(*f4),
            ft_out,
            build_selected_entity_action(*f3),
            ft_out,
        ]

    def step(
        self,
        proxy: FactorioLockstepProxy,
        state: PlayerWorldState,
    ) -> str:
        """Advance the autonomous bootstrap and starter base build by one telemetry step."""
        if self.res_name is None and "iron-ore" in state.resources_by_name:
            first_patch = state.resources_by_name["iron-ore"]
            self.res_name = first_patch.name
            cart = self.bot_agent.cartographer
            iron_pts = (
                sorted(cart.resource_patches.get("iron-ore", set())) if cart is not None else []
            )
            if iron_pts:
                self.res_x = sum(pt[0] for pt in iron_pts) / len(iron_pts) + 0.5
                self.res_y = sum(pt[1] for pt in iron_pts) / len(iron_pts) + 0.5
            else:
                self.res_x = first_patch.x
                self.res_y = first_patch.y
        if self.site is None:
            self.site = self._derive_plan_shoreline_site() or state.shoreline_site

        self.mined_ore = max(self.mined_ore, state.inventory.get("iron-ore", 0))
        for r_key in self.mined_resources:
            self.mined_resources[r_key] = max(
                self.mined_resources[r_key], state.inventory.get(r_key, 0)
            )

        if self._primed_stop and state.tick >= 1215:
            self.recording_ready = True

        if proxy.pending_action_count() > 0 or state.tick == self._last_action_tick:
            return self.stage

        # Stage 1: Prime client walking state before tick 1200 so ServerSynchronizer drops latency
        if self.stage == "prime_latency":
            if self.res_name is not None and not self._primed_stop:
                raw_dx = self.res_x - state.player_x
                raw_dy = self.res_y - state.player_y
                if not self._primed_walk:
                    self._primed_walk = True
                    if abs(raw_dx) >= abs(raw_dy):
                        pdx, pdy = 0.0, math.copysign(1.0, raw_dy)
                    else:
                        pdx, pdy = math.copysign(1.0, raw_dx), 0.0
                    self._queue_all(proxy, [build_start_walking_action(pdx, pdy)], state.tick)
                elif state.walking or state.tick >= 240:
                    self._primed_stop = True
                    self._queue_all(proxy, [build_stop_walking_action()], state.tick)
            elif self._primed_stop and self.site is not None and state.tick >= 1200:
                if state.walking:
                    self._queue_all(proxy, [build_stop_walking_action()], state.tick)
                else:
                    self.navigator.reset()
                    self.stage = "harvest_resources"
            return self.stage

        # Stage 2: Harvest raw resources using 2D A* navigation
        if self.stage == "harvest_resources":
            if self.patch_idx >= len(self.mining_targets):
                self.navigator.stop(proxy, state, player_index=self.player_index)
                self._queue_all(proxy, [build_stop_mining_action()], state.tick)
                self.stage = "walk_to_shoreline"
                return self.stage

            cur_res_name, cur_res_goal = self.mining_targets[self.patch_idx]
            if state.inventory.get(cur_res_name, 0) >= cur_res_goal:
                actions: list[tuple[int, bytes]] = [build_stop_mining_action()]
                if state.walking or self.navigator._last_walk_vec is not None:
                    actions.append(build_stop_walking_action())
                self._queue_all(proxy, actions, state.tick)
                self.navigator.reset()
                self._mining_active = False
                self.patch_idx += 1
                return self.stage

            cur_patch = state.resources_by_name.get(cur_res_name)
            if cur_patch is None:
                return self.stage

            stop_reach = 2.25 if cur_res_name == "wood" else 2.05
            mine_reach = 2.60 if cur_res_name == "wood" else 2.45
            dist = math.hypot(cur_patch.x - state.player_x, cur_patch.y - state.player_y)
            tol = mine_reach if self._mining_active else stop_reach

            if dist > tol:
                if self._mining_active:
                    self._mining_active = False
                    self._queue_all(proxy, [build_stop_mining_action()], state.tick)
                    return self.stage
                self.navigator.step(
                    proxy=proxy,
                    world_state=state,
                    target_x=cur_patch.x,
                    target_y=cur_patch.y,
                    stop_tolerance=stop_reach,
                    cartographer=self.bot_agent.cartographer,
                    data=self.bot_agent.data,
                    player_index=self.player_index,
                )
                return self.stage

            if state.walking or self.navigator._last_walk_vec is not None:
                self.navigator.stop(proxy, state, player_index=self.player_index)
                return self.stage

            target_shifted = (
                state.selected_x is None
                or state.selected_y is None
                or math.hypot(cur_patch.x - state.selected_x, cur_patch.y - state.selected_y) > 0.5
            )
            if not self._mining_active or (
                (state.tick - self._last_mine_tick) >= 18 and (not state.mining or target_shifted)
            ):
                mine_actions: list[tuple[int, bytes]] = []
                if self._mining_active:
                    mine_actions.append(build_stop_mining_action())
                mine_actions.append(build_selected_entity_action(cur_patch.x, cur_patch.y))
                mine_actions.append(build_begin_mining_action())
                self._queue_all(proxy, mine_actions, state.tick)
                self._mining_active = True
                self._last_mine_tick = state.tick
            return self.stage

        assert self.site is not None
        site = self.site
        item_map = self.id_mappings["item"]
        recipe_map = self.id_mappings["recipe"]
        tech_map = self.id_mappings["technology"]
        quality_id = int(self.id_mappings.get("quality", {}).get("normal", 1))

        # Stage 3: Navigate to shoreline bootstrap standing position through 2D A*
        if self.stage == "walk_to_shoreline":
            b_dx = abs(site.boiler_x - state.player_x)
            b_dy = abs(site.boiler_y - state.player_y)
            clear_of_boiler = b_dx >= 1.65 or b_dy >= 2.15
            tol = 1.35 if clear_of_boiler else 0.55
            arrived = self.navigator.step(
                proxy=proxy,
                world_state=state,
                target_x=site.stand_x,
                target_y=site.stand_y,
                stop_tolerance=tol,
                cartographer=self.bot_agent.cartographer,
                data=self.bot_agent.data,
                player_index=self.player_index,
            )
            if arrived and not state.walking and clear_of_boiler:
                self.stage = "shoreline_bootstrap"
                self.substep = 0
            return self.stage

        f1, f2, f3, f4 = self._furnace_positions(site)
        clr = build_clear_cursor_action()
        ft_in = build_fast_entity_transfer_action(into_entity=True)
        ft_out = build_fast_entity_transfer_action(into_entity=False)
        fs_in = build_fast_entity_split_action(into_entity=True)
        pip_coal = build_pipette_item_action(item_map["coal"])
        pip_iron_ore = build_pipette_item_action(item_map["iron-ore"])
        pip_copper_ore = build_pipette_item_action(item_map["copper-ore"])

        # Stage 4: Shoreline bootstrap (106 hand crafts + steam power + lab + assembler)
        if self.stage == "shoreline_bootstrap":
            if self.substep == 0:
                self.substep = 1
                self._queue_all(
                    proxy,
                    [build_craft_action(recipe_map["stone-furnace"], count=5)],
                    state.tick,
                )
            elif self.substep == 1 and state.inventory.get("stone-furnace", 0) >= 6:
                self.substep = 2
                self._smelt_last_tick = state.tick
                pip_furnace = build_pipette_item_action(item_map["stone-furnace"])
                actions = [
                    clr,
                    pip_furnace,
                    build_place_entity_action(f1[0], f1[1], direction=0),
                    build_place_entity_action(f2[0], f2[1], direction=0),
                    build_place_entity_action(f3[0], f3[1], direction=0),
                    build_place_entity_action(f4[0], f4[1], direction=0),
                    clr,
                ]
                for f_pos in (f1, f2, f4, f3):
                    actions.extend([build_selected_entity_action(*f_pos), pip_coal, fs_in, clr])
                for f_pos in (f1, f2, f4):
                    actions.extend([build_selected_entity_action(*f_pos), pip_iron_ore, ft_in, clr])
                actions.extend([build_selected_entity_action(*f3), pip_copper_ore, ft_in, clr])
                self._queue_all(proxy, actions, state.tick)
            elif (
                self.substep == 2 and state.steam_power_researched and state.electronics_researched
            ):
                self.substep = 3
                actions = self._extract_furnaces_actions(site)
                for f_pos in (f1, f2, f4):
                    actions.extend([build_selected_entity_action(*f_pos), pip_iron_ore, ft_in, clr])
                actions.extend(
                    [
                        build_craft_action(recipe_map["offshore-pump"], count=1),
                        build_craft_action(recipe_map["boiler"], count=1),
                        build_craft_action(recipe_map["steam-engine"], count=1),
                        build_craft_action(recipe_map["small-electric-pole"], count=1),
                    ]
                )
                self._queue_all(proxy, actions, state.tick)
            elif (
                self.substep == 3
                and state.inventory.get("offshore-pump", 0) >= 1
                and state.inventory.get("boiler", 0) >= 1
                and state.inventory.get("steam-engine", 0) >= 1
                and state.inventory.get("small-electric-pole", 0) >= 2
            ):
                self.substep = 4
                actions = [
                    clr,
                    build_pipette_item_action(item_map["offshore-pump"]),
                    build_place_entity_action(site.pump_x, site.pump_y, direction=site.pump_dir),
                    clr,
                    build_pipette_item_action(item_map["boiler"]),
                    build_place_entity_action(
                        site.boiler_x, site.boiler_y, direction=site.boiler_dir
                    ),
                    clr,
                    build_selected_entity_action(site.boiler_x, site.boiler_y),
                    pip_coal,
                    fs_in,
                    clr,
                    build_pipette_item_action(item_map["steam-engine"]),
                    build_place_entity_action(
                        site.engine_x, site.engine_y, direction=site.engine_dir
                    ),
                    clr,
                    build_pipette_item_action(item_map["small-electric-pole"]),
                    build_place_entity_action(site.pole_x, site.pole_y, direction=0),
                    clr,
                ]
                if self.bot_agent.plan is not None:
                    for t_task in self.bot_agent.plan.tasks:
                        if t_task.phase == PHASE_SHORELINE_POWER and t_task.target_name in (
                            "offshore-pump",
                            "boiler",
                            "steam-engine",
                            "coal",
                        ):
                            self.bot_agent.mark_task_complete(t_task.task_index)
                self._queue_all(proxy, actions, state.tick)
            elif self.substep == 4 and state.tick >= self._smelt_last_tick + 10800:
                self.substep = 5
                actions = self._extract_furnaces_actions(site)
                actions.append(build_craft_action(recipe_map["lab"], count=1))
                self._queue_all(proxy, actions, state.tick)
            elif (
                self.substep == 5
                and state.science_pack_researched
                and state.inventory.get("lab", 0) >= 1
            ):
                self.substep = 6
                actions = [
                    clr,
                    build_pipette_item_action(item_map["lab"]),
                    build_place_entity_action(site.lab_x, site.lab_y, direction=0),
                    clr,
                    build_start_research_action(tech_map["automation"]),
                    build_craft_action(recipe_map["automation-science-pack"], count=10),
                ]
                self._queue_all(proxy, actions, state.tick)
            elif self.substep == 6 and state.inventory.get("automation-science-pack", 0) >= 10:
                self.substep = 7
                self.final_science_load_queued = True
                actions = [
                    clr,
                    build_selected_entity_action(*f1),
                    ft_out,
                    build_selected_entity_action(*f4),
                    ft_out,
                    build_selected_entity_action(*f3),
                    ft_out,
                    build_selected_entity_action(site.boiler_x, site.boiler_y),
                    pip_coal,
                    fs_in,
                    clr,
                    build_selected_entity_action(site.lab_x, site.lab_y),
                    build_pipette_item_action(item_map["automation-science-pack"]),
                    ft_in,
                    clr,
                ]
                self._queue_all(proxy, actions, state.tick)
            elif self.substep == 7 and state.automation_researched:
                self.substep = 8
                actions = self._extract_furnaces_actions(site)
                actions.append(build_craft_action(recipe_map["assembling-machine-1"], count=1))
                self._queue_all(proxy, actions, state.tick)
            elif self.substep == 8 and state.inventory.get("assembling-machine-1", 0) >= 1:
                self.substep = 85
                self._batch_start_tick = state.tick
                actions = [
                    clr,
                    build_pipette_item_action(item_map["assembling-machine-1"]),
                    build_place_entity_action(site.asm_x, site.asm_y, direction=0),
                ]
                self._queue_all(proxy, actions, state.tick)
            elif (
                self.substep == 85
                and any(e.name == "assembling-machine-1" for e in state.placed_entities)
                and state.tick >= self._batch_start_tick + 60
            ):
                self.substep = 9
                self._mc_target = state.asm_made.get("iron-gear-wheel", 0) + 20
                self._smelt_last_tick = state.tick
                actions = [
                    clr,
                    build_start_research_action(tech_map["logistics"]),
                ]
                if state.inventory.get("iron-ore", 0) > 0:
                    for f_pos in (f1, f2, f4):
                        actions.extend(
                            [
                                build_selected_entity_action(*f_pos),
                                pip_iron_ore,
                                ft_in,
                                clr,
                            ]
                        )
                if state.inventory.get("copper-ore", 0) > 0:
                    actions.extend(
                        [
                            build_selected_entity_action(*f3),
                            pip_coal,
                            fs_in,
                            clr,
                            build_selected_entity_action(*f3),
                            pip_copper_ore,
                            ft_in,
                            clr,
                        ]
                    )
                actions.extend(
                    encode_assembler_batch_actions(
                        site.asm_x,
                        site.asm_y,
                        "iron-gear-wheel",
                        ("iron-plate",),
                        self.id_mappings,
                        extract_first=False,
                        quality_id=quality_id,
                    )
                )
                self._queue_all(proxy, actions, state.tick)
            elif self.substep == 9 and state.asm_made.get("iron-gear-wheel", 0) >= self._mc_target:
                self.substep = 10
                self._mc_target = state.asm_made.get("automation-science-pack", 0) + 20
                actions = self._extract_furnaces_actions(site)
                if state.inventory.get("iron-ore", 0) > 0:
                    for f_pos in (f1, f2, f4):
                        actions.extend(
                            [build_selected_entity_action(*f_pos), pip_iron_ore, ft_in, clr]
                        )
                if state.inventory.get("copper-ore", 0) > 0:
                    actions.extend([build_selected_entity_action(*f3), pip_copper_ore, ft_in, clr])
                actions.extend(
                    encode_assembler_batch_actions(
                        site.asm_x,
                        site.asm_y,
                        "automation-science-pack",
                        ("copper-plate", "iron-gear-wheel"),
                        self.id_mappings,
                        extract_first=True,
                        quality_id=quality_id,
                    )
                )
                self._queue_all(proxy, actions, state.tick)
            elif (
                self.substep == 10
                and state.asm_made.get("automation-science-pack", 0) >= self._mc_target
            ):
                self.substep = 11
                self._batch_start_tick = state.tick
                actions = [
                    clr,
                    build_selected_entity_action(site.asm_x, site.asm_y),
                    ft_out,
                    clr,
                ]
                self._queue_all(proxy, actions, state.tick)
            elif self.substep == 11 and (
                state.inventory.get("automation-science-pack", 0) >= 20
                or state.tick >= self._batch_start_tick + 90
            ):
                self.final_science_load_queued = True
                actions = [
                    clr,
                    build_selected_entity_action(site.asm_x, site.asm_y),
                    ft_out,
                    build_selected_entity_action(site.lab_x, site.lab_y),
                    build_pipette_item_action(item_map["automation-science-pack"]),
                    ft_in,
                    clr,
                ]
                self._queue_all(proxy, actions, state.tick)
                self.stage = "fabricate_drills"
                self.substep = 0
            return self.stage

        # Stage 4b: Fabricate initial electric-mining-drills (3) and small-electric-poles (11)
        if self.stage == "fabricate_drills":
            if self.substep == 0:
                have_iron = state.inventory.get("iron-plate", 0)
                have_copper = state.inventory.get("copper-plate", 0)
                if state.logistics_researched and have_iron >= 125 and have_copper >= 50:
                    self.substep = 1
                    self._batch_idx = 0
                    self._batch_reload_idx = 1
                    rec_name, ings, batch_crafts, _num_batches = self.drill_batches[0]
                    self._mc_target = state.asm_made.get(rec_name, 0) + batch_crafts
                    self._batch_start_tick = state.tick
                    actions = self._extract_furnaces_actions(site)
                    if state.inventory.get("iron-ore", 0) > 0:
                        for f_pos in (f1, f2, f4):
                            actions.extend(
                                [build_selected_entity_action(*f_pos), pip_iron_ore, ft_in, clr]
                            )
                    if state.inventory.get("copper-ore", 0) > 0:
                        actions.extend(
                            [build_selected_entity_action(*f3), pip_copper_ore, ft_in, clr]
                        )
                    if "electric-mining-drill" in tech_map:
                        actions.append(
                            build_start_research_action(tech_map["electric-mining-drill"])
                        )
                    actions.extend(
                        encode_assembler_batch_actions(
                            site.asm_x,
                            site.asm_y,
                            rec_name,
                            ings,
                            self.id_mappings,
                            extract_first=True,
                            quality_id=quality_id,
                        )
                    )
                    self._queue_all(proxy, actions, state.tick)
                    return self.stage

                if state.tick >= self._smelt_last_tick + 3600:
                    self._smelt_last_tick = state.tick
                    actions = self._extract_furnaces_actions(site)
                    if state.inventory.get("copper-ore", 0) > 0:
                        actions.extend(
                            [
                                build_selected_entity_action(*f3),
                                pip_copper_ore,
                                ft_in,
                                clr,
                            ]
                        )
                    if not state.logistics_researched and "logistics" in tech_map:
                        actions.append(build_start_research_action(tech_map["logistics"]))
                    self._queue_all(proxy, actions, state.tick)
                return self.stage

            if self.substep == 1:
                rec_name, ings, batch_crafts, _num_batches = self.drill_batches[self._batch_idx]
                if state.asm_made.get(rec_name, 0) >= self._mc_target:
                    if self._batch_idx + 1 < len(self.drill_batches):
                        next_rec, next_ings, next_crafts, _next_batches = self.drill_batches[
                            self._batch_idx + 1
                        ]
                        if (
                            next_rec == "electric-mining-drill"
                            and not state.electric_mining_drill_researched
                        ):
                            if state.tick >= self._batch_start_tick + 60:
                                self._batch_start_tick = state.tick
                                actions = [
                                    clr,
                                    build_selected_entity_action(site.asm_x, site.asm_y),
                                    ft_out,
                                ]
                                if state.inventory.get("automation-science-pack", 0) > 0:
                                    actions.extend(
                                        [
                                            build_selected_entity_action(site.lab_x, site.lab_y),
                                            build_pipette_item_action(
                                                item_map["automation-science-pack"]
                                            ),
                                            ft_in,
                                            clr,
                                        ]
                                    )
                                if "electric-mining-drill" in tech_map:
                                    actions.append(
                                        build_start_research_action(
                                            tech_map["electric-mining-drill"]
                                        )
                                    )
                                self._queue_all(proxy, actions, state.tick)
                            return self.stage
                        self._batch_idx += 1
                        self._mc_target = state.asm_made.get(next_rec, 0) + next_crafts
                        self._batch_start_tick = state.tick
                        actions = [
                            clr,
                            build_selected_entity_action(*f3),
                            ft_out,
                        ]
                        if (
                            state.inventory.get("automation-science-pack", 0) > 0
                            and not state.electric_mining_drill_researched
                        ):
                            actions.extend(
                                [
                                    build_selected_entity_action(site.lab_x, site.lab_y),
                                    build_pipette_item_action(item_map["automation-science-pack"]),
                                    ft_in,
                                    clr,
                                ]
                            )
                        actions.extend(
                            encode_assembler_batch_actions(
                                site.asm_x,
                                site.asm_y,
                                next_rec,
                                next_ings,
                                self.id_mappings,
                                extract_first=True,
                                quality_id=quality_id,
                            )
                        )
                        self._queue_all(proxy, actions, state.tick)
                        return self.stage

                    if state.tick >= self._batch_start_tick + 75:
                        actions = [
                            clr,
                            build_selected_entity_action(site.asm_x, site.asm_y),
                            ft_out,
                        ]
                        actions.extend(self._extract_furnaces_actions(site))
                        actions.extend(
                            [
                                build_selected_entity_action(site.boiler_x, site.boiler_y),
                                pip_coal,
                                ft_in,
                                clr,
                            ]
                        )
                        if state.inventory.get("copper-ore", 0) > 0:
                            actions.extend(
                                [
                                    build_selected_entity_action(*f3),
                                    pip_coal,
                                    fs_in,
                                    clr,
                                    build_selected_entity_action(*f3),
                                    pip_copper_ore,
                                    ft_in,
                                    clr,
                                ]
                            )
                        self._queue_all(proxy, actions, state.tick)
                        proxy.max_actions_per_heartbeat = 12
                        self._configure_bom_batches_from_plan()
                        self._select_iron_patch_drill_tasks()
                        self.stage = "run_patch_drills"
                        self.substep = 0
                        self._deconstruct_idx = 0
                        self._deconstruct_mining = False
            return self.stage

        # Stage 4c: Build permanent power network + iron-ore drills, smelt plates in temporary furnaces
        if self.stage == "run_patch_drills":
            if self.substep == 0:
                self.substep = 1
                return self.stage

            # Substep 1: Execute PHASE_CLEAR_OBSTACLES and PHASE_POWER_NETWORK
            if self.substep == 1:
                early_phases = {
                    PHASE_CLEAR_OBSTACLES,
                    PHASE_POWER_NETWORK,
                }
                if self.bot_agent.current_task(allowed_phases=early_phases) is not None:
                    self.bot_agent.step_lockstep_proxy(
                        proxy=proxy,
                        world_state=state,
                        id_mappings=self.id_mappings,
                        player_index=self.player_index,
                        stop_tolerance=1.5,
                        allowed_phases=early_phases,
                    )
                    return self.stage
                self.navigator.reset()
                self.substep = 2
                return self.stage

            # Substep 2: Place permanent iron-ore drills + temporary stone-furnaces at drop tiles
            if self.substep == 2:
                if self._iron_drill_idx < len(self._iron_drill_tasks):
                    d_task, f_pos = self._iron_drill_tasks[self._iron_drill_idx]
                    step_x, step_y = DIR_STEPS[d_task.direction]
                    stand_px = d_task.target_x - 3.0 * step_x
                    stand_py = d_task.target_y - 3.0 * step_y
                    dist = math.hypot(stand_px - state.player_x, stand_py - state.player_y)
                    if dist > 1.8:
                        arrived_stand = self.navigator.step(
                            proxy=proxy,
                            world_state=state,
                            target_x=stand_px,
                            target_y=stand_py,
                            stop_tolerance=0.8,
                            cartographer=self.bot_agent.cartographer,
                            data=self.bot_agent.data,
                            player_index=self.player_index,
                        )
                        if not arrived_stand:
                            return self.stage
                    if state.walking or self.navigator._last_walk_vec is not None:
                        self.navigator.stop(proxy, state, player_index=self.player_index)
                        return self.stage
                    actions = [
                        clr,
                        build_pipette_item_action(item_map["electric-mining-drill"]),
                        build_place_entity_action(
                            d_task.target_x,
                            d_task.target_y,
                            direction=d_task.direction,
                        ),
                        clr,
                        build_pipette_item_action(item_map["stone-furnace"]),
                        build_place_entity_action(f_pos[0], f_pos[1], direction=0),
                        clr,
                        build_selected_entity_action(f_pos[0], f_pos[1]),
                        pip_coal,
                        fs_in,
                        clr,
                    ]
                    if self._iron_drill_idx > 0:
                        actions.extend(
                            [
                                build_selected_entity_action(f_pos[0], f_pos[1]),
                                pip_coal,
                                fs_in,
                                clr,
                            ]
                        )
                    self.bot_agent.mark_task_complete(d_task.task_index)
                    self._iron_drill_idx += 1
                    self._queue_all(proxy, actions, state.tick)
                    return self.stage
                self.substep = 3
                self._smelt_last_tick = state.tick
                return self.stage

            # Substep 3: Extract iron-plates from the temporary furnaces until >= _iron_plate_target
            if self.substep == 3:
                if (
                    state.inventory.get("iron-plate", 0) >= self._iron_plate_target
                    or not self._iron_drill_tasks
                ):
                    self._temp_furnace_pts = tuple(
                        f_pos for _d_task, f_pos in self._iron_drill_tasks
                    )
                    self._temp_furnace_idx = 0
                    self._temp_furnace_mining = False
                    self.substep = 4
                    return self.stage

                if state.tick >= self._smelt_last_tick + 3600:
                    mid_task, _mid_f = self._iron_drill_tasks[len(self._iron_drill_tasks) // 2]
                    step_x, step_y = DIR_STEPS[mid_task.direction]
                    stand_px = mid_task.target_x - 3.0 * step_x
                    stand_py = mid_task.target_y - 3.0 * step_y
                    if math.hypot(stand_px - state.player_x, stand_py - state.player_y) > 4.0:
                        arrived_mid = self.navigator.step(
                            proxy=proxy,
                            world_state=state,
                            target_x=stand_px,
                            target_y=stand_py,
                            stop_tolerance=1.2,
                            cartographer=self.bot_agent.cartographer,
                            data=self.bot_agent.data,
                            player_index=self.player_index,
                        )
                        if not arrived_mid:
                            return self.stage
                    if state.walking or self.navigator._last_walk_vec is not None:
                        self.navigator.stop(proxy, state, player_index=self.player_index)
                        return self.stage
                    self._smelt_last_tick = state.tick
                    actions = [clr]
                    for _d_task, f_pos in self._iron_drill_tasks:
                        actions.extend([build_selected_entity_action(*f_pos), ft_out])
                    if state.inventory.get("coal", 0) > 0:
                        for _d_task, f_pos in reversed(self._iron_drill_tasks):
                            actions.extend(
                                [build_selected_entity_action(*f_pos), pip_coal, fs_in, clr]
                            )
                    self._queue_all(proxy, actions, state.tick)
                return self.stage

            # Substep 4: Deconstruct ONLY the temporary stone-furnaces (keep permanent drills + poles!)
            if self.substep == 4:
                if self._temp_furnace_idx < len(self._temp_furnace_pts):
                    tx_d, ty_d = self._temp_furnace_pts[self._temp_furnace_idx]
                    target_still_present = len(state.placed_entities) == 0 or any(
                        math.hypot(ent.x - tx_d, ent.y - ty_d) < 0.75
                        for ent in state.placed_entities
                    )
                    if not target_still_present:
                        if self._temp_furnace_mining or state.mining:
                            self._queue_all(proxy, [build_stop_mining_action()], state.tick)
                        self._temp_furnace_mining = False
                        self._temp_furnace_idx += 1
                        return self.stage

                    dist = math.hypot(tx_d - state.player_x, ty_d - state.player_y)
                    if dist > 2.4 and not self._temp_furnace_mining:
                        arrived_tf = self.navigator.step(
                            proxy=proxy,
                            world_state=state,
                            target_x=tx_d,
                            target_y=ty_d,
                            stop_tolerance=0.5,
                            cartographer=self.bot_agent.cartographer,
                            data=self.bot_agent.data,
                            player_index=self.player_index,
                        )
                        if not arrived_tf:
                            return self.stage
                    if state.walking or self.navigator._last_walk_vec is not None:
                        self.navigator.stop(proxy, state, player_index=self.player_index)
                        return self.stage
                    if not self._temp_furnace_mining:
                        self._temp_furnace_mining = True
                        self._queue_all(
                            proxy,
                            [
                                build_selected_entity_action(tx_d, ty_d),
                                build_begin_mining_action(),
                            ],
                            state.tick,
                        )
                    elif not state.mining and (state.tick - self._last_action_tick) >= 18:
                        if len(state.placed_entities) == 0:
                            self._queue_all(proxy, [build_stop_mining_action()], state.tick)
                            self._temp_furnace_mining = False
                            self._temp_furnace_idx += 1
                        else:
                            self._queue_all(
                                proxy,
                                [
                                    build_selected_entity_action(tx_d, ty_d),
                                    build_begin_mining_action(),
                                ],
                                state.tick,
                            )
                    return self.stage
                self.substep = 5
                return self.stage

            # Substep 5: Walk back to shoreline bootstrap site to fabricate initial BOM
            if self.substep == 5:
                b_dx = abs(site.boiler_x - state.player_x)
                b_dy = abs(site.boiler_y - state.player_y)
                clear_of_boiler = b_dx >= 1.65 or b_dy >= 2.15
                tol = 1.35 if clear_of_boiler else 0.55
                arrived = self.navigator.step(
                    proxy=proxy,
                    world_state=state,
                    target_x=site.stand_x,
                    target_y=site.stand_y,
                    stop_tolerance=tol,
                    cartographer=self.bot_agent.cartographer,
                    data=self.bot_agent.data,
                    player_index=self.player_index,
                )
                if arrived and not state.walking and clear_of_boiler:
                    self.stage = "fabricate_bom"
                    self.substep = 0
                return self.stage

        # Stage 5: Finish smelting plates and fabricate the starter base BOM in batches
        if self.stage == "fabricate_bom":
            if self.substep == 0:
                have_iron_plates = state.inventory.get("iron-plate", 0)
                have_copper_plates = state.inventory.get("copper-plate", 0)

                if (
                    state.logistics_researched
                    and have_iron_plates >= 540
                    and have_copper_plates >= 20
                ):
                    self.substep = 1
                    self._batch_idx = 0
                    self._batch_reload_idx = 1
                    rec_name, ings, batch_crafts, _num_batches = self.bom_batches[0]
                    self._mc_target = state.asm_made.get(rec_name, 0) + batch_crafts
                    self._batch_start_tick = state.tick
                    actions = self._extract_furnaces_actions(site)
                    actions.extend(
                        [
                            build_selected_entity_action(site.boiler_x, site.boiler_y),
                            pip_coal,
                            ft_in,
                            clr,
                        ]
                    )
                    if state.inventory.get("iron-ore", 0) > 0:
                        for f_pos in (f1, f2, f4):
                            actions.extend(
                                [
                                    build_selected_entity_action(*f_pos),
                                    pip_iron_ore,
                                    fs_in,
                                    clr,
                                ]
                            )
                    actions.extend(
                        encode_assembler_batch_actions(
                            site.asm_x,
                            site.asm_y,
                            rec_name,
                            ings,
                            self.id_mappings,
                            extract_first=True,
                            quality_id=quality_id,
                        )
                    )
                    self._queue_all(proxy, actions, state.tick)
                    return self.stage

                if state.tick >= self._smelt_last_tick + 3600:
                    self._smelt_last_tick = state.tick
                    actions = [
                        clr,
                        build_selected_entity_action(*f3),
                        ft_out,
                        build_selected_entity_action(*f3),
                        pip_coal,
                        fs_in,
                        clr,
                    ]
                    if state.inventory.get("copper-ore", 0) > 0:
                        actions.extend(
                            [
                                build_selected_entity_action(*f3),
                                pip_copper_ore,
                                ft_in,
                                clr,
                            ]
                        )
                    self._queue_all(proxy, actions, state.tick)
                return self.stage

            if self.substep == 1:
                rec_name, ings, batch_crafts, num_batches = self.bom_batches[self._batch_idx]
                if state.asm_made.get(rec_name, 0) >= self._mc_target:
                    if self._batch_reload_idx < num_batches:
                        self._batch_reload_idx += 1
                        self._mc_target = state.asm_made.get(rec_name, 0) + batch_crafts
                        self._batch_start_tick = state.tick
                        actions = [
                            clr,
                            build_selected_entity_action(site.asm_x, site.asm_y),
                            ft_out,
                        ]
                        for ing_name in ings:
                            actions.extend(
                                [
                                    build_selected_entity_action(site.asm_x, site.asm_y),
                                    build_pipette_item_action(item_map[ing_name]),
                                    ft_in,
                                    clr,
                                ]
                            )
                        self._queue_all(proxy, actions, state.tick)
                        return self.stage

                    if self._batch_idx + 1 < len(self.bom_batches):
                        self._batch_idx += 1
                        self._batch_reload_idx = 1
                        next_rec, next_ings, next_crafts, _next_batches = self.bom_batches[
                            self._batch_idx
                        ]
                        self._mc_target = state.asm_made.get(next_rec, 0) + next_crafts
                        self._batch_start_tick = state.tick
                        actions = self._extract_furnaces_actions(site)
                        actions.extend(
                            encode_assembler_batch_actions(
                                site.asm_x,
                                site.asm_y,
                                next_rec,
                                next_ings,
                                self.id_mappings,
                                extract_first=True,
                                quality_id=quality_id,
                            )
                        )
                        self._queue_all(proxy, actions, state.tick)
                        return self.stage

                    if state.tick >= self._batch_start_tick + 75:
                        self.phase1_collected_queued = True
                        self.phase1_planned = True
                        actions = [
                            clr,
                            build_selected_entity_action(site.asm_x, site.asm_y),
                            ft_out,
                            build_selected_entity_action(site.boiler_x, site.boiler_y),
                            pip_coal,
                            fs_in,
                            clr,
                        ]
                        actions.extend(self._extract_furnaces_actions(site))
                        self._queue_all(proxy, actions, state.tick)
                        self.stage = "teardown_bootstrap"
                        self._deconstruct_idx = 0
                        self._deconstruct_mining = False
            return self.stage

        # Stage 6: Deconstruct temporary shoreline bootstrap structures (keep pump/boiler/engine!)
        if self.stage == "teardown_bootstrap":
            deconstruct_positions = (
                (site.asm_x, site.asm_y),
                (site.pole_x, site.pole_y),
                (site.lab_x, site.lab_y),
                f1,
                f2,
                f4,
                f3,
            )
            if self._deconstruct_idx < len(deconstruct_positions):
                dx_pos, dy_pos = deconstruct_positions[self._deconstruct_idx]
                target_still_present = len(state.placed_entities) == 0 or any(
                    math.hypot(ent.x - dx_pos, ent.y - dy_pos) < 0.75
                    for ent in state.placed_entities
                )
                if not target_still_present:
                    if self._deconstruct_mining or state.mining:
                        self._queue_all(proxy, [build_stop_mining_action()], state.tick)
                    self._deconstruct_mining = False
                    self._deconstruct_idx += 1
                    return self.stage

                dist_d = math.hypot(dx_pos - state.player_x, dy_pos - state.player_y)
                if dist_d > 2.6 and not self._deconstruct_mining:
                    arrived_d = self.navigator.step(
                        proxy=proxy,
                        world_state=state,
                        target_x=dx_pos,
                        target_y=dy_pos,
                        stop_tolerance=0.5,
                        cartographer=self.bot_agent.cartographer,
                        data=self.bot_agent.data,
                        player_index=self.player_index,
                    )
                    if not arrived_d:
                        return self.stage
                if state.walking or self.navigator._last_walk_vec is not None:
                    self.navigator.stop(proxy, state, player_index=self.player_index)
                    return self.stage
                if not self._deconstruct_mining:
                    self._deconstruct_mining = True
                    self._queue_all(
                        proxy,
                        [
                            build_selected_entity_action(dx_pos, dy_pos),
                            build_begin_mining_action(),
                        ],
                        state.tick,
                    )
                elif not state.mining and (state.tick - self._last_action_tick) >= 18:
                    if len(state.placed_entities) == 0:
                        self._queue_all(proxy, [build_stop_mining_action()], state.tick)
                        self._deconstruct_mining = False
                        self._deconstruct_idx += 1
                    else:
                        self._queue_all(
                            proxy,
                            [
                                build_selected_entity_action(dx_pos, dy_pos),
                                build_begin_mining_action(),
                            ],
                            state.tick,
                        )
                return self.stage

            if state.logistics_researched and state.inventory.get("transport-belt", 0) >= 340:
                self.phase1_inventory_ready = True
                proxy.max_actions_per_heartbeat = 12
                self.stage = "build_starter_base"
            return self.stage

        # Stage 7: Build 100% of the synthesized starter base through BotAgent + CharacterNavigator
        if self.stage == "build_starter_base":
            cur_t = self.bot_agent.current_task()
            if cur_t is not None:
                if (
                    not self._core_mall_seeded
                    and cur_t.phase == PHASE_TRUNK_BELTS
                    and self.bot_agent.plan is not None
                ):
                    self._core_mall_seeded = True
                    seed_actions: list[tuple[int, bytes]] = [clr]
                    pip_plate = build_pipette_item_action(item_map["iron-plate"])
                    for t_task in self.bot_agent.plan.tasks:
                        if t_task.task_kind == TASK_PLACE_ENTITY and t_task.recipe in (
                            "iron-gear-wheel",
                            "transport-belt",
                        ):
                            dist_m = math.hypot(
                                t_task.target_x - state.player_x,
                                t_task.target_y - state.player_y,
                            )
                            if dist_m <= 9.5:
                                seed_actions.extend(
                                    [
                                        build_selected_entity_action(
                                            t_task.target_x, t_task.target_y
                                        ),
                                        pip_plate,
                                        fs_in,
                                        clr,
                                    ]
                                )
                    if len(seed_actions) > 1:
                        self._queue_all(proxy, seed_actions, state.tick)
                        return self.stage

                self.bot_agent.step_lockstep_proxy(
                    proxy=proxy,
                    world_state=state,
                    id_mappings=self.id_mappings,
                    player_index=self.player_index,
                    stop_tolerance=0.65,
                )
            else:
                if (
                    proxy.pending_action_count() > 0
                    or (state.tick - self.bot_agent._last_action_tick) < 180
                ):
                    return self.stage
                if self.bot_agent.plan is not None and len(state.placed_entities) > 0:
                    by_pos = {
                        (round(ent.x, COORD_ROUND_DIGITS), round(ent.y, COORD_ROUND_DIGITS)): ent
                        for ent in state.placed_entities
                    }
                    requeued = False
                    for t_task in self.bot_agent.plan.tasks:
                        key = (
                            round(t_task.target_x, COORD_ROUND_DIGITS),
                            round(t_task.target_y, COORD_ROUND_DIGITS),
                        )
                        actual = by_pos.get(key)
                        if t_task.task_kind == TASK_PLACE_ENTITY:
                            if actual is None or actual.name != t_task.target_name:
                                self.bot_agent.completed_task_indices.discard(t_task.task_index)
                                requeued = True
                        elif (
                            t_task.task_kind == TASK_CONFIGURE_RECIPE
                            and actual is not None
                            and t_task.recipe is not None
                            and actual.recipe != t_task.recipe
                        ):
                            self.bot_agent.completed_task_indices.discard(t_task.task_index)
                            requeued = True
                    if requeued:
                        return self.stage
                self.final_science_load_queued = True
                if "steel-processing" in tech_map:
                    self._queue_all(
                        proxy,
                        [build_start_research_action(tech_map["steel-processing"])],
                        state.tick,
                    )
                self.stage = "completed"
            return self.stage

        return self.stage
