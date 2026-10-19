"""Helper functions for entity geometry, power, collisions, and belt pruning.

Provides bounding box calculation, collision checks, electric pole checks,
and dead-end belt removal.
"""

from __future__ import annotations

import math
from typing import Any

EntityDict = dict[str, Any]
EntityList = list[EntityDict]
ProtoDict = dict[str, Any]

# Factorio 2.0 direction values: North=0, East=4, South=8, West=12.
DIRECTION_NORTH = 0
DIRECTION_EAST = 4
DIRECTION_SOUTH = 8
DIRECTION_WEST = 12
EAST_WEST_DIRS = (DIRECTION_EAST, DIRECTION_WEST)
NORTH_SOUTH_DIRS = (DIRECTION_NORTH, DIRECTION_SOUTH)

DEFAULT_UNDERGROUND_SPAN = 5
DEFAULT_PIPE_UNDERGROUND_SPAN = 10
DEFAULT_POLE_SUPPLY_RADIUS = 3.5
DEFAULT_POLE_WIRE_REACH = 9.0
DEFAULT_COLLISION_HALF = 0.4
DEFAULT_COLLISION_BOX = (
    (-DEFAULT_COLLISION_HALF, -DEFAULT_COLLISION_HALF),
    (DEFAULT_COLLISION_HALF, DEFAULT_COLLISION_HALF),
)
MIN_COLLISION_BOX_CORNERS = 2
POLE_BUCKET_SIZE = 16
TILE_HALF = 0.5
DEFAULT_DEF_UNITS = 1000.0
DEFAULT_CELL_DIM = 3.0
MERGE_CELL_DIM = 3
LONG_INSERTER_REACH = 2
STANDARD_INSERTER_REACH = 1

PUMP_NORTH_SIZE: tuple[float, float] = (1.0, 2.0)
DEFAULT_ENTITY_SIZE: tuple[float, float] = (1.0, 1.0)
FALLBACK_SIZES_NORTH: tuple[tuple[tuple[str, ...], tuple[float, float]], ...] = (
    (("rocket-silo",), (9.0, 9.0)),
    (("oil-refinery", "nuclear-reactor"), (5.0, 5.0)),
    (("steam-engine", "steam-turbine"), (3.0, 5.0)),
    (("boiler", "heat-exchanger"), (3.0, 2.0)),
    (
        (
            "assembling-machine",
            "electric-furnace",
            "chemical-plant",
            "centrifuge",
            "lab",
            "radar",
        ),
        (3.0, 3.0),
    ),
    (
        (
            "steel-furnace",
            "stone-furnace",
            "power-switch",
            "big-electric-pole",
            "substation",
        ),
        (2.0, 2.0),
    ),
    (("pump",), PUMP_NORTH_SIZE),
)

PUMP_NORTH_HALF_SIZE: tuple[float, float] = (0.29, 0.9)
DEFAULT_HALF_SIZE: tuple[float, float] = (0.15, 0.15)
FALLBACK_HALF_SIZES_NORTH: tuple[tuple[tuple[str, ...], tuple[float, float]], ...] = (
    (("rocket-silo",), (4.2, 4.2)),
    (("oil-refinery",), (2.2, 2.2)),
    (("steam-engine", "steam-turbine"), (1.25, 2.35)),
    (("boiler", "heat-exchanger"), (1.29, 0.79)),
    (
        (
            "assembling-machine",
            "electric-furnace",
            "chemical-plant",
            "centrifuge",
            "lab",
            "radar",
        ),
        (1.2, 1.2),
    ),
    (
        (
            "furnace",
            "power-switch",
            "big-electric-pole",
            "substation",
        ),
        (0.7, 0.7),
    ),
    (("pump",), PUMP_NORTH_HALF_SIZE),
)

DIR_STEPS: dict[int, tuple[int, int]] = {
    DIRECTION_EAST: (1, 0),
    DIRECTION_WEST: (-1, 0),
    DIRECTION_SOUTH: (0, 1),
    DIRECTION_NORTH: (0, -1),
}

OPPOSITE_DIR: dict[int, int] = {
    DIRECTION_NORTH: DIRECTION_SOUTH,
    DIRECTION_SOUTH: DIRECTION_NORTH,
    DIRECTION_EAST: DIRECTION_WEST,
    DIRECTION_WEST: DIRECTION_EAST,
}

