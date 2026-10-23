"""Autonomous bot agent that synthesizes and builds the starter base from cartography.

Reads surface telemetry through MapCartographer, synthesizes the patch-to-base
starter layout and bootstrap mall, computes the obstacle pre-clearing schedule,
orders entity construction by power and material dependencies, and emits
Factorio InputAction sequences within character reach limits.
All docstrings and comments follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foundry import solve
from foundry.campaign.cartographer import MapCartographer, TerrainObstacle
from foundry.entity_helpers import DIRECTION_NORTH, entity_size, register_prototype_boxes
from foundry.mp_proxy import (
    ACTION_ROTATE_ENTITY,
    FactorioLockstepProxy,
    PlayerWorldState,
    build_begin_mining_action,
    build_clear_cursor_action,
    build_fast_entity_transfer_action,
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

TILE_CENTER_OFFSET: float = 0.5
COORD_ROUND_DIGITS: int = 2
REACH_SAFETY_MARGIN: float = 0.65
SEARCH_RADIUS_PADDING: int = 2
DEFAULT_SEED_FUEL_COUNT: int = 50
DEFAULT_STOP_TOLERANCE: float = 0.75
DIAGONAL_AXIS_RATIO: float = 0.3826834323650898
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

    # Phase 1: Shoreline steam power array
    power_ents = sorted(layout.power_array.entities, key=_power_entity_sort_key)
    for ent in power_ents:
        _schedule_entity(ent, PHASE_SHORELINE_POWER)

    # Phase 2: Resource patch miner arrays (fuel patch first so coal flows as soon as power connects)
    patch_order = sorted(
        layout.patch_arrays.keys(),
        key=lambda comm: (0 if comm == fuel_commodity else 1, comm),
    )
    for comm in patch_order:
        p_ents = sorted(layout.patch_arrays[comm].entities, key=_patch_entity_sort_key)
        for ent in p_ents:
            _schedule_entity(ent, PHASE_PATCH_MINERS)

    # Phase 3: Power distribution network (poles ordered outward from the steam engine)
    engine_anchor = cur_pos
    for ent in layout.power_array.entities:
        if str(ent["name"]) == "steam-engine":
            engine_anchor = _entity_pos_key(ent)
            break

    pole_ents = [
        ent
        for ent in layout.entities
        if _entity_pos_key(ent) not in scheduled_positions and "pole" in str(ent["name"])
    ]
    pole_ents.sort(
        key=lambda e: (
            math.hypot(
                float(e["position"]["x"]) - engine_anchor[0],
                float(e["position"]["y"]) - engine_anchor[1],
            ),
            float(e["position"]["x"]),
            float(e["position"]["y"]),
        )
    )
    for ent in pole_ents:
        _schedule_entity(ent, PHASE_POWER_NETWORK)

    # Phase 4: Trunk belts (boiler fuel trunk first, then base ore trunks)
    trunk_order = sorted(
        layout.trunk_routes.keys(),
        key=lambda net_name: (0 if "boiler" in net_name else 1, net_name),
    )
    for net_name in trunk_order:
        route_res = layout.trunk_routes[net_name]
        route_ents: list[Mapping[str, Any]] = []
        for node in route_res.nodes:
            n_key = (
                round(node.x + TILE_CENTER_OFFSET, COORD_ROUND_DIGITS),
                round(node.y + TILE_CENTER_OFFSET, COORD_ROUND_DIGITS),
            )
            if n_key in entities_by_pos:
                route_ents.append(entities_by_pos[n_key])
        for ent in sorted(route_ents, key=_trunk_entity_sort_key):
            _schedule_entity(ent, PHASE_TRUNK_BELTS)

    # Phase 5: Starter base core (furnaces, assemblers + recipes, lab, belts, inserters)
    core_ents = sorted(layout.base_core.entities, key=_base_core_entity_sort_key)
    for ent in core_ents:
        _schedule_entity(ent, PHASE_STARTER_BASE_CORE)

    # Schedule any remaining unscheduled entities from layout.entities
    for ent in layout.entities:
        if _entity_pos_key(ent) not in scheduled_positions:
            _schedule_entity(ent, PHASE_STARTER_BASE_CORE)

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
        ]
        if task.entity_type == "output":
            actions.append(build_selected_entity_action(task.target_x, task.target_y))
            actions.append((ACTION_ROTATE_ENTITY, struct.pack("<B", 0)))
        actions.append(build_clear_cursor_action())
        return actions

    if task.task_kind == TASK_CONFIGURE_RECIPE:
        if task.recipe is None or task.recipe not in recipe_map:
            raise KeyError(f"Recipe '{task.recipe}' not found in id_mappings['recipe']")
        recipe_id = int(recipe_map[task.recipe])
        return [
            build_selected_entity_action(task.target_x, task.target_y),
            build_setup_assembling_machine_action(recipe_id),
        ]

    if task.task_kind == TASK_SEED_FUEL:
        if task.target_name not in item_map:
            raise KeyError(f"Fuel item '{task.target_name}' not found in id_mappings['item']")
        fuel_id = int(item_map[task.target_name])
        return [
            build_pipette_item_action(fuel_id),
            build_selected_entity_action(task.target_x, task.target_y),
            build_fast_entity_transfer_action(into_entity=True),
            build_clear_cursor_action(),
        ]

    raise ValueError(f"Unsupported task_kind: {task.task_kind!r}")


def compute_8way_walk_vector(
    player_x: float,
    player_y: float,
    target_x: float,
    target_y: float,
    stop_tolerance: float = DEFAULT_STOP_TOLERANCE,
) -> tuple[float, float] | None:
    """Compute an 8-direction walking vector toward (target_x, target_y), or None if within tolerance."""
    dx = target_x - player_x
    dy = target_y - player_y
    dist = math.hypot(dx, dy)
    if dist <= stop_tolerance:
        return None
    ux = dx / dist
    uy = dy / dist
    sx = 0.0 if abs(ux) < DIAGONAL_AXIS_RATIO else (1.0 if ux > 0.0 else -1.0)
    sy = 0.0 if abs(uy) < DIAGONAL_AXIS_RATIO else (1.0 if uy > 0.0 else -1.0)
    return (sx, sy)


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

        if proxy.pending_action_count() > 0:
            return "waiting_for_queue"

        walk_vec = compute_8way_walk_vector(
            world_state.player_x,
            world_state.player_y,
            task.stand_x,
            task.stand_y,
            stop_tolerance=stop_tolerance,
        )
        if walk_vec is not None:
            act_type, payload = build_start_walking_action(walk_vec[0], walk_vec[1])
            proxy.queue_action(act_type, player_index, payload)
            return f"walking_to_task_{task.task_index}"

        if world_state.walking:
            act_type, payload = build_stop_walking_action()
            proxy.queue_action(act_type, player_index, payload)

        if task.task_kind == TASK_CLEAR_OBSTACLE:
            if self._active_mining_task != task.task_index:
                for act_type, payload in encode_task_input_actions(task, id_mappings):
                    proxy.queue_action(act_type, player_index, payload)
                self._active_mining_task = task.task_index
                return f"mining_obstacle_{task.task_index}"
            if not world_state.mining:
                stop_type, stop_payload = build_stop_mining_action()
                proxy.queue_action(stop_type, player_index, stop_payload)
                self.mark_task_complete(task.task_index)
                return f"cleared_obstacle_{task.task_index}"
            return f"mining_obstacle_{task.task_index}"

        for act_type, payload in encode_task_input_actions(task, id_mappings):
            proxy.queue_action(act_type, player_index, payload)
        self.mark_task_complete(task.task_index)
        return f"executed_task_{task.task_index}"

    def build_lua_plan_executor_snippet(self) -> str:
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
                lines.append(
                    f"  if ent then ent.insert({{name = '{task.target_name}', count = {task.item_count}}}) seeded_count = seeded_count + 1 end"
                )

        lines.append("  storage.bot_execution = {")
        lines.append("    cleared_count = cleared_count,")
        lines.append("    placed_count = placed_count,")
        lines.append("    configured_count = configured_count,")
        lines.append("    seeded_count = seeded_count,")
        lines.append("  }")
        return "\n".join(lines) + "\n"

    def build_lua_progressive_plan_executor_snippet(
        self,
        step_stride: float = 0.15,
    ) -> str:
        """Generate a Lua snippet that walks the bot and executes tasks progressively across ticks."""
        if self.plan is None:
            raise RuntimeError("Cannot generate progressive Lua snippet before synthesize_plan()")

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
            "      task_idx = 1,\n"
            "      wait_left = nil,\n"
            "      completed = false,\n"
            "    }\n"
            "  end\n"
            "  local exec = storage.bot_execution\n"
            "  if exec.completed then return end\n"
            "  local char_ent = surface.find_entities_filtered({name = 'character'})[1]\n"
            "  local idx = exec.task_idx\n"
            "  if idx > #BOT_TASKS then\n"
            "    if char_ent and char_ent.valid then\n"
            "      local dx = 31.5 - char_ent.position.x\n"
            "      local dy = 22.5 - char_ent.position.y\n"
            "      local dist = math.sqrt(dx * dx + dy * dy)\n"
            f"      if dist > {stride_val} then\n"
            f"        char_ent.direction = vec_to_dir(dx, dy)\n"
            f"        char_ent.teleport({{char_ent.position.x + (dx / dist) * {stride_val}, char_ent.position.y + (dy / dist) * {stride_val}}})\n"
            "        return\n"
            "      else\n"
            "        char_ent.teleport({31.5, 22.5})\n"
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
            "    local params = {name = t.name, position = {t.tx, t.ty}, direction = t.dir, force = force, raise_built = true}\n"
            "    if t.ug then params.type = t.ug end\n"
            "    if t.rec then params.recipe = t.rec end\n"
            "    local ent = surface.create_entity(params)\n"
            "    if ent then exec.placed_count = exec.placed_count + 1 end\n"
            "  elseif t.kind == 'configure_recipe' and t.rec then\n"
            "    local ent = surface.find_entity(t.name, {t.tx, t.ty})\n"
            "    if ent then ent.set_recipe(t.rec) exec.configured_count = exec.configured_count + 1 end\n"
            "  elseif t.kind == 'seed_fuel' then\n"
            "    local ent = surface.find_entity('boiler', {t.tx, t.ty})\n"
            "    if ent then ent.insert({name = t.name, count = t.cnt}) exec.seeded_count = exec.seeded_count + 1 end\n"
            "  end\n"
            "  exec.task_idx = idx + 1\n"
            "end\n"
        )
