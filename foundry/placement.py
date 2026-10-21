"""Component and inserter placement from DEF and netlist data.

Places standard cells, splitters, pumps, combinators, machines, and inserters
on the Factorio tile grid.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

try:
    from foundry.entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )
    from foundry.lef import get_macro_port_geometry
except ImportError:
    from entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )
    from lef import get_macro_port_geometry

JsonMap: TypeAlias = Mapping[str, object]
EntityDict: TypeAlias = dict[str, Any]
GridPos: TypeAlias = tuple[float, float]

TILE_HALF: float = 0.5
TILE_ONE_HALF: float = 1.5
TILE_TWO_HALF: float = 2.5

NO_RECIPE_MACHINES: frozenset[str] = frozenset({"boiler", "steam-engine", "lab"})
AON_INSERTER_MACHINES: frozenset[str] = frozenset({"boiler", "steam-engine"})

BELT_MERGE2_LAYOUTS: dict[str, tuple[tuple[str, float, float, int], ...]] = {
    "W": (
        ("splitter", 1.0, TILE_TWO_HALF, DIRECTION_NORTH),
        ("transport-belt", TILE_HALF, TILE_ONE_HALF, DIRECTION_NORTH),
        ("transport-belt", TILE_HALF, TILE_HALF, DIRECTION_EAST),
        ("transport-belt", TILE_ONE_HALF, TILE_ONE_HALF, DIRECTION_EAST),
        ("transport-belt", TILE_TWO_HALF, TILE_ONE_HALF, DIRECTION_NORTH),
        ("transport-belt", TILE_TWO_HALF, TILE_HALF, DIRECTION_WEST),
        ("transport-belt", TILE_ONE_HALF, TILE_HALF, DIRECTION_NORTH),
    ),
    "E": (
        ("splitter", 1.0, TILE_HALF, DIRECTION_SOUTH),
        ("transport-belt", TILE_HALF, TILE_ONE_HALF, DIRECTION_SOUTH),
        ("transport-belt", TILE_HALF, TILE_TWO_HALF, DIRECTION_EAST),
        ("transport-belt", TILE_ONE_HALF, TILE_ONE_HALF, DIRECTION_EAST),
        ("transport-belt", TILE_TWO_HALF, TILE_ONE_HALF, DIRECTION_SOUTH),
        ("transport-belt", TILE_TWO_HALF, TILE_TWO_HALF, DIRECTION_WEST),
        ("transport-belt", TILE_ONE_HALF, TILE_TWO_HALF, DIRECTION_SOUTH),
    ),
    "S": (
        ("splitter", TILE_TWO_HALF, 1.0, DIRECTION_WEST),
        ("transport-belt", TILE_ONE_HALF, TILE_HALF, DIRECTION_WEST),
        ("transport-belt", TILE_HALF, TILE_HALF, DIRECTION_SOUTH),
        ("transport-belt", TILE_ONE_HALF, TILE_ONE_HALF, DIRECTION_SOUTH),
        ("transport-belt", TILE_ONE_HALF, TILE_TWO_HALF, DIRECTION_WEST),
        ("transport-belt", TILE_HALF, TILE_TWO_HALF, DIRECTION_NORTH),
        ("transport-belt", TILE_HALF, TILE_ONE_HALF, DIRECTION_WEST),
    ),
    "N": (
        ("splitter", TILE_HALF, 1.0, DIRECTION_EAST),
        ("transport-belt", TILE_ONE_HALF, TILE_HALF, DIRECTION_EAST),
        ("transport-belt", TILE_TWO_HALF, TILE_HALF, DIRECTION_SOUTH),
        ("transport-belt", TILE_ONE_HALF, TILE_ONE_HALF, DIRECTION_SOUTH),
        ("transport-belt", TILE_ONE_HALF, TILE_TWO_HALF, DIRECTION_EAST),
        ("transport-belt", TILE_TWO_HALF, TILE_TWO_HALF, DIRECTION_NORTH),
        ("transport-belt", TILE_TWO_HALF, TILE_ONE_HALF, DIRECTION_EAST),
    ),
}

# (width, height, center_dx, center_dy, direction)
SPLITTER_SPECS: dict[str, tuple[int, int, float, float, int]] = {
    "W": (2, 1, 1.0, TILE_HALF, DIRECTION_NORTH),
    "E": (2, 1, 1.0, TILE_HALF, DIRECTION_SOUTH),
    "S": (1, 2, TILE_HALF, 1.0, DIRECTION_WEST),
    "N": (1, 2, TILE_HALF, 1.0, DIRECTION_EAST),
}

PUMP_SPECS: dict[str, tuple[int, int, float, float, int]] = {
    "W": (2, 1, 1.0, TILE_HALF, DIRECTION_WEST),
    "E": (2, 1, 1.0, TILE_HALF, DIRECTION_EAST),
    "S": (1, 2, TILE_HALF, 1.0, DIRECTION_SOUTH),
    "N": (1, 2, TILE_HALF, 1.0, DIRECTION_NORTH),
}

DEFAULT_DECIDER_CB: dict[str, object] = {
    "decider_conditions": {
        "first_signal": {"type": "virtual", "name": "signal-each"},
        "comparator": ">",
        "constant": 0,
        "output_signal": {"type": "virtual", "name": "signal-each"},
        "copy_count_from_input": True,
    }
}

DEFAULT_SIGNAL_EACH: dict[str, str] = {"type": "virtual", "name": "signal-each"}


@dataclass(frozen=True)
class PlacementConfig:
    """Configuration parameters for component and inserter placement."""

    default_units: float = 1000.0
    merge_size: int = 3
    default_cell_size: int = 3
    default_power_switch_threshold: int = 30
    inserter_rate_cap_ratio: float = 0.35
    inserter_safety_margin: float = 1.40
    default_max_stack: int = 3
    default_inserter_name: str = "fast-inserter"
    default_landing_pad_size: int = 8
    default_landing_pad_grid: int = 2


DEFAULT_PLACEMENT_CONFIG = PlacementConfig()


@dataclass
class RouteResult:
    """Surface routes, conflict tiles, and pin locations."""

    net_surface_tiles: dict[str, dict[GridPos, int]]
    tile_to_nets: dict[GridPos, set[str]]
    adjacent_fluid_conflicts: set[GridPos]
    pin_tiles: set[GridPos]
    valid_input_pins: set[GridPos]
    valid_output_pins: set[GridPos]


@dataclass
class PlacementResult:
    """Entities and occupancy resulting from component placement."""

    entities: list[EntityDict]
    occupied_positions: set[GridPos]
    grid_entities: dict[GridPos, EntityDict]
    inst_to_entity: dict[str, EntityDict]
    fluids: set[str]
    has_fluid_metadata: bool

    def add_entity(
        self,
        name: str,
        x: float,
        y: float,
        direction: int | None = None,
        extra: EntityDict | None = None,
    ) -> EntityDict:
        """Add an entity at tile coordinates (x, y) if unoccupied."""
        pos_key = (round(x, 2), round(y, 2))
        if pos_key in self.occupied_positions:
            return self.grid_entities.get(pos_key, {})
        self.occupied_positions.add(pos_key)
        entity: EntityDict = {
            "name": name,
            "position": {"x": pos_key[0], "y": pos_key[1]},
        }
        if direction is not None:
            entity["direction"] = direction
        if extra:
            entity.update(extra)
        self.entities.append(entity)
        self.grid_entities[pos_key] = entity
        return entity

    def mark_footprint(self, gx: float, gy: float, width: int, height: int) -> None:
        """Mark all tile centers in a width x height box starting at (gx, gy) as occupied."""
        for tx in range(width):
            for ty in range(height):
                self.occupied_positions.add(
                    (round(gx + tx + TILE_HALF, 2), round(gy + ty + TILE_HALF, 2))
                )


def _as_map(val: object) -> JsonMap:
    return val if isinstance(val, Mapping) else {}


def _as_list(val: object) -> list[object]:
    return list(val) if isinstance(val, (list, tuple, set)) else []


def _as_float(val: object, default: float = 0.0) -> float:
    return float(val) if isinstance(val, (int, float, str)) and val != "" else default


def _as_int(val: object, default: int = 0) -> int:
    return int(float(val)) if isinstance(val, (int, float, str)) and val != "" else default


def _as_str(val: object, default: str = "") -> str:
    return str(val) if val is not None and val != "" else default


def _place_belt_merge2(
    res: PlacementResult, orient: str, gx: float, gy: float, _inst: JsonMap, cfg: PlacementConfig
) -> EntityDict:
    layout = BELT_MERGE2_LAYOUTS.get(orient, BELT_MERGE2_LAYOUTS["N"])
    placed = [
        res.add_entity(ename, gx + dx, gy + dy, direction=edirection)
        for ename, dx, dy, edirection in layout
    ]
    res.mark_footprint(gx, gy, cfg.merge_size, cfg.merge_size)
    return placed[0]


def _place_splitter2(
    res: PlacementResult, orient: str, gx: float, gy: float, _inst: JsonMap, _cfg: PlacementConfig
) -> EntityDict:
    cw, ch, dx, dy, edirection = SPLITTER_SPECS.get(orient, SPLITTER_SPECS["N"])
    ent = res.add_entity("splitter", gx + dx, gy + dy, direction=edirection)
    res.mark_footprint(gx, gy, cw, ch)
    return ent


def _place_pump(
    res: PlacementResult, orient: str, gx: float, gy: float, _inst: JsonMap, _cfg: PlacementConfig
) -> EntityDict:
    cw, ch, dx, dy, edirection = PUMP_SPECS.get(orient, PUMP_SPECS["N"])
    ent = res.add_entity("pump", gx + dx, gy + dy, direction=edirection)
    res.mark_footprint(gx, gy, cw, ch)
    return ent


def _place_display_panel(
    res: PlacementResult, _orient: str, gx: float, gy: float, inst: JsonMap, _cfg: PlacementConfig
) -> EntityDict:
    mat = _as_str(inst.get("recipe"))
    extra: EntityDict = {"text": mat}
    if mat:
        extra["custom_icon"] = {"type": "item", "name": mat}
    return res.add_entity(
        "display-panel", gx + TILE_HALF, gy + TILE_HALF, direction=DIRECTION_NORTH, extra=extra
    )


def _place_arithmetic_combinator(
    res: PlacementResult, _orient: str, gx: float, gy: float, inst: JsonMap, _cfg: PlacementConfig
) -> EntityDict:
    recipe = _as_str(inst.get("recipe"))
    sig_type = "fluid" if recipe in res.fluids else "item"
    sig = {"type": sig_type, "name": recipe} if recipe else dict(DEFAULT_SIGNAL_EACH)
    cb = inst.get("control_behavior") or {
        "arithmetic_conditions": {
            "first_signal": sig,
            "operation": "+",
            "second_constant": 0,
            "output_signal": dict(sig),
        }
    }
    ent = res.add_entity(
        "arithmetic-combinator",
        gx + TILE_HALF,
        gy + 1.0,
        direction=DIRECTION_NORTH,
        extra={"control_behavior": cb},
    )
    res.mark_footprint(gx, gy, 1, 2)
    return ent


def _place_decider_combinator(
    res: PlacementResult, _orient: str, gx: float, gy: float, inst: JsonMap, _cfg: PlacementConfig
) -> EntityDict:
    cb = inst.get("control_behavior") or DEFAULT_DECIDER_CB
    ent = res.add_entity(
        "decider-combinator",
        gx + TILE_HALF,
        gy + 1.0,
        direction=DIRECTION_NORTH,
        extra={"control_behavior": cb},
    )
    res.mark_footprint(gx, gy, 1, 2)
    return ent


def _place_constant_combinator(
    res: PlacementResult, _orient: str, gx: float, gy: float, inst: JsonMap, _cfg: PlacementConfig
) -> EntityDict:
    cb = inst.get("control_behavior")
    extra: EntityDict | None = {"control_behavior": cb} if cb else None
    return res.add_entity(
        "constant-combinator",
        gx + TILE_HALF,
        gy + TILE_HALF,
        direction=DIRECTION_NORTH,
        extra=extra,
    )


def _place_power_switch(
    res: PlacementResult, _orient: str, gx: float, gy: float, inst: JsonMap, cfg: PlacementConfig
) -> EntityDict:
    cb = inst.get("control_behavior") or {
        "circuit_enabled": True,
        "circuit_condition": inst.get(
            "circuit_condition",
            {
                "first_signal": {"type": "virtual", "name": "signal-T"},
                "comparator": "<",
                "constant": cfg.default_power_switch_threshold,
            },
        ),
    }
    ent = res.add_entity(
        "power-switch",
        gx + 1.0,
        gy + 1.0,
        direction=DIRECTION_NORTH,
        extra={"control_behavior": cb, "switch_state": False},
    )
    res.mark_footprint(gx, gy, 2, 2)
    return ent


SpecialPlacer: TypeAlias = Callable[
    [PlacementResult, str, float, float, JsonMap, PlacementConfig], EntityDict
]

SPECIAL_MACRO_PLACERS: dict[str, SpecialPlacer] = {
    "belt_merge2": _place_belt_merge2,
    "belt_split2": _place_splitter2,
    "belt_balancer2": _place_splitter2,
    "pump": _place_pump,
    "display_panel": _place_display_panel,
    "arithmetic_combinator": _place_arithmetic_combinator,
    "decider_combinator": _place_decider_combinator,
    "constant_combinator": _place_constant_combinator,
    "power_switch": _place_power_switch,
}


def _collect_tier_candidates(
    tiers: list[object],
    default_name: str,
    is_out: bool,
    cfg: PlacementConfig,
) -> list[tuple[float, str, int, int]]:
    """Build sorted candidate tuples (rate, tier_name, stack_size, max_stack)."""
    rate_key = "out_rates_by_stack" if is_out else "rates_by_stack"
    candidates: list[tuple[float, str, int, int]] = []
    for raw_tier in tiers:
        t_entry = _as_map(raw_tier)
        t_name = _as_str(t_entry.get("name"), default_name)
        max_s = _as_int(t_entry.get("max_stack"), cfg.default_max_stack)
        rate_map = _as_map(t_entry.get(rate_key) or t_entry.get("rates_by_stack"))
        for s_key, s_rate in rate_map.items():
            candidates.append((_as_float(s_rate), t_name, _as_int(s_key), max_s))
    candidates.sort(key=lambda item: (item[0], item[2]))
    return candidates


def _select_port_inserter(
    inst_rec: JsonMap,
    cell_rec: JsonMap,
    port_name: str,
    is_out: bool,
    cfg: PlacementConfig,
) -> tuple[str, EntityDict | None]:
    """Select the lowest-tier inserter and stack size override meeting port throughput."""
    tiers = _as_list(cell_rec.get("inserter_tiers"))
    last_tier = _as_map(tiers[-1]) if tiers else {}
    default_name = _as_str(last_tier.get("name"), cfg.default_inserter_name)
    plan_r = _as_float(inst_rec.get("plan_rate_per_min"))
    rated_r = _as_float(inst_rec.get("rated_per_min"))
    if (
        not tiers
        or plan_r <= 0.0
        or rated_r <= 0.0
        or plan_r >= cfg.inserter_rate_cap_ratio * rated_r
        or bool(cell_rec.get("landing_pad"))
    ):
        return default_name, None

    amt_key = "out_amounts" if is_out else "in_amounts"
    amt_map = _as_map(cell_rec.get(amt_key))
    port_amt = _as_float(amt_map.get(port_name), 1.0)
    req_rate = cfg.inserter_safety_margin * plan_r * port_amt

    for c_rate, c_name, c_stack, c_max_s in _collect_tier_candidates(
        tiers, default_name, is_out, cfg
    ):
        if c_rate >= req_rate:
            ins_extra: EntityDict | None = (
                {"override_stack_size": c_stack} if c_stack < c_max_s else None
            )
            return c_name, ins_extra
    return default_name, None


def _place_port_inserters(
    res: PlacementResult,
    ports: list[str],
    is_output: bool,
    gx: float,
    gy: float,
    inst: JsonMap,
    cell: JsonMap,
    machine: str,
    cfg: PlacementConfig,
) -> None:
    """Place input or output port inserters for a standard machine cell."""
    for port in ports:
        if port in res.fluids:
            continue
        geom = get_macro_port_geometry(dict(cell), port, is_output=is_output)
        ix, iy = geom["inserter_pos"]
        idir = int(geom["inserter_dir"])
        ins_name, ins_extra = _select_port_inserter(inst, cell, port, is_output, cfg)
        if not is_output and machine in AON_INSERTER_MACHINES:
            ins_extra = dict(ins_extra or {})
            ins_extra["power_domain"] = "aon"
        res.add_entity(ins_name, gx + ix, gy + iy, direction=idir, extra=ins_extra)


def _place_landing_pad(
    res: PlacementResult,
    pad_name: str,
    outputs: list[str],
    gx: float,
    gy: float,
    in_offset: float,
    m_w: int,
    cell: JsonMap,
    cfg: PlacementConfig,
) -> None:
    """Place a cargo landing pad and its output inserters/belts next to a rocket silo."""
    pad_w = _as_int(cell.get("landing_pad_width"), cfg.default_landing_pad_size)
    pad_h = _as_int(cell.get("landing_pad_height"), cfg.default_landing_pad_size)
    pad_grid = _as_int(cell.get("landing_pad_grid_size"), cfg.default_landing_pad_grid)
    pad_left = round(gx + in_offset + m_w)
    if ((pad_left + pad_w // 2) % pad_grid) != 0:
        pad_left += 1
    pad_bottom = round(gy)
    if ((pad_bottom + pad_h // 2) % pad_grid) != 0:
        pad_bottom += 1
    res.add_entity(pad_name, float(pad_left) + pad_w / 2.0, float(pad_bottom) + pad_h / 2.0)
    res.mark_footprint(float(pad_left), float(pad_bottom), pad_w, pad_h)

    pad_right = pad_left + pad_w
    tiers = _as_list(cell.get("inserter_tiers"))
    last_tier = _as_map(tiers[-1]) if tiers else {}
    ins_name = _as_str(last_tier.get("name"), cfg.default_inserter_name)
    for port in outputs:
        if port in res.fluids:
            continue
        geom = get_macro_port_geometry(dict(cell), port, is_output=True)
        ix, iy = geom["inserter_pos"]
        idir = int(geom["inserter_dir"])
        ins_x = float(pad_right) + TILE_HALF
        ins_y = gy + iy
        res.add_entity(ins_name, ins_x, ins_y, direction=idir)
        out_x = gx + ix
        if round(ins_x, 2) < round(out_x, 2):
            bdir = _as_int(geom.get("approach_dir"), DIRECTION_EAST)
            res.add_entity("transport-belt", out_x, ins_y, direction=bdir)


def _place_cell_pipes_and_ports(
    res: PlacementResult,
    gx: float,
    gy: float,
    cell: JsonMap,
    machine: str,
    has_blockages: bool,
) -> None:
    """Place internal pipes and reserve unused fluid port tiles for a machine cell."""
    for raw_pipe in _as_list(cell.get("internal_pipes")):
        ipipe = _as_map(raw_pipe)
        px = gx + _as_float(ipipe.get("x"))
        py = gy + _as_float(ipipe.get("y"))
        p_extra: EntityDict | None = {"fluid": str(ipipe["fluid"])} if "fluid" in ipipe else None
        res.add_entity("pipe", px, py, extra=p_extra)

    if not has_blockages or machine in AON_INSERTER_MACHINES:
        for raw_port in _as_list(cell.get("unused_fluid_ports")):
            ux, uy = _as_list(raw_port)
            res.occupied_positions.add((round(gx + _as_float(ux), 2), round(gy + _as_float(uy), 2)))


def _resolve_cell_recipe(inst: JsonMap, cell: JsonMap) -> str:
    """Resolve the active recipe string for a machine instance and cell definition."""
    for candidate in (cell.get("fixed_recipe"), inst.get("recipe"), cell.get("recipe")):
        if candidate:
            return str(candidate)
    return ""


def _build_machine_extra(machine: str, recipe: str, pad_name: str) -> EntityDict | None:
    """Build the extra attribute dictionary for a crafting machine entity."""
    extra: EntityDict = {}
    if recipe and machine not in NO_RECIPE_MACHINES:
        extra["recipe"] = recipe
    if machine == "steam-engine":
        extra["power_domain"] = "aon"
    if pad_name:
        extra["launch_to_orbit_automatically"] = True
    return extra or None


def _place_machine_cell(
    res: PlacementResult,
    gx: float,
    gy: float,
    inst: JsonMap,
    cell: JsonMap,
    has_blockages: bool,
    cfg: PlacementConfig,
) -> EntityDict:
    """Place a crafting machine cell, its port inserters, internal pipes, and occupancy."""
    cw = _as_int(cell.get("width"), cfg.default_cell_size)
    ch = _as_int(cell.get("height"), cfg.default_cell_size)
    m_w = _as_int(cell.get("machine_width"), cfg.default_cell_size)
    m_h = _as_int(cell.get("machine_height"), cfg.default_cell_size)
    machine = _as_str(cell.get("machine"), "assembling-machine-3")
    recipe = _resolve_cell_recipe(inst, cell)
    pad_name = _as_str(cell.get("landing_pad"))

    in_offset = 1.0 if cw > m_w else 0.0
    y_offset = 1.0 if ch > m_h else 0.0
    direction = _as_int(cell.get("direction"), DIRECTION_NORTH)
    ent = res.add_entity(
        machine,
        gx + in_offset + m_w / 2.0,
        gy + y_offset + m_h / 2.0,
        direction=direction,
        extra=_build_machine_extra(machine, recipe, pad_name),
    )

    inputs = sorted(str(p) for p in _as_list(cell.get("inputs")))
    outputs = sorted(str(p) for p in _as_list(cell.get("outputs")))
    _place_port_inserters(res, inputs, False, gx, gy, inst, cell, machine, cfg)
    if pad_name:
        _place_landing_pad(res, pad_name, outputs, gx, gy, in_offset, m_w, cell, cfg)
    else:
        _place_port_inserters(res, outputs, True, gx, gy, inst, cell, machine, cfg)

    _place_cell_pipes_and_ports(res, gx, gy, cell, machine, has_blockages)
    res.mark_footprint(gx + in_offset, gy + y_offset, m_w, m_h)
    return ent


def place_components(
    def_data: JsonMap,
    netlist_data: JsonMap,
    config: PlacementConfig = DEFAULT_PLACEMENT_CONFIG,
) -> PlacementResult:
    """Place all macro components, inserters, and control entities."""
    scale = 1.0 / _as_float(def_data.get("units"), config.default_units)
    inst_map: dict[str, JsonMap] = {
        str(m["name"]): m
        for raw in _as_list(netlist_data.get("instances"))
        if (m := _as_map(raw)) and "name" in m
    }
    cell_map = _as_map(netlist_data.get("cells"))
    res = PlacementResult(
        entities=[],
        occupied_positions=set(),
        grid_entities={},
        inst_to_entity={},
        fluids={str(f) for f in _as_list(netlist_data.get("fluids"))},
        has_fluid_metadata="fluids" in netlist_data,
    )
    has_blockages = bool(def_data.get("blockages"))

    for raw_comp in _as_list(def_data.get("components")):
        comp = _as_map(raw_comp)
        cname = _as_str(comp.get("name"))
        macro = _as_str(comp.get("macro"))
        gx = _as_float(comp.get("x")) * scale
        gy = _as_float(comp.get("y")) * scale
        orient = _as_str(comp.get("orient"), "N")
        inst = inst_map.get(cname, {})
        cell_id = _as_str(inst.get("cell"))
        cell = _as_map(cell_map.get(cell_id))

        placer = SPECIAL_MACRO_PLACERS.get(macro) or SPECIAL_MACRO_PLACERS.get(cell_id)
        if placer is not None:
            res.inst_to_entity[cname] = placer(res, orient, gx, gy, inst, config)
        else:
            res.inst_to_entity[cname] = _place_machine_cell(
                res, gx, gy, inst, cell, has_blockages, config
            )

    return res