INSERTER_OFFSETS: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {
    DIRECTION_NORTH: ((0, -1), (0, 1)),
    DIRECTION_EAST: ((1, 0), (-1, 0)),
    DIRECTION_SOUTH: ((0, 1), (0, -1)),
    DIRECTION_WEST: ((-1, 0), (1, 0)),
}


def _segment_direction(x1: int, y1: int, x2: int, y2: int) -> int:
    """Determine Factorio 2.0 cardinal direction from endpoint coordinates."""
    if x1 == x2:
        return DIRECTION_SOUTH if y2 >= y1 else DIRECTION_NORTH
    return DIRECTION_EAST if x2 >= x1 else DIRECTION_WEST


_PROTOTYPE_BOXES: dict[str, tuple[float, float]] = {}
_PROTOTYPE_COLLISIONS: dict[str, tuple[float, float]] = {}
_PROTOTYPE_SPANS: dict[str, int] = {
    "underground-belt": 5,
    "fast-underground-belt": 7,
    "express-underground-belt": 9,
    "pipe-to-ground": 10,
}
_PROTOTYPE_POLE_SUPPLY: dict[str, float] = {
    "small-electric-pole": 2.5,
    "medium-electric-pole": 3.5,
    "big-electric-pole": 2.0,
    "substation": 9.0,
}
_PROTOTYPE_POLE_WIRE: dict[str, float] = {
    "small-electric-pole": 7.5,
    "medium-electric-pole": 9.0,
    "big-electric-pole": 32.0,
    "substation": 18.0,
}


def register_underground_span(name: str, span: int) -> None:
    """Register or override maximum underground span for named entity."""
    _PROTOTYPE_SPANS[name] = span


def get_underground_span(name: str, default: int = DEFAULT_UNDERGROUND_SPAN) -> int:
    """Return maximum underground span for named entity from prototype data."""
    return _PROTOTYPE_SPANS.get(name, default)


def get_pole_supply_radius(name: str, default: float = DEFAULT_POLE_SUPPLY_RADIUS) -> float:
    """Return electric supply area distance for named pole from prototype data."""
    return _PROTOTYPE_POLE_SUPPLY.get(name, default)


def get_pole_wire_reach(name: str, default: float = DEFAULT_POLE_WIRE_REACH) -> float:
    """Return maximum wire reach distance for named entity from prototype data."""
    return _PROTOTYPE_POLE_WIRE.get(name, default)


def is_electric_pole(name: str) -> bool:
    """Return True if entity name is an electric pole or substation."""
    return "electric-pole" in name or "substation" in name or name in _PROTOTYPE_POLE_SUPPLY


def is_electric_consumer(name: str) -> bool:
    """Return True if entity name consumes electric power."""
    if not name or name in ("burner-inserter", "constant-combinator", "display-panel"):
        return False
    return any(
        k in name
        for k in (
            "assembling-machine",
            "electric-furnace",
            "chemical-plant",
            "oil-refinery",
            "centrifuge",
            "rocket-silo",
            "electric-mining-drill",
            "pumpjack",
            "inserter",
            "pump",
            "combinator",
            "lab",
            "radar",
            "lamp",
            "steam-engine",
        )
    )


def get_compatible_underground_belt(min_span: int, preferred_name: str | None = None) -> str:
    """Find an underground belt entity meeting the minimum required span.

    If preferred_name meets min_span, returns preferred_name.
    Otherwise returns the registered underground belt entity with the smallest span >= min_span.
    """
    if preferred_name and get_underground_span(preferred_name) >= min_span:
        return preferred_name

    candidates = [
        (name, span)
        for name, span in _PROTOTYPE_SPANS.items()
        if "pipe" not in name and span >= min_span
    ]
    if candidates:
        candidates.sort(key=lambda item: item[1])
        return candidates[0][0]

    return preferred_name or "fast-underground-belt"


def _extract_proto_span(proto: ProtoDict) -> int | None:
    if "max_distance" in proto:
        return int(proto["max_distance"])
    fluid_box = proto.get("fluid_box")
    if isinstance(fluid_box, dict):
        for conn in fluid_box.get("pipe_connections", []):
            if conn.get("connection_type") == "underground" and "max_underground_distance" in conn:
                return int(conn["max_underground_distance"])
    return None


def entity_bounding_box(proto: ProtoDict) -> tuple[float, float]:
    """Derive entity width and height from prototype collision box."""
    box = proto.get("collision_box", DEFAULT_COLLISION_BOX)
    w = math.ceil(float(box[1][0]) - float(box[0][0]))
    h = math.ceil(float(box[1][1]) - float(box[0][1]))
    return float(max(1, w)), float(max(1, h))


def _register_single_prototype(key: str, proto: ProtoDict) -> None:
    """Register dimensions, underground span, and pole distances for one prototype."""
    box = proto.get("collision_box")
    if isinstance(box, list) and len(box) >= MIN_COLLISION_BOX_CORNERS:
        _PROTOTYPE_BOXES[key] = entity_bounding_box(proto)
        _PROTOTYPE_COLLISIONS[key] = (
            abs(float(box[1][0])),
            abs(float(box[1][1])),
        )
    span = _extract_proto_span(proto)
    if span is not None:
        _PROTOTYPE_SPANS[key] = span
    if "supply_area_distance" in proto:
        _PROTOTYPE_POLE_SUPPLY[key] = float(proto["supply_area_distance"])
    if "maximum_wire_distance" in proto:
        _PROTOTYPE_POLE_WIRE[key] = float(proto["maximum_wire_distance"])


def register_prototype_boxes(prototypes: ProtoDict) -> None:
    """Register prototype dimensions, spans, and pole parameters from prototype data."""
    for key, value in prototypes.items():
        if not isinstance(value, dict):
            continue
        _register_single_prototype(key, value)
        for sub_key, sub_val in value.items():
            if isinstance(sub_val, dict):
                _register_single_prototype(sub_key, sub_val)


def _orient_dims(dims: tuple[float, float], direction: int) -> tuple[float, float]:
    """Orient (width, height) pair for given cardinal direction."""
    if direction in EAST_WEST_DIRS:
        return dims[1], dims[0]
    return dims


def _lookup_dims(
    name: str,
    direction: int,
    exact_pump: tuple[float, float],
    registry: dict[str, tuple[float, float]],
    fallbacks: tuple[tuple[tuple[str, ...], tuple[float, float]], ...],
    default: tuple[float, float],
) -> tuple[float, float]:
    """Look up oriented dimensions from prototype registry or fallback rules."""
    if name == "pump":
        return _orient_dims(exact_pump, direction)
    if name in registry:
        return _orient_dims(registry[name], direction)
    for keywords, dims in fallbacks:
        if any(k in name for k in keywords):
            return _orient_dims(dims, direction)
    return default


def entity_size(name: str, direction: int = DIRECTION_NORTH) -> tuple[float, float]:
    """Return width and height of named entity."""
    return _lookup_dims(
        name,
        direction,
        PUMP_NORTH_SIZE,
        _PROTOTYPE_BOXES,
        FALLBACK_SIZES_NORTH,
        DEFAULT_ENTITY_SIZE,
    )


def entity_collision_half_size(name: str, direction: int = DIRECTION_NORTH) -> tuple[float, float]:
    """Return half width and half height of entity collision box."""
    return _lookup_dims(
        name,
        direction,
        PUMP_NORTH_HALF_SIZE,
        _PROTOTYPE_COLLISIONS,
        FALLBACK_HALF_SIZES_NORTH,
        DEFAULT_HALF_SIZE,
    )


def _is_consumer_powered(
    consumer: EntityDict,
    pole_buckets: dict[tuple[int, int], list[tuple[float, float, float]]],
    max_pole_radius: float,
) -> bool:
    """Return True if consumer entity is within supply range of any bucketed pole."""
    ex = float(consumer["position"]["x"])
    ey = float(consumer["position"]["y"])
    cx, cy = entity_collision_half_size(
        consumer.get("name", ""), consumer.get("direction", DIRECTION_NORTH)
    )
    search_r = max_pole_radius + max(cx, cy)
    min_bx = int((ex - search_r) // POLE_BUCKET_SIZE)
    max_bx = int((ex + search_r) // POLE_BUCKET_SIZE)
    min_by = int((ey - search_r) // POLE_BUCKET_SIZE)
    max_by = int((ey + search_r) // POLE_BUCKET_SIZE)
    buckets = [(bx, by) for bx in range(min_bx, max_bx + 1) for by in range(min_by, max_by + 1)]
    for bucket_key in buckets:
        for px, py, p_radius in pole_buckets.get(bucket_key, ()):
            dx = max(0.0, abs(ex - px) - cx)
            dy = max(0.0, abs(ey - py) - cy)
            if max(dx, dy) <= p_radius:
                return True
    return False


def check_power(entities: EntityList, supply_radius: float | None = None) -> EntityList:
    """Return list of powered entities lacking electric pole supply coverage."""
    poles = [e for e in entities if is_electric_pole(e.get("name", ""))]
    powered = [e for e in entities if is_electric_consumer(e.get("name", ""))]
    if not poles:
        return powered
    pole_buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    max_pole_radius = 0.0
    for p in poles:
        px = float(p["position"]["x"])
        py = float(p["position"]["y"])
        p_radius = (
            supply_radius
            if supply_radius is not None
            else get_pole_supply_radius(p.get("name", ""))
        )
        max_pole_radius = max(max_pole_radius, p_radius)
        bucket = (int(px // POLE_BUCKET_SIZE), int(py // POLE_BUCKET_SIZE))
        pole_buckets.setdefault(bucket, []).append((px, py, p_radius))

    return [e for e in powered if not _is_consumer_powered(e, pole_buckets, max_pole_radius)]


def _entity_occupied_tiles(ent: EntityDict) -> list[tuple[float, float]]:
    """Return list of 1x1 tile centers occupied by an entity for collision checks."""
    epos = ent["position"]
    x = float(epos["x"])
    y = float(epos["y"])
    ename = ent.get("name", "")
    d = ent.get("direction", DIRECTION_NORTH)
    if "splitter" in ename:
        spans_x = d in NORTH_SOUTH_DIRS
    elif ename == "pump":
        spans_x = d not in NORTH_SOUTH_DIRS
    else:
        return [(round(x, 2), round(y, 2))]

    if spans_x:
        return [
            (round(x - TILE_HALF, 2), round(y, 2)),
            (round(x + TILE_HALF, 2), round(y, 2)),
        ]
    return [
        (round(x, 2), round(y - TILE_HALF, 2)),
        (round(x, 2), round(y + TILE_HALF, 2)),
    ]


def check_collisions(
    entities: EntityList,
) -> list[tuple[EntityDict, EntityDict]]:
    """Check for surface entity collisions. Return list of colliding entity pairs."""
    occupied: dict[tuple[float, float], EntityDict] = {}
    collisions: list[tuple[EntityDict, EntityDict]] = []
    for ent in entities:
        for t in _entity_occupied_tiles(ent):
            if t in occupied:
                collisions.append((occupied[t], ent))
            else:
                occupied[t] = ent
    return collisions


def _find_unpaired_underground_belts(entities: EntityList, max_span: int | None) -> EntityList:
    """Return underground belt inputs and outputs that lack a valid partner."""
    ugs = [e for e in entities if "underground-belt" in e.get("name", "")]
    inputs = [e for e in ugs if e.get("type") == "input"]
    outputs = [e for e in ugs if e.get("type") == "output"]
    outputs_by_line: dict[tuple[str, int, float], EntityList] = {}
    for o in outputs:
        od = o.get("direction", DIRECTION_NORTH)
        line_coord = o["position"]["y"] if od in EAST_WEST_DIRS else o["position"]["x"]
        outputs_by_line.setdefault((o.get("name", ""), od, line_coord), []).append(o)

    matched_outputs: set[int] = set()
    unpaired: EntityList = []
    for inp in inputs:
        ix, iy = inp["position"]["x"], inp["position"]["y"]
        d = inp.get("direction", DIRECTION_NORTH)
        dx, dy = DIR_STEPS.get(d, (0, 0))
        tier_span = (
            max_span
            if max_span is not None
            else get_underground_span(inp.get("name", ""), default=DEFAULT_UNDERGROUND_SPAN)
        )
        line_coord = iy if d in EAST_WEST_DIRS else ix
        candidates = outputs_by_line.get((inp.get("name", ""), d, line_coord), ())
        found = next(
            (
                o
                for o in candidates
                if id(o) not in matched_outputs
                and 1
                <= (o["position"]["x"] - ix) * dx + (o["position"]["y"] - iy) * dy
                <= tier_span
            ),
            None,
        )
        if found is not None:
            matched_outputs.add(id(found))
        else:
            unpaired.append(inp)
    unpaired.extend(o for o in outputs if id(o) not in matched_outputs)
    return unpaired


def _find_unpaired_pipes_to_ground(entities: EntityList) -> EntityList:
    """Return pipe-to-ground entities that lack an opposing underground partner."""
    pipes = [e for e in entities if e.get("name") == "pipe-to-ground"]
    pipes_by_pos_dir: dict[tuple[float, float, int], EntityList] = {}
    for other in pipes:
        ox = round(other["position"]["x"], 2)
        oy = round(other["position"]["y"], 2)
        od = other.get("direction", DIRECTION_NORTH)
        pipes_by_pos_dir.setdefault((ox, oy, od), []).append(other)

    matched_pipes: set[int] = set()
    unpaired: EntityList = []
    max_pipe_span = get_underground_span("pipe-to-ground", DEFAULT_PIPE_UNDERGROUND_SPAN)
    for p in pipes:
        if id(p) in matched_pipes:
            continue
        px, py = p["position"]["x"], p["position"]["y"]
        pd = p.get("direction", DIRECTION_NORTH)
        ug_d = OPPOSITE_DIR.get(pd, pd)
        dx, dy = DIR_STEPS.get(ug_d, (0, 0))
        candidates = [
            other
            for s in range(1, max_pipe_span + 1)
            for other in pipes_by_pos_dir.get(
                (round(px + dx * s, 2), round(py + dy * s, 2), ug_d), ()
            )
            if id(other) not in matched_pipes and id(other) != id(p)
        ]
        if candidates:
            matched_pipes.add(id(p))
            matched_pipes.add(id(candidates[0]))
        else:
            unpaired.append(p)
    return unpaired


def check_underground_pairing(entities: EntityList, max_span: int | None = None) -> EntityList:
    """Verify that all underground belt and pipe-to-ground entities pair."""
    unpaired = _find_unpaired_underground_belts(entities, max_span)
    unpaired.extend(_find_unpaired_pipes_to_ground(entities))
    return unpaired


def _collect_splitter_and_ug_targets(
    entities: EntityList,
) -> tuple[set[tuple[float, float, int]], set[tuple[float, float, int]]]:
    """Collect (x, y, direction) entry targets for splitters and underground belt inputs."""
    splitter_targets: set[tuple[float, float, int]] = set()
    ug_targets: set[tuple[float, float, int]] = set()
    for e in entities:
        ename = e.get("name", "")
        pos = e["position"]
        ex, ey = float(pos["x"]), float(pos["y"])
        d = e.get("direction", DIRECTION_NORTH)
        if ename == "splitter":
            for tx, ty in _entity_occupied_tiles(e):
                splitter_targets.add((round(tx, 1), round(ty, 1), d))
        elif "underground-belt" in ename and e.get("type") == "input":
            ug_targets.add((round(ex, 1), round(ey, 1), d))
    return splitter_targets, ug_targets


def _port_slot_offset(
    ports: list[str], slots: dict[str, int], fluids: set[str], height: float
) -> list[float]:
    """Return vertical center offsets for non-fluid macro ports."""
    count = len(ports)
    base = (height - count) // 2 if count <= height else 0.0
    return [
        float(slots.get(port, base + idx)) + TILE_HALF
        for idx, port in enumerate(sorted(ports))
        if port not in fluids
    ]


def _collect_macro_protected_tiles(
    def_data: ProtoDict,
    netlist_data: ProtoDict,
    scale: float,
) -> set[tuple[float, float]]:
    """Collect belt tile coordinates protected by macro input and output ports."""
    try:
        from foundry.lef import get_macro_port_slots
    except ImportError:
        from lef import get_macro_port_slots

    fluids = set(netlist_data.get("fluids") or [])
    instances = {i["name"]: i for i in netlist_data.get("instances", [])}
    cells = netlist_data.get("cells", {})
    protected: set[tuple[float, float]] = set()

    for comp in def_data.get("components", []):
        gx, gy = comp["x"] * scale, comp["y"] * scale
        cell_id = instances.get(comp["name"], {}).get("cell", comp["macro"])
        if cell_id == "belt_merge2" or comp.get("macro") == "belt_merge2":
            protected.update(
                (round(gx + tx + TILE_HALF, 1), round(gy + ty + TILE_HALF, 1))
                for tx in range(MERGE_CELL_DIM)
                for ty in range(MERGE_CELL_DIM)
            )
            continue
        cell = cells.get(cell_id, {})
        if cell_id in ("belt_split2", "belt_balancer2") or cell.get("circuit_cell"):
            continue
        w = float(cell.get("width", DEFAULT_CELL_DIM))
        h = float(cell.get("height", DEFAULT_CELL_DIM))
        in_slots, out_slots = get_macro_port_slots(cell)
        protected.update(
            (round(gx - TILE_HALF, 1), round(gy + cy, 1))
            for cy in _port_slot_offset(cell.get("inputs", []), in_slots, fluids, h)
        )
        protected.update(
            (round(gx + w + TILE_HALF, 1), round(gy + cy, 1))
            for cy in _port_slot_offset(cell.get("outputs", []), out_slots, fluids, h)
        )
    return protected


def _collect_inserter_pickup_tiles(entities: EntityList) -> set[tuple[float, float]]:
    """Collect belt tile coordinates where placed inserters pick up items."""
    protected: set[tuple[float, float]] = set()
    for e in entities:
        ename = e.get("name", "")
        if "inserter" not in ename:
            continue
        d = e.get("direction", DIRECTION_NORTH)
        mult = LONG_INSERTER_REACH if "long-handed" in ename else STANDARD_INSERTER_REACH
        p_dx, p_dy = INSERTER_OFFSETS.get(d, ((-1, 0), (1, 0)))[0]
        pos = e["position"]
        protected.add(
            (round(float(pos["x"]) + p_dx * mult, 1), round(float(pos["y"]) + p_dy * mult, 1))
        )
    return protected


def _is_belt_kept(
    pos: tuple[float, float],
    ent: EntityDict,
    belt_map: dict[tuple[float, float], EntityDict],
    protected_tiles: set[tuple[float, float]],
    entry_targets: set[tuple[float, float, int]],
) -> bool:
    """Return True if belt tile at pos feeds a valid downstream consumer or belt."""
    if pos in protected_tiles:
        return True
    d = ent.get("direction", DIRECTION_NORTH)
    if d not in DIR_STEPS:
        return False
    dx, dy = DIR_STEPS[d]
    target = (round(pos[0] + dx, 1), round(pos[1] + dy, 1))
    if (target[0], target[1], d) in entry_targets:
        return True
    return (
        target in belt_map and belt_map[target].get("direction", DIRECTION_NORTH) != OPPOSITE_DIR[d]
    )


def prune_dead_end_belts(
    entities: EntityList,
    def_data: ProtoDict,
    netlist_data: ProtoDict,
) -> EntityList:
    """Prune dead-end stub belts that do not reach any downstream sink."""
    scale = 1.0 / float(def_data.get("units", DEFAULT_DEF_UNITS))
    output_pins = {
        (
            round(math.floor(pin["x"] * scale) + TILE_HALF, 1),
            round(math.floor(pin["y"] * scale) + TILE_HALF, 1),
        )
        for pin in def_data.get("pins", [])
        if pin.get("direction") == "OUTPUT"
    }
    splitter_targets, ug_targets = _collect_splitter_and_ug_targets(entities)
    protected_inserter_tiles = _collect_macro_protected_tiles(
        def_data, netlist_data, scale
    ) | _collect_inserter_pickup_tiles(entities)
    if not (output_pins or protected_inserter_tiles or splitter_targets or ug_targets):
        return entities

    protected_tiles = output_pins | protected_inserter_tiles
    entry_targets = splitter_targets | ug_targets
    belt_map: dict[tuple[float, float], EntityDict] = {
        (round(e["position"]["x"], 1), round(e["position"]["y"], 1)): e
        for e in entities
        if "transport-belt" in e.get("name", "") and "underground" not in e.get("name", "")
    }

    while True:
        to_remove = [
            pos
            for pos, ent in belt_map.items()
            if not _is_belt_kept(pos, ent, belt_map, protected_tiles, entry_targets)
        ]
        if not to_remove:
            break
        for pos in to_remove:
            del belt_map[pos]

    clean_entities = [
        e
        for e in entities
        if not ("transport-belt" in e.get("name", "") and "underground" not in e.get("name", ""))
    ]
    clean_entities.extend(belt_map.values())
    return clean_entities
