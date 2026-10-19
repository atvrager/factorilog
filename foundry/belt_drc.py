"""Physical Design Rule Check (DRC) engine for Factorio layouts."""

from __future__ import annotations

import collections
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

try:
    from foundry.belt_library import (
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        INSERTER_OFFSETS,
        MAX_UNDERGROUND_SPAN,
        OPPOSITE_DIR,
    )
except ImportError:
    from belt_library import (
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        INSERTER_OFFSETS,
        MAX_UNDERGROUND_SPAN,
        OPPOSITE_DIR,
    )

try:
    from foundry.entity_helpers import (
        DEFAULT_DEF_UNITS,
        DEFAULT_POLE_WIRE_REACH,
        DEFAULT_UNDERGROUND_SPAN,
        LONG_INSERTER_REACH,
        POLE_BUCKET_SIZE,
        STANDARD_INSERTER_REACH,
        TILE_HALF,
        entity_collision_half_size,
        entity_size,
        get_pole_supply_radius,
        get_pole_wire_reach,
        get_underground_span,
        is_electric_consumer,
        is_electric_pole,
    )
except ImportError:
    from entity_helpers import (
        DEFAULT_DEF_UNITS,
        DEFAULT_POLE_WIRE_REACH,
        DEFAULT_UNDERGROUND_SPAN,
        LONG_INSERTER_REACH,
        POLE_BUCKET_SIZE,
        STANDARD_INSERTER_REACH,
        TILE_HALF,
        entity_collision_half_size,
        entity_size,
        get_pole_supply_radius,
        get_pole_wire_reach,
        get_underground_span,
        is_electric_consumer,
        is_electric_pole,
    )

try:
    from foundry.pump_insertion import (
        build_conduit_graph,
        component_extent,
        find_fluid_components,
    )
except ImportError:
    from pump_insertion import (
        build_conduit_graph,
        component_extent,
        find_fluid_components,
    )

EntityRecord = Mapping[str, object]
TileMap = Mapping[tuple[int, int], list[EntityRecord]]
PinCollection = Sequence[tuple[float, float]] | set[tuple[int, int]] | set[tuple[float, float]]

DEFAULT_MAX_PIPELINE_EXTENT = 320
EXTERNAL_LOADER_STEPS = (1, 2, 3)
MAX_UPSTREAM_TRACE_STEPS = 500
CARDINAL_STEPS: tuple[tuple[int, int], ...] = ((0, -1), (1, 0), (0, 1), (-1, 0))
ORTHOGONAL_DIRS: dict[int, tuple[int, int]] = {
    DIRECTION_NORTH: (DIRECTION_EAST, DIRECTION_WEST),
    DIRECTION_SOUTH: (DIRECTION_EAST, DIRECTION_WEST),
    DIRECTION_EAST: (DIRECTION_NORTH, DIRECTION_SOUTH),
    DIRECTION_WEST: (DIRECTION_NORTH, DIRECTION_SOUTH),
}
WIRE_TUPLE_MIN_LEN = 4
WIRE_SECOND_ENTITY_INDEX = 2
WIRE_SECOND_CONN_INDEX = 3
CIRCUIT_INPUT_CONNECTORS: frozenset[int] = frozenset({1, 2})
CIRCUIT_OUTPUT_CONNECTORS: frozenset[int] = frozenset({3, 4})
COPPER_LEFT_CONNECTOR = 5
COPPER_RIGHT_CONNECTOR = 6
COPPER_CONNECTORS: frozenset[int] = frozenset({COPPER_LEFT_CONNECTOR, COPPER_RIGHT_CONNECTOR})
WIRE_DISTANCE_TOLERANCE = 1e-4

DEFAULT_CHASSIS_PITCH = 100
DEFAULT_CHASSIS_MARGIN = 3
DIE_AREA_WIDTH_INDEX = 2
DIE_AREA_HEIGHT_INDEX = 3
BAY_STREET_MIN_RX = 11
BAY_STREET_MAX_RX = 88
BAY_STREET_MAX_RY = 16
BAY_CORE_MIN_RY = 17
BAY_PORT_MIN_LAT = 19


@dataclass(frozen=True)
class DrcViolation:
    """A single physical design rule violation."""

    code: str
    message: str
    x: float
    y: float
    net: str = ""
    severity: str = "ERROR"


@dataclass(frozen=True)
class _ChassisGeom:
    """Runtime geometry parameters for chassis matrix discipline checks."""

    grid_w: int
    grid_h: int
    pitch: int
    margin: int
    chan_w: int
    active_bays: set[tuple[int, int]]


def _as_float(val: object, default: float = 0.0) -> float:
    """Convert a dynamic JSON/entity scalar value to float."""
    if isinstance(val, (int, float, str)):
        return float(val)
    return default


def _as_int(val: object, default: int = 0) -> int:
    """Convert a dynamic JSON/entity scalar value to int."""
    if isinstance(val, (int, float, str)):
        return int(val)
    return default


def _ent_coords(ent: EntityRecord) -> tuple[float, float]:
    """Return floating-point (x, y) coordinates of an entity."""
    pos = ent.get("position", {})
    if isinstance(pos, Mapping):
        return _as_float(pos.get("x", 0)), _as_float(pos.get("y", 0))
    if isinstance(pos, Sequence) and len(pos) >= 2:
        return _as_float(pos[0]), _as_float(pos[1])
    return 0.0, 0.0


def _ent_int_coords(ent: EntityRecord) -> tuple[int, int]:
    """Return integer (x, y) coordinates of an entity."""
    cx, cy = _ent_coords(ent)
    return int(cx), int(cy)


def _ent_floor_coords(ent: EntityRecord) -> tuple[int, int]:
    """Return floored integer tile coordinates (x, y) of an entity."""
    cx, cy = _ent_coords(ent)
    return math.floor(cx), math.floor(cy)


def _ent_dir(ent: EntityRecord) -> int:
    """Return integer cardinal direction of an entity."""
    return _as_int(ent.get("direction", DIRECTION_NORTH), DIRECTION_NORTH)


def _ent_name(ent: EntityRecord) -> str:
    """Return prototype name of an entity."""
    return str(ent.get("name", ""))


def _splitter_halves(ent: EntityRecord) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return the two integer tile coordinates occupied by a 2-tile splitter."""
    cx, cy = _ent_coords(ent)
    if _ent_dir(ent) in (DIRECTION_NORTH, DIRECTION_SOUTH):
        return (math.floor(cx - TILE_HALF), math.floor(cy)), (
            math.floor(cx + TILE_HALF),
            math.floor(cy),
        )
    return (math.floor(cx), math.floor(cy - TILE_HALF)), (
        math.floor(cx),
        math.floor(cy + TILE_HALF),
    )


def _occupied_tiles_for_entity(ent: EntityRecord) -> tuple[tuple[int, int], ...]:
    """Return integer tile coordinates occupied by an entity on the surface map."""
    name = _ent_name(ent)
    if "splitter" in name:
        return _splitter_halves(ent)
    if "pump" in name:
        cx, cy = _ent_coords(ent)
        if _ent_dir(ent) in (DIRECTION_NORTH, DIRECTION_SOUTH):
            return (
                (math.floor(cx), math.floor(cy - TILE_HALF)),
                (math.floor(cx), math.floor(cy + TILE_HALF)),
            )
        return (
            (math.floor(cx - TILE_HALF), math.floor(cy)),
            (math.floor(cx + TILE_HALF), math.floor(cy)),
        )
    return (_ent_floor_coords(ent),)


def _build_tile_map(entities: Sequence[EntityRecord]) -> dict[tuple[int, int], list[EntityRecord]]:
    """Map integer tile coordinates to placed entities."""
    tile_map: dict[tuple[int, int], list[EntityRecord]] = {}
    for ent in entities:
        for tile in _occupied_tiles_for_entity(ent):
            tile_map.setdefault(tile, []).append(ent)
    return tile_map


def _collect_machine_tiles(entities: Sequence[EntityRecord]) -> set[tuple[int, int]]:
    """Return all integer tiles occupied by multi-tile crafting machines."""
    machine_tiles: set[tuple[int, int]] = set()
    for ent in entities:
        name = _ent_name(ent)
        if "switch" in name or "splitter" in name or "pole" in name:
            continue
        w_f, h_f = entity_size(name, _ent_dir(ent))
        if w_f >= 2.0 or h_f >= 2.0:
            w, h = int(w_f), int(h_f)
            cx, cy = _ent_coords(ent)
            min_x = math.floor(cx - w / 2.0)
            min_y = math.floor(cy - h / 2.0)
            machine_tiles.update(
                (tx, ty) for tx in range(min_x, min_x + w) for ty in range(min_y, min_y + h)
            )
    return machine_tiles


def _collect_container_and_supply_tiles(
    entities: Sequence[EntityRecord],
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """Return (container_tiles, supply_tiles) across all belts, splitters, and chests."""
    container_tiles: set[tuple[int, int]] = set()
    supply_tiles: set[tuple[int, int]] = set()
    for ent in entities:
        name = _ent_name(ent)
        if "transport-belt" in name or "underground-belt" in name:
            supply_tiles.add(_ent_floor_coords(ent))
        elif "splitter" in name:
            supply_tiles.update(_splitter_halves(ent))
        elif "chest" in name or "container" in name:
            tile = _ent_floor_coords(ent)
            supply_tiles.add(tile)
            container_tiles.add(tile)
    return container_tiles, supply_tiles


def _inserter_endpoints(
    ent: EntityRecord,
) -> tuple[int, int, tuple[int, int], tuple[int, int]]:
    """Return (ix, iy, pickup_tile, drop_tile) for an inserter entity."""
    ix, iy = _ent_floor_coords(ent)
    d = _ent_dir(ent)
    mult = LONG_INSERTER_REACH if "long-handed" in _ent_name(ent) else STANDARD_INSERTER_REACH
    offsets = INSERTER_OFFSETS.get(d, ((0, -1), (0, 1)))
    pickup_tile = (ix + offsets[0][0] * mult, iy + offsets[0][1] * mult)
    drop_tile = (ix + offsets[1][0] * mult, iy + offsets[1][1] * mult)
    return ix, iy, pickup_tile, drop_tile


def _classify_inserters(
    entities: Sequence[EntityRecord],
    machine_tiles: set[tuple[int, int]],
) -> tuple[list[tuple[int, int, tuple[int, int]]], list[tuple[int, int, tuple[int, int]]]]:
    """Classify inserters into (machine_input_inserters, machine_output_inserters)."""
    input_inserters: list[tuple[int, int, tuple[int, int]]] = []
    output_inserters: list[tuple[int, int, tuple[int, int]]] = []
    for ent in entities:
        if "inserter" not in _ent_name(ent):
            continue
        ix, iy, pickup_tile, drop_tile = _inserter_endpoints(ent)
        if drop_tile in machine_tiles:
            input_inserters.append((ix, iy, pickup_tile))
        elif pickup_tile in machine_tiles:
            output_inserters.append((ix, iy, drop_tile))
    return input_inserters, output_inserters


class BeltDrcChecker:
    """Validator for physical belt, underground, and splitter rules."""

    def __init__(
        self,
        allow_peeling: bool = False,
        rules_database: Mapping[str, object] | None = None,
        rules_path: Path | str | None = None,
    ) -> None:
        self.allow_peeling = allow_peeling
        self.span_limits: dict[str, int] = dict(MAX_UNDERGROUND_SPAN)
        self.max_pipeline_extent = DEFAULT_MAX_PIPELINE_EXTENT
        if rules_database is not None:
            self._apply_rules_database(rules_database)
        elif rules_path is not None and Path(rules_path).is_file():
            db = json.loads(Path(rules_path).read_text())
            self._apply_rules_database(db)

    def _apply_rules_database(self, db: Mapping[str, object]) -> None:
        """Apply parameters from rule database."""
        tiers = db.get("belt_tiers", {})
        if isinstance(tiers, Mapping):
            self._apply_belt_tiers(tiers)
        derived_rules = db.get("derived_rules", [])
        if isinstance(derived_rules, Sequence):
            for r in derived_rules:
                if isinstance(r, Mapping):
                    self._apply_single_derived_rule(r)

    def _apply_belt_tiers(self, tiers: Mapping[object, object]) -> None:
        """Apply underground span limits from belt_tiers mapping."""
        for tier_data in tiers.values():
            if not isinstance(tier_data, Mapping):
                continue
            ug_name = tier_data.get("underground")
            max_span = tier_data.get("max_underground_span_tiles")
            if ug_name and max_span:
                self.span_limits[str(ug_name)] = _as_int(max_span)

    def _apply_single_derived_rule(self, r: Mapping[str, object]) -> None:
        """Apply a single derived rule entry from the rules database."""
        rule_id = r.get("rule_id")
        if rule_id == "RULE-05-UNDERGROUND_PAIR":
            spans = r.get("max_span_tiles", {})
            if isinstance(spans, Mapping):
                for k, v in spans.items():
                    self.span_limits[str(k)] = _as_int(v)
        elif rule_id == "RULE-16-PIPELINE_EXTENT":
            self.max_pipeline_extent = _as_int(
                r.get("max_extent_tiles", DEFAULT_MAX_PIPELINE_EXTENT),
                DEFAULT_MAX_PIPELINE_EXTENT,
            )

    def _tier_span(self, name: str) -> int:
        """Return maximum underground span for the named belt prototype."""
        return self.span_limits.get(
            name, get_underground_span(name, default=DEFAULT_UNDERGROUND_SPAN)
        )

    def check_entities(
        self,
        entities: Sequence[EntityRecord],
        _net_map: Mapping[object, object] | None = None,
        input_pins: PinCollection | None = None,
        unresolved_crossings: PinCollection | None = None,
        output_pins: PinCollection | None = None,
        wires: Sequence[Sequence[int]] | None = None,
        tile_to_nets: Mapping[tuple[float, float], set[str]] | None = None,
        fluids: set[str] | frozenset[str] | None = None,
    ) -> list[DrcViolation]:
        """Verify all physical rules on a list of placed entities."""
        tile_map = _build_tile_map(entities)
        violations: list[DrcViolation] = []
        violations.extend(self._check_head_to_head(entities, tile_map))
        violations.extend(self._check_underground_pairing(entities, tile_map))
        if not self.allow_peeling:
            violations.extend(self._check_hood_clearance(entities, tile_map))
        violations.extend(self._check_splitter_clearance(entities, tile_map))
        violations.extend(
            self._check_machine_input_connectivity(entities, tile_map, input_pins=input_pins)
        )
        violations.extend(
            self._check_machine_output_connectivity(entities, tile_map, output_pins=output_pins)
        )
        violations.extend(self._check_underground_span_overlap(entities, tile_map))
        violations.extend(
            self._check_surface_collision(tile_map, unresolved_crossings=unresolved_crossings)
        )
        if self._should_check_circuits(entities, wires):
            violations.extend(self._check_circuit_connectivity(entities, wires or []))
        violations.extend(
            self._check_fluid_mixing(
                entities,
                tile_map,
                tile_to_nets=tile_to_nets,
                fluids=fluids,
                net_map=_net_map,
            )
        )
        violations.extend(self._check_pipe_pairing(entities, tile_map))
        violations.extend(self._check_pipe_span_overlap(entities, tile_map))
        violations.extend(self._check_pipeline_extent(entities, tile_map))
        if any(is_electric_pole(_ent_name(e)) for e in entities):
            violations.extend(self._check_power_coverage(entities, wires or []))
        return violations

    @staticmethod
    def _should_check_circuits(
        entities: Sequence[EntityRecord],
        wires: Sequence[Sequence[int]] | None,
    ) -> bool:
        """Return True if circuit connectivity rules should be evaluated."""
        if wires is not None:
            return True
        return any("combinator" in _ent_name(e) or "switch" in _ent_name(e) for e in entities)

    def _check_head_to_head(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> list[DrcViolation]:
        """Detect opposing belts that point directly at each other."""
        violations: list[DrcViolation] = []
        for ent in entities:
            d = _ent_dir(ent)
            if _ent_name(ent) != "transport-belt" or d not in DIR_STEPS:
                continue
            x, y = _ent_int_coords(ent)
            dx, dy = DIR_STEPS[d]
            target_pos = (x + dx, y + dy)
            opp_d = OPPOSITE_DIR.get(d)
            if any(
                _ent_name(t) == "transport-belt" and _ent_dir(t) == opp_d
                for t in tile_map.get(target_pos, ())
            ):
                violations.append(
                    DrcViolation(
                        code="DRC-01",
                        message=f"Opposing belts face each other at ({x}, {y}) and {target_pos}",
                        x=x,
                        y=y,
                    )
                )
        return violations

    def _find_matching_underground_output(
        self,
        u_in: EntityRecord,
        tile_map: TileMap,
    ) -> tuple[EntityRecord | None, tuple[int, int], int]:
        """Find the nearest matching underground output within max span."""
        name = _ent_name(u_in) or "underground-belt"
        d = _ent_dir(u_in)
        x, y = _ent_int_coords(u_in)
        max_span = self._tier_span(name)
        dx, dy = DIR_STEPS.get(d, (0, 0))
        for span in range(1, max_span + 1):
            chk_pos = (x + dx * span, y + dy * span)
            for c in tile_map.get(chk_pos, ()):
                if _ent_name(c) == name and _ent_dir(c) == d and c.get("type") == "output":
                    return c, chk_pos, span
        return None, (x, y), max_span

    def _check_underground_pairing(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> list[DrcViolation]:
        """Verify that every underground input pairs with an output within maximum span."""
        violations: list[DrcViolation] = []
        inputs = [
            e for e in entities if "underground-belt" in _ent_name(e) and e.get("type") == "input"
        ]
        outputs = [
            e for e in entities if "underground-belt" in _ent_name(e) and e.get("type") == "output"
        ]
        matched_outputs: set[int] = set()

        for u_in in inputs:
            matched_ent, _, max_span = self._find_matching_underground_output(u_in, tile_map)
            if matched_ent is not None:
                matched_outputs.add(id(matched_ent))
            else:
                x, y = _ent_int_coords(u_in)
                violations.append(
                    DrcViolation(
                        code="DRC-02",
                        message=(
                            f"Underground input at ({x}, {y}) has no matching output "
                            f"within span {max_span}"
                        ),
                        x=x,
                        y=y,
                    )
                )

        for u_out in outputs:
            if id(u_out) not in matched_outputs:
                x, y = _ent_int_coords(u_out)
                violations.append(
                    DrcViolation(
                        code="DRC-02",
                        message=f"Underground output at ({x}, {y}) has no matching upstream input",
                        x=x,
                        y=y,
                    )
                )
        return violations

    def _check_hood_clearance(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> list[DrcViolation]:
        """Ensure no perpendicular belt feeds into the side of an underground hood."""
        violations: list[DrcViolation] = []
        for ent in entities:
            if "underground-belt" not in _ent_name(ent):
                continue
            hood_d = _ent_dir(ent)
            hx, hy = _ent_int_coords(ent)
            for s_dir in ORTHOGONAL_DIRS.get(hood_d, (DIRECTION_NORTH, DIRECTION_SOUTH)):
                sdx, sdy = DIR_STEPS[s_dir]
                nb_pos = (hx - sdx, hy - sdy)
                violations.extend(
                    DrcViolation(
                        code="DRC-03",
                        message=(
                            f"Perpendicular belt at {nb_pos} feeds into underground hood side "
                            f"at ({hx}, {hy})"
                        ),
                        x=nb_pos[0],
                        y=nb_pos[1],
                    )
                    for nb in tile_map.get(nb_pos, ())
                    if self._is_surface_belt_or_splitter(nb, s_dir)
                )
            if ent.get("type") == "output":
                dx, dy = DIR_STEPS[hood_d]
                rear_pos = (hx - dx, hy - dy)
                violations.extend(
                    DrcViolation(
                        code="DRC-03",
                        message=(
                            f"Surface belt at {rear_pos} feeds into rear of underground output "
                            f"hood at ({hx}, {hy})"
                        ),
                        x=rear_pos[0],
                        y=rear_pos[1],
                    )
                    for nb in tile_map.get(rear_pos, ())
                    if self._is_surface_belt_or_splitter(nb, hood_d)
                )
        return violations

    @staticmethod
    def _is_surface_belt_or_splitter(ent: EntityRecord, direction: int) -> bool:
        """Return True if ent is a surface transport belt or splitter facing direction."""
        name = _ent_name(ent)
        return ("transport-belt" in name or "splitter" in name) and _ent_dir(ent) == direction

    def _check_splitter_clearance(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> list[DrcViolation]:
        """Ensure no perpendicular belt feeds into the flank of a splitter."""
        violations: list[DrcViolation] = []
        for ent in entities:
            if "splitter" not in _ent_name(ent):
                continue
            d = _ent_dir(ent)
            sx, sy = _ent_int_coords(ent)
            for f_dir in ORTHOGONAL_DIRS.get(d, (DIRECTION_NORTH, DIRECTION_SOUTH)):
                fdx, fdy = DIR_STEPS[f_dir]
                flank_pos = (sx - fdx, sy - fdy)
                violations.extend(
                    DrcViolation(
                        code="DRC-04",
                        message=(
                            f"Perpendicular belt at {flank_pos} feeds into splitter flank "
                            f"at ({sx}, {sy})"
                        ),
                        x=flank_pos[0],
                        y=flank_pos[1],
                    )
                    for nb in tile_map.get(flank_pos, ())
                    if _ent_name(nb) == "transport-belt" and _ent_dir(nb) == f_dir
                )
        return violations

    def _validate_external_pin_loaders(
        self,
        pins: PinCollection,
        tile_map: TileMap,
        is_input: bool,
    ) -> tuple[set[tuple[int, int]], list[DrcViolation]]:
        """Validate loader clearance for external input or output pins and return valid pin tiles."""
        valid_pins: set[tuple[int, int]] = set()
        reserved_tiles: set[tuple[int, int]] = set()
        violations: list[DrcViolation] = []

        for pin in pins:
            if not isinstance(pin, Sequence) or len(pin) < 2:
                continue
            px, py = math.floor(pin[0]), math.floor(pin[1])
            violation = self._check_single_pin_loader(px, py, tile_map, reserved_tiles, is_input)
            if violation is not None:
                violations.append(violation)
            else:
                valid_pins.add((px, py))
        return valid_pins, violations

    def _check_single_pin_loader(
        self,
        px: int,
        py: int,
        tile_map: TileMap,
        reserved_tiles: set[tuple[int, int]],
        is_input: bool,
    ) -> DrcViolation | None:
        """Check loader clearance for one external pin and reserve its loader tiles if valid."""
        belt_ent = next(
            (
                e
                for e in tile_map.get((px, py), ())
                if "transport-belt" in _ent_name(e) or "underground-belt" in _ent_name(e)
            ),
            None,
        )
        if belt_ent is None:
            return None
        b_dir = _ent_dir(belt_ent)
        dx, dy = DIR_STEPS.get(b_dir, (0, -1))
        sign, code, label, detail = (
            (-1, "DRC-05", "input", "blocked or misoriented")
            if is_input
            else (1, "DRC-06", "output", "blocked")
        )
        loader_tiles = tuple(
            (px + sign * step * dx, py + sign * step * dy) for step in EXTERNAL_LOADER_STEPS
        )
        blocked = any(bool(tile_map.get(lt)) or lt in reserved_tiles for lt in loader_tiles)
        if blocked or (is_input and self._is_misoriented_input_pin(px, py, b_dir)):
            return DrcViolation(
                code=code,
                message=(
                    f"External {label} pin at ({px}, {py}) with direction {b_dir} "
                    f"has {detail} external loader/infinity-chest tiles {loader_tiles}"
                ),
                x=px,
                y=py,
            )
        reserved_tiles.update(loader_tiles)
        return None

    @staticmethod
    def _is_misoriented_input_pin(px: int, py: int, b_dir: int) -> bool:
        """Return True if a boundary input pin belt points along or out of the boundary."""
        if px == 0 and b_dir != DIRECTION_EAST:
            return True
        return py == 0 and px > 0 and b_dir != DIRECTION_SOUTH

    @staticmethod
    def _infer_belt_root_sources(tile_map: TileMap) -> set[tuple[int, int]]:
        """Infer source tiles with no incoming belt, splitter, or underground output."""
        incoming_targets: set[tuple[int, int]] = set()
        for t_pos, ents in tile_map.items():
            for e in ents:
                e_name = _ent_name(e)
                e_dx, e_dy = DIR_STEPS.get(_ent_dir(e), (0, 0))
                if (
                    "transport-belt" in e_name
                    or ("underground-belt" in e_name and e.get("type") == "output")
                    or "splitter" in e_name
                ):
                    incoming_targets.add((t_pos[0] + e_dx, t_pos[1] + e_dy))
        return {
            t_pos
            for t_pos, ents in tile_map.items()
            if t_pos not in incoming_targets
            and any(
                "transport-belt" in _ent_name(e)
                or ("underground-belt" in _ent_name(e) and e.get("type") == "input")
                for e in ents
            )
        }

    def _next_transport_tiles(
        self,
        curr: tuple[int, int],
        ent: EntityRecord,
        tile_map: TileMap,
    ) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        """Return list of (parent_tile, next_tile) forward transitions from ent at curr."""
        e_name = _ent_name(ent)
        e_dir = _ent_dir(ent)
        e_dx, e_dy = DIR_STEPS.get(e_dir, (0, 0))
        if "transport-belt" in e_name:
            next_tile = (curr[0] + e_dx, curr[1] + e_dy)
            if any(self._accepts_belt_feed(ne, e_dir) for ne in tile_map.get(next_tile, ())):
                return [(curr, next_tile)]
            return []
        if "underground-belt" in e_name:
            return self._underground_forward_steps(curr, ent, tile_map)
        if "splitter" in e_name:
            half1, half2 = _splitter_halves(ent)
            out1 = (half1[0] + e_dx, half1[1] + e_dy)
            out2 = (half2[0] + e_dx, half2[1] + e_dy)
            return [(curr, t) for t in (half1, half2, out1, out2)]
        return []

    @staticmethod
    def _accepts_belt_feed(ne: EntityRecord, src_dir: int) -> bool:
        """Return True if entity ne on the downstream tile accepts items moving in src_dir."""
        ne_name = _ent_name(ne)
        ne_dir = _ent_dir(ne)
        if "transport-belt" in ne_name:
            return ne_dir != OPPOSITE_DIR.get(src_dir)
        if "underground-belt" in ne_name:
            return ne.get("type") == "input" and ne_dir == src_dir
        return "splitter" in ne_name and ne_dir == src_dir

    def _underground_forward_steps(
        self,
        curr: tuple[int, int],
        ent: EntityRecord,
        tile_map: TileMap,
    ) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        """Return forward (parent_tile, next_tile) transitions for an underground belt."""
        u_type = ent.get("type", "")
        e_dir = _ent_dir(ent)
        e_dx, e_dy = DIR_STEPS.get(e_dir, (0, 0))
        if u_type == "output":
            return [(curr, (curr[0] + e_dx, curr[1] + e_dy))]
        if u_type != "input":
            return []
        e_name = _ent_name(ent)
        max_span = self._tier_span(e_name)
        for span in range(1, max_span + 1):
            out_pos = (curr[0] + e_dx * span, curr[1] + e_dy * span)
            if any(
                _ent_name(cand) == e_name
                and _ent_dir(cand) == e_dir
                and cand.get("type") == "output"
                for cand in tile_map.get(out_pos, ())
            ):
                front_tile = (out_pos[0] + e_dx, out_pos[1] + e_dy)
                return [(curr, out_pos), (out_pos, front_tile)]
        return []

    def _bfs_forward_reachable(
        self,
        sources: set[tuple[int, int]],
        tile_map: TileMap,
    ) -> set[tuple[int, int]]:
        """Return all tiles reachable downstream from sources."""
        visited: set[tuple[int, int]] = set(sources)
        queue = collections.deque(sources)
        while queue:
            curr = queue.popleft()
            for ent in tile_map.get(curr, ()):
                for _parent, nxt in self._next_transport_tiles(curr, ent, tile_map):
                    if nxt not in visited:
                        visited.add(nxt)
                        queue.append(nxt)
        return visited

    def _find_upstream_tile(
        self,
        curr: tuple[int, int],
        tile_map: TileMap,
    ) -> tuple[int, int] | None:
        """Find one immediate upstream feeder tile for diagnostics."""
        for te in tile_map.get(curr, ()):
            found = self._find_upstream_from_entity(curr, te, tile_map)
            if found is not None:
                return found
        return self._find_upstream_neighbor_belt(curr, tile_map)

    def _find_upstream_from_entity(
        self,
        curr: tuple[int, int],
        te: EntityRecord,
        tile_map: TileMap,
    ) -> tuple[int, int] | None:
        """Find upstream feeder tile for an underground output or splitter at curr."""
        tname = _ent_name(te)
        if "underground-belt" in tname and te.get("type") == "output":
            return self._find_upstream_underground_input(curr, te, tile_map)
        if "splitter" in tname:
            return self._find_upstream_splitter_input(te, tile_map)
        return None

    def _find_upstream_underground_input(
        self,
        curr: tuple[int, int],
        te: EntityRecord,
        tile_map: TileMap,
    ) -> tuple[int, int] | None:
        """Find matching upstream underground input tile for an underground output."""
        td = _ent_dir(te)
        tdx, tdy = DIR_STEPS.get(td, (0, 0))
        tname = _ent_name(te)
        max_s = self._tier_span(tname)
        for sp in range(1, max_s + 1):
            in_p = (curr[0] - tdx * sp, curr[1] - tdy * sp)
            if any(
                _ent_name(ce) == tname and _ent_dir(ce) == td and ce.get("type") == "input"
                for ce in tile_map.get(in_p, ())
            ):
                return in_p
        return None

    @staticmethod
    def _find_upstream_splitter_input(
        te: EntityRecord,
        tile_map: TileMap,
    ) -> tuple[int, int] | None:
        """Find an occupied tile immediately behind either half of a splitter."""
        sdx, sdy = DIR_STEPS.get(_ent_dir(te), (0, 0))
        for hp in _splitter_halves(te):
            bp = (hp[0] - sdx, hp[1] - sdy)
            if tile_map.get(bp):
                return bp
        return None

    @staticmethod
    def _find_upstream_neighbor_belt(
        curr: tuple[int, int],
        tile_map: TileMap,
    ) -> tuple[int, int] | None:
        """Find an adjacent cardinal tile with a belt, splitter, or underground output pointing into curr."""
        for ndx, ndy in CARDINAL_STEPS:
            prev_p = (curr[0] - ndx, curr[1] - ndy)
            for pe in tile_map.get(prev_p, ()):
                pname = _ent_name(pe)
                if DIR_STEPS.get(_ent_dir(pe)) == (ndx, ndy) and (
                    "transport-belt" in pname
                    or ("underground-belt" in pname and pe.get("type") == "output")
                    or "splitter" in pname
                ):
                    return prev_p
        return None

    def _trace_upstream_root(
        self,
        pickup_tile: tuple[int, int],
        visited: set[tuple[int, int]],
        tile_map: TileMap,
    ) -> tuple[tuple[int, int], int]:
        """Trace upstream from an unvisited pickup tile to find the root of the disconnected chain."""
        trace_curr = pickup_tile
        trace_seen = {trace_curr}
        path_len = 1
        for _ in range(MAX_UPSTREAM_TRACE_STEPS):
            found_prev = self._find_upstream_tile(trace_curr, tile_map)
            if found_prev is None or found_prev in trace_seen:
                break
            trace_seen.add(found_prev)
            path_len += 1
            trace_curr = found_prev
            if trace_curr in visited:
                break
        return trace_curr, path_len

    @staticmethod
    def _describe_tile_neighborhood(
        tile: tuple[int, int],
        visited: set[tuple[int, int]],
        tile_map: TileMap,
    ) -> tuple[list[str], list[str]]:
        """Format entity and neighbor descriptions for DRC-05 diagnostics."""
        ents_desc = [
            f"{e.get('name')}(d={e.get('direction')},t={e.get('type')})"
            for e in tile_map.get(tile, ())
        ]
        nb_desc: list[str] = []
        for ndx, ndy in CARDINAL_STEPS:
            np = (tile[0] + ndx, tile[1] + ndy)
            n_ents = tile_map.get(np, ())
            if n_ents:
                items = ",".join(
                    f"{e.get('name')}(d={e.get('direction')},t={e.get('type')})" for e in n_ents
                )
                nb_desc.append(f"{np}:vis={np in visited}:{items}")
        return ents_desc, nb_desc

    def _check_machine_input_connectivity(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
        input_pins: PinCollection | None = None,
    ) -> list[DrcViolation]:
        """Verify that every machine input inserter has reachable upstream supply."""
        violations: list[DrcViolation] = []
        machine_tiles = _collect_machine_tiles(entities)
        container_tiles, supply_tiles = _collect_container_and_supply_tiles(entities)
        in_inserters, out_inserters = _classify_inserters(entities, machine_tiles)

        sources: set[tuple[int, int]] = {dr for _, _, dr in out_inserters} | container_tiles
        if input_pins is not None:
            pin_sources, pin_violations = self._validate_external_pin_loaders(
                input_pins, tile_map, is_input=True
            )
            violations.extend(pin_violations)
            sources.update(pin_sources)
        else:
            sources.update(self._infer_belt_root_sources(tile_map))

        visited = self._bfs_forward_reachable(sources, tile_map)
        valid_direct_sources = visited | machine_tiles | container_tiles
        all_supply = supply_tiles | machine_tiles

        for ix, iy, pickup_tile in in_inserters:
            if pickup_tile not in all_supply:
                violations.append(
                    DrcViolation(
                        code="DRC-05",
                        message=(
                            f"Machine input inserter at ({ix}, {iy}) has no connected "
                            f"supply entity at pickup tile {pickup_tile}"
                        ),
                        x=ix,
                        y=iy,
                    )
                )
            elif pickup_tile not in valid_direct_sources:
                pu_desc = [
                    f"{e.get('name')}(dir={e.get('direction')},type={e.get('type')})"
                    for e in tile_map.get(pickup_tile, ())
                ]
                _, nb_desc = self._describe_tile_neighborhood(pickup_tile, visited, tile_map)
                root_tile, path_len = self._trace_upstream_root(pickup_tile, visited, tile_map)
                root_ents, root_nbs = self._describe_tile_neighborhood(root_tile, visited, tile_map)
                violations.append(
                    DrcViolation(
                        code="DRC-05",
                        message=(
                            f"Machine input inserter at ({ix}, {iy}) is not reachable "
                            f"from upstream supply (pickup at {pickup_tile}, ents={pu_desc}, "
                            f"nbs={nb_desc}, upstream_root={root_tile}(vis={root_tile in visited}, "
                            f"len={path_len}, ents={root_ents}, nbs={root_nbs}))"
                        ),
                        x=ix,
                        y=iy,
                    )
                )
        return violations

    def _reaches_downstream_sink(
        self,
        drop_tile: tuple[int, int],
        known_reaching_sink: set[tuple[int, int]],
        tile_map: TileMap,
    ) -> tuple[bool, tuple[int, int] | None]:
        """Run BFS from drop_tile to any tile in known_reaching_sink and cache reaching path."""
        if drop_tile in known_reaching_sink:
            return True, None
        visited: set[tuple[int, int]] = {drop_tile}
        parent_tile: dict[tuple[int, int], tuple[int, int]] = {}
        queue = collections.deque([drop_tile])
        reached_from: tuple[int, int] | None = None
        dead_end_tile: tuple[int, int] | None = None

        while queue and reached_from is None:
            curr = queue.popleft()
            if curr in known_reaching_sink:
                reached_from = curr
                break
            ents = tile_map.get(curr, ())
            if not ents:
                dead_end_tile = curr
                continue
            reached_from = self._expand_sink_search_tile(
                curr, ents, known_reaching_sink, visited, parent_tile, queue, tile_map
            )

        if reached_from is not None:
            trace_curr: tuple[int, int] | None = reached_from
            while trace_curr is not None and trace_curr not in known_reaching_sink:
                known_reaching_sink.add(trace_curr)
                trace_curr = parent_tile.get(trace_curr)
            return True, None
        return False, dead_end_tile

    def _expand_sink_search_tile(
        self,
        curr: tuple[int, int],
        ents: Sequence[EntityRecord],
        known_reaching_sink: set[tuple[int, int]],
        visited: set[tuple[int, int]],
        parent_tile: dict[tuple[int, int], tuple[int, int]],
        queue: collections.deque[tuple[int, int]],
        tile_map: TileMap,
    ) -> tuple[int, int] | None:
        """Expand one tile during downstream sink search; return reaching tile if sink hit."""
        for e in ents:
            if self._belt_directly_hits_sink(curr, e, known_reaching_sink):
                return curr
            for parent, nxt in self._next_transport_tiles(curr, e, tile_map):
                if nxt in known_reaching_sink:
                    return parent
                if nxt not in visited:
                    visited.add(nxt)
                    parent_tile[nxt] = parent
                    queue.append(nxt)
        return None

    @staticmethod
    def _belt_directly_hits_sink(
        curr: tuple[int, int],
        e: EntityRecord,
        known_reaching_sink: set[tuple[int, int]],
    ) -> bool:
        """Return True if e is a transport belt pointing directly into a known sink tile."""
        if "transport-belt" not in _ent_name(e):
            return False
        e_dx, e_dy = DIR_STEPS.get(_ent_dir(e), (0, 0))
        return (curr[0] + e_dx, curr[1] + e_dy) in known_reaching_sink

    def _check_machine_output_connectivity(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
        output_pins: PinCollection | None = None,
    ) -> list[DrcViolation]:
        """Verify that every machine output inserter drops onto a valid receiver entity and reaches a sink."""
        violations: list[DrcViolation] = []
        machine_tiles = _collect_machine_tiles(entities)
        container_tiles, supply_tiles = _collect_container_and_supply_tiles(entities)
        in_inserters, out_inserters = _classify_inserters(entities, machine_tiles)

        sink_tiles: set[tuple[int, int]] = set(container_tiles) | {pu for _, _, pu in in_inserters}
        if output_pins is not None:
            pin_sinks, pin_violations = self._validate_external_pin_loaders(
                output_pins, tile_map, is_input=False
            )
            violations.extend(pin_violations)
            sink_tiles.update(pin_sinks)

        known_reaching_sink: set[tuple[int, int]] = set(sink_tiles)
        valid_drop_targets = supply_tiles | machine_tiles

        for ix, iy, drop_tile in out_inserters:
            if drop_tile not in valid_drop_targets:
                violations.append(
                    DrcViolation(
                        code="DRC-06",
                        message=(
                            f"Machine output inserter at ({ix}, {iy}) has no connected "
                            f"receiver entity at drop tile {drop_tile}"
                        ),
                        x=ix,
                        y=iy,
                    )
                )
            elif sink_tiles:
                reached, dead_end_tile = self._reaches_downstream_sink(
                    drop_tile, known_reaching_sink, tile_map
                )
                if not reached:
                    violations.append(
                        DrcViolation(
                            code="DRC-06",
                            message=(
                                f"Machine output inserter at ({ix}, {iy}) is not reachable to "
                                f"any downstream consumer, container, or output pin "
                                f"(dead-end near {dead_end_tile})"
                            ),
                            x=ix,
                            y=iy,
                        )
                    )
        return violations

    def _collect_underground_belt_spans(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> list[tuple[tuple[int, int], tuple[int, int], int, list[tuple[int, int]]]]:
        """Collect (in_pos, out_pos, direction, intermediate_tiles) for paired underground belts."""
        spans: list[tuple[tuple[int, int], tuple[int, int], int, list[tuple[int, int]]]] = []
        for u_in in entities:
            if "underground-belt" not in _ent_name(u_in) or u_in.get("type") != "input":
                continue
            matched_ent, out_pos, span = self._find_matching_underground_output(u_in, tile_map)
            if matched_ent is not None:
                x, y = _ent_int_coords(u_in)
                d = _ent_dir(u_in)
                dx, dy = DIR_STEPS.get(d, (0, 0))
                span_tiles = [(x + dx * s, y + dy * s) for s in range(1, span)]
                spans.append(((x, y), out_pos, d, span_tiles))
        return spans

    @staticmethod
    def _check_parallel_span_overlaps(
        spans: Sequence[tuple[tuple[int, int], tuple[int, int], int, list[tuple[int, int]]]],
        code: str,
        entity_kind: str,
    ) -> list[DrcViolation]:
        """Detect parallel horizontal or vertical underground spans sharing intermediate tiles."""
        violations: list[DrcViolation] = []
        axis_maps: dict[str, dict[tuple[int, int], tuple[tuple[int, int], tuple[int, int]]]] = {
            "horizontal": {},
            "vertical": {},
        }
        for in_pos, out_pos, d, span_tiles in spans:
            axis_name = "horizontal" if d in (DIRECTION_EAST, DIRECTION_WEST) else "vertical"
            axis_map = axis_maps[axis_name]
            for st in span_tiles:
                if st in axis_map:
                    other_in, other_out = axis_map[st]
                    violations.append(
                        DrcViolation(
                            code=code,
                            message=(
                                f"Parallel {axis_name} {entity_kind} span collision at {st} "
                                f"between span {in_pos}->{out_pos} and {other_in}->{other_out}"
                            ),
                            x=float(st[0]),
                            y=float(st[1]),
                        )
                    )
                else:
                    axis_map[st] = (in_pos, out_pos)
        return violations

    def _check_underground_span_overlap(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> list[DrcViolation]:
        """Detect underground belt hoods inside active tunnel spans or parallel span overlaps."""
        violations: list[DrcViolation] = []
        spans = self._collect_underground_belt_spans(entities, tile_map)
        for (ix, iy), (ox, oy), _d, span_tiles in spans:
            for sx, sy in span_tiles:
                violations.extend(
                    DrcViolation(
                        code="DRC-07",
                        message=(
                            f"Underground belt hood ({ent.get('type')}) at ({sx}, {sy}) "
                            f"collides with underground tunnel span from ({ix}, {iy}) to ({ox}, {oy})"
                        ),
                        x=sx,
                        y=sy,
                    )
                    for ent in tile_map.get((sx, sy), ())
                    if "underground-belt" in _ent_name(ent)
                )
        violations.extend(
            self._check_parallel_span_overlaps(
                spans, code="DRC-07", entity_kind="underground tunnel"
            )
        )
        return violations

    def _check_surface_collision(
        self,
        tile_map: TileMap,
        unresolved_crossings: PinCollection | None = None,
    ) -> list[DrcViolation]:
        """Detect unresolved surface crossings and multiple entities on the same surface tile (Rule DRC-08)."""
        violations: list[DrcViolation] = []
        if unresolved_crossings:
            violations.extend(
                DrcViolation(
                    code="DRC-08",
                    message=f"Unresolved surface crossing at ({pt[0]}, {pt[1]})",
                    x=float(pt[0]),
                    y=float(pt[1]),
                )
                for pt in unresolved_crossings
            )
        for pos, ents in tile_map.items():
            if len(ents) > 1:
                names = [_ent_name(e) or "unknown" for e in ents]
                violations.append(
                    DrcViolation(
                        code="DRC-08",
                        message=(
                            f"Surface tile collision between {len(ents)} entities {names} "
                            f"at ({pos[0]}, {pos[1]})"
                        ),
                        x=float(pos[0]),
                        y=float(pos[1]),
                    )
                )
        return violations

    def _check_circuit_connectivity(
        self,
        entities: Sequence[EntityRecord],
        wires: Sequence[Sequence[int]],
    ) -> list[DrcViolation]:
        """Verify circuit network connectivity and wire length limits (Rules DRC-09..12)."""
        violations: list[DrcViolation] = []
        ent_by_num = {
            _as_int(ent["entity_number"]): ent for ent in entities if ent.get("entity_number")
        }
        ent_conns: dict[int, set[int]] = collections.defaultdict(set)
        for w in wires:
            if len(w) >= WIRE_TUPLE_MIN_LEN:
                id1, c1 = int(w[0]), int(w[1])
                id2, c2 = int(w[WIRE_SECOND_ENTITY_INDEX]), int(w[WIRE_SECOND_CONN_INDEX])
                ent_conns[id1].add(c1)
                ent_conns[id2].add(c2)
                wire_violation = self._check_single_circuit_wire_reach(id1, id2, ent_by_num)
                if wire_violation is not None:
                    violations.append(wire_violation)

        for ent in entities:
            num = ent.get("entity_number")
            conns = ent_conns.get(_as_int(num), set()) if num else set()
            violations.extend(self._check_entity_circuit_terminals(ent, num, conns))
        return violations

    @staticmethod
    def _check_single_circuit_wire_reach(
        id1: int,
        id2: int,
        ent_by_num: Mapping[int, EntityRecord],
    ) -> DrcViolation | None:
        """Check Rule DRC-09 wire reach limit between two connected entities."""
        e1 = ent_by_num.get(id1)
        e2 = ent_by_num.get(id2)
        if not e1 or not e2:
            return None
        x1, y1 = _ent_coords(e1)
        x2, y2 = _ent_coords(e2)
        dist = math.hypot(x1 - x2, y1 - y2)
        reach_limit = min(
            get_pole_wire_reach(_ent_name(e1), DEFAULT_POLE_WIRE_REACH),
            get_pole_wire_reach(_ent_name(e2), DEFAULT_POLE_WIRE_REACH),
        )
        if dist > reach_limit:
            return DrcViolation(
                code="DRC-09",
                message=(
                    f"Wire distance {dist:.2f} between {_ent_name(e1)} (#{id1}) and "
                    f"{_ent_name(e2)} (#{id2}) exceeds {reach_limit:.1f} tiles limit"
                ),
                x=x1,
                y=y1,
            )
        return None

    def _check_entity_circuit_terminals(
        self,
        ent: EntityRecord,
        num: object,
        conns: set[int],
    ) -> list[DrcViolation]:
        """Verify Rules DRC-10..12 terminal connections on a single combinator or power switch."""
        ename = _ent_name(ent)
        x, y = _ent_coords(ent)
        if ename in ("decider-combinator", "arithmetic-combinator"):
            return self._check_combinator_terminals(ename, num, x, y, conns)
        if ename == "constant-combinator":
            if not (conns & CIRCUIT_INPUT_CONNECTORS):
                return [
                    DrcViolation(
                        code="DRC-11",
                        message=f"Constant combinator (#{num}) at ({x}, {y}) has no wire connections",
                        x=x,
                        y=y,
                    )
                ]
            return []
        if ename == "power-switch":
            return self._check_power_switch_terminals(num, x, y, conns)
        return []

    @staticmethod
    def _check_combinator_terminals(
        ename: str,
        num: object,
        x: float,
        y: float,
        conns: set[int],
    ) -> list[DrcViolation]:
        """Verify Rule DRC-10 input and output connections on an arithmetic or decider combinator."""
        violations: list[DrcViolation] = []
        if not (conns & CIRCUIT_INPUT_CONNECTORS):
            violations.append(
                DrcViolation(
                    code="DRC-10",
                    message=f"Combinator {ename} (#{num}) at ({x}, {y}) has unconnected inputs",
                    x=x,
                    y=y,
                )
            )
        if not (conns & CIRCUIT_OUTPUT_CONNECTORS):
            violations.append(
                DrcViolation(
                    code="DRC-10",
                    message=f"Combinator {ename} (#{num}) at ({x}, {y}) has unconnected outputs",
                    x=x,
                    y=y,
                )
            )
        return violations

    @staticmethod
    def _check_power_switch_terminals(
        num: object,
        x: float,
        y: float,
        conns: set[int],
    ) -> list[DrcViolation]:
        """Verify Rule DRC-12 circuit and copper terminal connections on a power switch."""
        violations: list[DrcViolation] = []
        has_left_cu = COPPER_LEFT_CONNECTOR in conns
        has_right_cu = COPPER_RIGHT_CONNECTOR in conns
        if not (conns & CIRCUIT_INPUT_CONNECTORS):
            violations.append(
                DrcViolation(
                    code="DRC-12",
                    message=f"Power switch (#{num}) at ({x}, {y}) has no circuit wire connection",
                    x=x,
                    y=y,
                )
            )
        if not (has_left_cu and has_right_cu):
            violations.append(
                DrcViolation(
                    code="DRC-12",
                    message=(
                        f"Power switch (#{num}) at ({x}, {y}) missing copper terminal "
                        f"connections (left={has_left_cu}, right={has_right_cu})"
                    ),
                    x=x,
                    y=y,
                )
            )
        return violations

    @classmethod
    def _resolve_entity_fluid(
        cls,
        ent: EntityRecord,
        known_fluids: set[str],
        tile_to_nets: Mapping[tuple[float, float], set[str]] | None,
        net_map: Mapping[object, object] | None,
    ) -> str | None:
        """Resolve the fluid carried by an entity from direct tags, tile_to_nets, or net_map."""
        fluid = ent.get("fluid")
        if fluid:
            return str(fluid)
        cx, cy = _ent_coords(ent)
        itile = (math.floor(cx), math.floor(cy))
        if tile_to_nets:
            nets = tile_to_nets.get((cx, cy), set()) or tile_to_nets.get(itile, set())
            for n in nets:
                mat = n.split("__")[0].replace("_", "-")
                if mat in known_fluids:
                    return mat
        if net_map:
            return cls._resolve_fluid_from_net_map(
                net_map.get(itile) or net_map.get((cx, cy)), known_fluids
            )
        return None

    @staticmethod
    def _resolve_fluid_from_net_map(net_val: object, known_fluids: set[str]) -> str | None:
        """Extract fluid material name from a net_map entry if it matches known_fluids."""
        if isinstance(net_val, str):
            mat = net_val.split("__")[0].replace("_", "-")
            return mat if mat in known_fluids else None
        if isinstance(net_val, Mapping):
            mat = str(net_val.get("material", "")).replace("_", "-")
            return mat if mat in known_fluids else None
        return None

    def _check_fluid_mixing(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
        tile_to_nets: Mapping[tuple[float, float], set[str]] | None = None,
        fluids: set[str] | frozenset[str] | None = None,
        net_map: Mapping[object, object] | None = None,
    ) -> list[DrcViolation]:
        """Detect adjacent surface pipes or pipe connections carrying different fluids (Rule DRC-13)."""
        violations: list[DrcViolation] = []
        known_fluids = set(fluids) if fluids else set()
        tile_fluids = self._build_tile_fluid_map(entities, known_fluids, tile_to_nets, net_map)
        reported_pairs: set[tuple[tuple[int, int], tuple[int, int]]] = set()

        for ent in entities:
            if _ent_name(ent) != "pipe":
                continue
            pos = _ent_floor_coords(ent)
            f1 = tile_fluids.get(pos)
            if f1:
                violations.extend(
                    self._check_pipe_cardinal_mixing(pos, f1, tile_fluids, tile_map, reported_pairs)
                )
        return violations

    def _build_tile_fluid_map(
        self,
        entities: Sequence[EntityRecord],
        known_fluids: set[str],
        tile_to_nets: Mapping[tuple[float, float], set[str]] | None,
        net_map: Mapping[object, object] | None,
    ) -> dict[tuple[int, int], str]:
        """Map integer tile coordinates to their resolved fluid name."""
        tile_fluids: dict[tuple[int, int], str] = {}
        for ent in entities:
            resolved = self._resolve_entity_fluid(ent, known_fluids, tile_to_nets, net_map)
            if resolved:
                tile_fluids[_ent_floor_coords(ent)] = resolved
        return tile_fluids

    def _check_pipe_cardinal_mixing(
        self,
        pos: tuple[int, int],
        f1: str,
        tile_fluids: Mapping[tuple[int, int], str],
        tile_map: TileMap,
        reported_pairs: set[tuple[tuple[int, int], tuple[int, int]]],
    ) -> list[DrcViolation]:
        """Check all 4 cardinal neighbors of pipe at pos for fluid mixing."""
        violations: list[DrcViolation] = []
        x, y = pos
        for dx, dy in CARDINAL_STEPS:
            nb_tile = (x + dx, y + dy)
            pair_key = (min(pos, nb_tile), max(pos, nb_tile))
            f2 = tile_fluids.get(nb_tile)
            if pair_key in reported_pairs or not f2 or f1 == f2:
                continue
            violation = self._check_neighbor_pipe_mixing(
                pos, f1, nb_tile, f2, tile_map.get(nb_tile, ())
            )
            if violation is not None:
                reported_pairs.add(pair_key)
                violations.append(violation)
        return violations

    @staticmethod
    def _check_neighbor_pipe_mixing(
        pos: tuple[int, int],
        f1: str,
        nb_tile: tuple[int, int],
        f2: str,
        nb_ents: Sequence[EntityRecord],
    ) -> DrcViolation | None:
        """Check whether any pipe entity on nb_tile mixes fluid f2 into pipe at pos carrying f1."""
        x, y = pos
        for nb in nb_ents:
            nb_name = _ent_name(nb)
            if nb_name == "pipe":
                return DrcViolation(
                    code="DRC-13",
                    message=(
                        f"Fluid mixing: adjacent pipes carry different fluids "
                        f"'{f1}' at ({x}, {y}) and '{f2}' at {nb_tile}"
                    ),
                    x=float(x),
                    y=float(y),
                )
            if nb_name == "pipe-to-ground":
                surf_dx, surf_dy = DIR_STEPS.get(_ent_dir(nb), (0, 0))
                if (nb_tile[0] + surf_dx, nb_tile[1] + surf_dy) == pos:
                    return DrcViolation(
                        code="DRC-13",
                        message=(
                            f"Fluid mixing: pipe with fluid '{f1}' at ({x}, {y}) "
                            f"connects into pipe-to-ground with fluid '{f2}' at {nb_tile}"
                        ),
                        x=float(x),
                        y=float(y),
                    )
        return None

    def _pair_pipes_to_ground(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> tuple[
        list[EntityRecord],
        list[tuple[tuple[int, int], tuple[int, int], int, list[tuple[int, int]]]],
        int,
    ]:
        """Match pipe-to-ground pairs and return (unpaired_pipes, paired_spans, max_span)."""
        pipes = [e for e in entities if _ent_name(e) == "pipe-to-ground"]
        max_span = get_underground_span("pipe-to-ground", 10)
        matched: set[int] = set()
        unpaired: list[EntityRecord] = []
        spans: list[tuple[tuple[int, int], tuple[int, int], int, list[tuple[int, int]]]] = []

        for p in pipes:
            if id(p) in matched:
                continue
            found, found_pos, found_span = self._find_matching_pipe_partner(
                p, max_span, matched, tile_map
            )
            if found is not None and found_pos is not None:
                matched.add(id(p))
                matched.add(id(found))
                px, py = _ent_floor_coords(p)
                ug_d = OPPOSITE_DIR.get(_ent_dir(p), _ent_dir(p))
                dx, dy = DIR_STEPS.get(ug_d, (0, 0))
                span_tiles = [(px + dx * s, py + dy * s) for s in range(1, found_span)]
                spans.append(((px, py), found_pos, ug_d, span_tiles))
            else:
                unpaired.append(p)
        return unpaired, spans, max_span

    @staticmethod
    def _find_matching_pipe_partner(
        p: EntityRecord,
        max_span: int,
        matched: set[int],
        tile_map: TileMap,
    ) -> tuple[EntityRecord | None, tuple[int, int] | None, int]:
        """Find the nearest unmatched opposing pipe-to-ground within max_span."""
        px, py = _ent_floor_coords(p)
        d = _ent_dir(p)
        ug_d = OPPOSITE_DIR.get(d, d)
        dx, dy = DIR_STEPS.get(ug_d, (0, 0))
        for span in range(1, max_span + 1):
            chk_pos = (px + dx * span, py + dy * span)
            for c in tile_map.get(chk_pos, ()):
                if (
                    _ent_name(c) == "pipe-to-ground"
                    and _ent_dir(c) == ug_d
                    and id(c) not in matched
                ):
                    return c, chk_pos, span
        return None, None, 0

    def _check_pipe_pairing(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> list[DrcViolation]:
        """Verify that every pipe-to-ground entity pairs with a facing pipe within maximum span (Rule DRC-14)."""
        unpaired, _, max_span = self._pair_pipes_to_ground(entities, tile_map)
        violations: list[DrcViolation] = []
        for p in unpaired:
            px, py = _ent_floor_coords(p)
            d = _ent_dir(p)
            violations.append(
                DrcViolation(
                    code="DRC-14",
                    message=(
                        f"Pipe-to-ground at ({px}, {py}) facing {d} has no matching "
                        f"facing pipe-to-ground within max span {max_span}"
                    ),
                    x=float(px),
                    y=float(py),
                )
            )
        return violations

    def _check_pipe_span_overlap(
        self,
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
    ) -> list[DrcViolation]:
        """Detect pipe-to-ground hoods inside active underground spans or parallel span overlaps (Rule DRC-15)."""
        _, spans, _ = self._pair_pipes_to_ground(entities, tile_map)
        violations: list[DrcViolation] = []
        for (ix, iy), (ox, oy), _d, span_tiles in spans:
            for sx, sy in span_tiles:
                violations.extend(
                    DrcViolation(
                        code="DRC-15",
                        message=(
                            f"Pipe-to-ground at ({sx}, {sy}) collides with "
                            f"underground pipe span from ({ix}, {iy}) to ({ox}, {oy})"
                        ),
                        x=float(sx),
                        y=float(sy),
                    )
                    for ent in tile_map.get((sx, sy), ())
                    if _ent_name(ent) == "pipe-to-ground"
                )
        violations.extend(
            self._check_parallel_span_overlaps(spans, code="DRC-15", entity_kind="underground pipe")
        )
        return violations

    def _check_pipeline_extent(
        self,
        entities: Sequence[EntityRecord],
        _tile_map: TileMap,
    ) -> list[DrcViolation]:
        """Verify that every connected fluid network does not exceed maximum pipeline extent (Rule DRC-16)."""
        violations: list[DrcViolation] = []
        max_limit = getattr(self, "max_pipeline_extent", DEFAULT_MAX_PIPELINE_EXTENT)
        adj, _, _ = build_conduit_graph([dict(e) for e in entities])
        for comp in find_fluid_components(adj):
            ext, min_x, max_x, min_y, max_y = component_extent(comp)
            if ext > max_limit:
                violations.append(
                    DrcViolation(
                        code="DRC-16",
                        message=(
                            f"Pipeline extent {ext:.1f} tiles exceeds maximum allowed extent "
                            f"{max_limit} tiles for connected fluid network (span: "
                            f"x=[{min_x:.1f}, {max_x:.1f}], y=[{min_y:.1f}, {max_y:.1f}], "
                            f"nodes={len(comp)})"
                        ),
                        x=min_x,
                        y=min_y,
                    )
                )
        return violations

    @staticmethod
    def _index_entities_by_number(
        entities: Sequence[EntityRecord],
    ) -> tuple[dict[int, EntityRecord], list[DrcViolation]]:
        """Map entity_number to entity and report duplicate entity_number violations."""
        ent_by_num: dict[int, EntityRecord] = {}
        violations: list[DrcViolation] = []
        for ent in entities:
            num = ent.get("entity_number")
            if num is None:
                continue
            inum = _as_int(num)
            if inum in ent_by_num:
                x, y = _ent_coords(ent)
                prev = ent_by_num[inum]
                violations.append(
                    DrcViolation(
                        code="DRC-17",
                        message=(
                            f"Duplicate entity_number #{inum} shared by {_ent_name(ent)} "
                            f"at ({x}, {y}) and {_ent_name(prev)}"
                        ),
                        x=x,
                        y=y,
                    )
                )
            else:
                ent_by_num[inum] = ent
        return ent_by_num, violations

    def _build_copper_wire_graph(
        self,
        wires: Sequence[Sequence[int]],
        ent_by_num: Mapping[int, EntityRecord],
    ) -> tuple[dict[int, set[int]], list[DrcViolation]]:
        """Validate copper wires and build adjacency graph across poles and power switches."""
        copper_adj: dict[int, set[int]] = collections.defaultdict(set)
        violations: list[DrcViolation] = []
        for w in wires:
            if len(w) < WIRE_TUPLE_MIN_LEN:
                continue
            id1, c1 = int(w[0]), int(w[1])
            id2, c2 = int(w[WIRE_SECOND_ENTITY_INDEX]), int(w[WIRE_SECOND_CONN_INDEX])
            if c1 not in COPPER_CONNECTORS and c2 not in COPPER_CONNECTORS:
                continue
            v = self._validate_copper_wire_edge(id1, c1, id2, c2, ent_by_num)
            if v is not None:
                violations.append(v)
            else:
                copper_adj[id1].add(id2)
                copper_adj[id2].add(id1)
        return copper_adj, violations

    @classmethod
    def _validate_copper_wire_edge(
        cls,
        id1: int,
        c1: int,
        id2: int,
        c2: int,
        ent_by_num: Mapping[int, EntityRecord],
    ) -> DrcViolation | None:
        """Validate endpoints and reach of a single copper wire connection."""
        e1 = ent_by_num.get(id1)
        e2 = ent_by_num.get(id2)
        if not cls._is_valid_copper_endpoint(e1) or not cls._is_valid_copper_endpoint(e2):
            n1 = _ent_name(e1) if e1 else "missing"
            n2 = _ent_name(e2) if e2 else "missing"
            bad_ent = e1 or e2
            bx, by = _ent_coords(bad_ent) if bad_ent else (0.0, 0.0)
            return DrcViolation(
                code="DRC-17",
                message=(
                    f"Copper wire [{id1}, {c1}, {id2}, {c2}] connects invalid "
                    f"endpoint(s) ({n1} #{id1}, {n2} #{id2})"
                ),
                x=bx,
                y=by,
            )
        assert e1 is not None and e2 is not None
        n1, n2 = _ent_name(e1), _ent_name(e2)
        x1, y1 = _ent_coords(e1)
        x2, y2 = _ent_coords(e2)
        w_dist = math.hypot(x1 - x2, y1 - y2)
        max_wire = min(get_pole_wire_reach(n1), get_pole_wire_reach(n2))
        if w_dist > max_wire + WIRE_DISTANCE_TOLERANCE:
            return DrcViolation(
                code="DRC-17",
                message=(
                    f"Copper wire [{id1}, {c1}, {id2}, {c2}] length {w_dist:.2f} "
                    f"exceeds maximum wire reach {max_wire:.1f} between "
                    f"{n1} #{id1} and {n2} #{id2}"
                ),
                x=x1,
                y=y1,
            )
        return None

    @staticmethod
    def _is_valid_copper_endpoint(ent: EntityRecord | None) -> bool:
        """Return True if ent is an electric pole or power switch."""
        if ent is None:
            return False
        name = _ent_name(ent)
        return is_electric_pole(name) or name == "power-switch"

    @classmethod
    def _find_main_pdn_poles(
        cls,
        numbered_poles: Sequence[EntityRecord],
        copper_adj: Mapping[int, set[int]],
        ent_by_num: Mapping[int, EntityRecord],
    ) -> set[int]:
        """Return pole entity_numbers belonging to the largest connected copper component."""
        visited: set[int] = set()
        best_comp: set[int] = set()
        for p in numbered_poles:
            pnum = _as_int(p.get("entity_number"))
            if pnum in visited:
                continue
            comp_poles = cls._bfs_copper_pole_component(pnum, visited, copper_adj, ent_by_num)
            if len(comp_poles) > len(best_comp):
                best_comp = comp_poles
        return best_comp

    @staticmethod
    def _bfs_copper_pole_component(
        start_num: int,
        visited: set[int],
        copper_adj: Mapping[int, set[int]],
        ent_by_num: Mapping[int, EntityRecord],
    ) -> set[int]:
        """Run BFS over copper_adj from start_num and return all pole IDs in the component."""
        comp_poles: set[int] = set()
        queue = collections.deque([start_num])
        visited.add(start_num)
        while queue:
            curr = queue.popleft()
            curr_ent = ent_by_num.get(curr)
            if curr_ent and is_electric_pole(_ent_name(curr_ent)):
                comp_poles.add(curr)
            for nb in copper_adj.get(curr, ()):
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        return comp_poles

    @staticmethod
    def _find_covering_poles(
        ent: EntityRecord,
        pole_buckets: Mapping[tuple[int, int], list[tuple[EntityRecord, float, float, float]]],
        max_pole_radius: float,
    ) -> list[EntityRecord]:
        """Return all electric poles whose supply area covers consumer ent."""
        ename = _ent_name(ent)
        ex, ey = _ent_coords(ent)
        cx, cy = entity_collision_half_size(ename, _ent_dir(ent))
        search_r = max_pole_radius + max(cx, cy)
        min_bx = int((ex - search_r) // POLE_BUCKET_SIZE)
        max_bx = int((ex + search_r) // POLE_BUCKET_SIZE)
        min_by = int((ey - search_r) // POLE_BUCKET_SIZE)
        max_by = int((ey + search_r) // POLE_BUCKET_SIZE)
        buckets = [(bx, by) for bx in range(min_bx, max_bx + 1) for by in range(min_by, max_by + 1)]
        covering: list[EntityRecord] = []
        for bkey in buckets:
            for p, px, py, p_radius in pole_buckets.get(bkey, ()):
                max_reach = p_radius + max(cx, cy)
                if abs(ex - px) <= max_reach and abs(ey - py) <= max_reach:
                    dx = max(0.0, abs(ex - px) - cx)
                    dy = max(0.0, abs(ey - py) - cy)
                    if max(dx, dy) <= p_radius:
                        covering.append(p)
        return covering

    def _check_power_coverage(
        self,
        entities: Sequence[EntityRecord],
        wires: Sequence[Sequence[int]],
    ) -> list[DrcViolation]:
        """Verify electric pole coverage and copper PDN connectivity (Rule DRC-17)."""
        ent_by_num, violations = self._index_entities_by_number(entities)
        poles = [e for e in entities if is_electric_pole(_ent_name(e))]
        if not poles:
            return violations

        copper_adj, wire_violations = self._build_copper_wire_graph(wires, ent_by_num)
        violations.extend(wire_violations)

        numbered_poles = [p for p in poles if p.get("entity_number") is not None]
        main_pole_ids = (
            self._find_main_pdn_poles(numbered_poles, copper_adj, ent_by_num)
            if wires and len(numbered_poles) > 1
            else set()
        )
        pole_buckets, max_pole_radius = self._build_pole_buckets(poles)

        for ent in entities:
            if is_electric_consumer(_ent_name(ent)):
                v = self._check_single_consumer_power(
                    ent, pole_buckets, max_pole_radius, main_pole_ids
                )
                if v is not None:
                    violations.append(v)
        return violations

    @staticmethod
    def _build_pole_buckets(
        poles: Sequence[EntityRecord],
    ) -> tuple[dict[tuple[int, int], list[tuple[EntityRecord, float, float, float]]], float]:
        """Bucket electric poles spatially and return (pole_buckets, max_pole_radius)."""
        pole_buckets: dict[tuple[int, int], list[tuple[EntityRecord, float, float, float]]] = {}
        max_pole_radius = 0.0
        for p in poles:
            px, py = _ent_coords(p)
            p_radius = get_pole_supply_radius(_ent_name(p))
            max_pole_radius = max(max_pole_radius, p_radius)
            bucket = (int(px // POLE_BUCKET_SIZE), int(py // POLE_BUCKET_SIZE))
            pole_buckets.setdefault(bucket, []).append((p, px, py, p_radius))
        return pole_buckets, max_pole_radius

    def _check_single_consumer_power(
        self,
        ent: EntityRecord,
        pole_buckets: Mapping[tuple[int, int], list[tuple[EntityRecord, float, float, float]]],
        max_pole_radius: float,
        main_pole_ids: set[int],
    ) -> DrcViolation | None:
        """Check whether consumer ent is covered by a powered pole on the main PDN."""
        ename = _ent_name(ent)
        ex, ey = _ent_coords(ent)
        covering_poles = self._find_covering_poles(ent, pole_buckets, max_pole_radius)
        if not covering_poles:
            return DrcViolation(
                code="DRC-17",
                message=(
                    f"Unpowered entity {ename} at ({ex}, {ey}) is outside electric pole supply area"
                ),
                x=ex,
                y=ey,
            )
        if main_pole_ids and not any(
            _as_int(p.get("entity_number")) in main_pole_ids
            for p in covering_poles
            if p.get("entity_number") is not None
        ):
            return DrcViolation(
                code="DRC-17",
                message=(
                    f"Entity {ename} at ({ex}, {ey}) is covered only by "
                    f"disconnected electric pole(s) not wired to the main PDN"
                ),
                x=ex,
                y=ey,
            )
        return None

    @staticmethod
    def _build_chassis_geom(
        macro_floorplan: object,
        def_data: Mapping[str, object] | None,
        bay_blocks: set[tuple[int, int]] | None,
    ) -> _ChassisGeom:
        """Extract runtime chassis geometry parameters from floorplan and DEF data."""
        chassis_obj = getattr(macro_floorplan, "chassis", None)
        pitch = int(getattr(chassis_obj, "pitch_x", DEFAULT_CHASSIS_PITCH))
        margin = int(getattr(chassis_obj, "core_margin_x", DEFAULT_CHASSIS_MARGIN))
        chan_w = int(getattr(chassis_obj, "channel_width", 2 * margin))
        grid_w = int(getattr(macro_floorplan, "grid_w", 1))
        grid_h = int(getattr(macro_floorplan, "grid_h", 1))
        if def_data and (grid_w * grid_h <= 1):
            units = _as_float(def_data.get("units", DEFAULT_DEF_UNITS), DEFAULT_DEF_UNITS)
            die_area = def_data.get("die_area", [0, 0, pitch * units, pitch * units])
            if isinstance(die_area, Sequence) and len(die_area) > DIE_AREA_HEIGHT_INDEX:
                die_w = _as_float(die_area[DIE_AREA_WIDTH_INDEX]) / units
                die_h = _as_float(die_area[DIE_AREA_HEIGHT_INDEX]) / units
                grid_w = max(1, round(die_w / float(pitch)))
                grid_h = max(1, round(die_h / float(pitch)))
        return _ChassisGeom(
            grid_w=grid_w,
            grid_h=grid_h,
            pitch=pitch,
            margin=margin,
            chan_w=chan_w,
            active_bays=bay_blocks or set(),
        )

    @staticmethod
    def _in_switchbox(tx: int, ty: int, geom: _ChassisGeom) -> bool:
        """Return True if tile (tx, ty) lies inside a 6x6 corner or bay switchbox."""
        if (tx + geom.margin) % geom.pitch >= geom.chan_w:
            return False
        if (ty + geom.margin) % geom.pitch < geom.chan_w:
            return True
        if geom.margin <= (ty % geom.pitch) <= BAY_STREET_MAX_RY:
            ix = (tx + geom.margin) // geom.pitch
            by_i = ty // geom.pitch
            return (ix, by_i) in geom.active_bays or (ix - 1, by_i) in geom.active_bays
        return False

    @staticmethod
    def _street_seg(
        coord: tuple[int, int],
        geom: _ChassisGeom,
    ) -> tuple[str, int, int] | None:
        """Classify a channel coordinate into its ('H'|'V', x_idx, y_idx) street segment."""
        x, y = coord
        bx, by = x // geom.pitch, y // geom.pitch
        rx, ry = x % geom.pitch, y % geom.pitch
        if (
            (bx, by) in geom.active_bays
            and BAY_STREET_MIN_RX <= rx <= BAY_STREET_MAX_RX
            and geom.margin <= ry <= BAY_STREET_MAX_RY
        ):
            return ("H", bx, by)
        in_v = (x + geom.margin) % geom.pitch < geom.chan_w
        in_h = (y + geom.margin) % geom.pitch < geom.chan_w
        if in_v and not in_h and 0 <= by < geom.grid_h:
            return ("V", (x + geom.margin) // geom.pitch, by)
        if in_h and not in_v and 0 <= bx < geom.grid_w:
            return ("H", bx, (y + geom.margin) // geom.pitch)
        return None

    @staticmethod
    def _core_block(cx: int, cy: int, geom: _ChassisGeom) -> tuple[int, int] | None:
        """Return (bx, by) core block indices if (cx, cy) lies inside a block core."""
        bx, by = cx // geom.pitch, cy // geom.pitch
        if not (0 <= bx < geom.grid_w and 0 <= by < geom.grid_h):
            return None
        rx, ry = cx % geom.pitch, cy % geom.pitch
        min_ry = BAY_CORE_MIN_RY if (bx, by) in geom.active_bays else geom.margin
        max_coord = geom.pitch - 1 - geom.margin
        if geom.margin <= rx <= max_coord and min_ry <= ry <= max_coord:
            return (bx, by)
        return None

    @classmethod
    def _expand_route_coords(cls, r_obj: object) -> list[tuple[int, int]]:
        """Expand a router route object or path into contiguous integer tile coordinates."""
        r_nodes = getattr(r_obj, "nodes", None)
        raw_pts = (
            [(int(n.x), int(n.y)) for n in r_nodes]
            if r_nodes is not None
            else [(int(pt[0]), int(pt[1])) for pt in getattr(r_obj, "path", [])]
        )
        if len(raw_pts) <= 1:
            return raw_pts
        expanded: list[tuple[int, int]] = [raw_pts[0]]
        for p0, p1 in itertools.pairwise(raw_pts):
            expanded.extend(cls._interpolate_segment(p0, p1))
        return expanded

    @staticmethod
    def _interpolate_segment(
        p0: tuple[int, int],
        p1: tuple[int, int],
    ) -> list[tuple[int, int]]:
        """Interpolate orthogonal steps from p0 (exclusive) to p1 (inclusive)."""
        x0, y0 = p0
        x1, y1 = p1
        if x0 == x1 and y0 != y1:
            step_y = 1 if y1 > y0 else -1
            return [(x0, cy_i) for cy_i in range(y0 + step_y, y1 + step_y, step_y)]
        if y0 == y1 and x0 != x1:
            step_x = 1 if x1 > x0 else -1
            return [(cx_i, y0) for cx_i in range(x0 + step_x, x1 + step_x, step_x)]
        return [(x1, y1)]

    def _check_port_tap_geometry(
        self,
        block_ports: Sequence[object],
        entities: Sequence[EntityRecord],
        tile_map: TileMap,
        geom: _ChassisGeom,
    ) -> tuple[set[tuple[int, int]], list[DrcViolation]]:
        """Validate Rule 1 (PORT_TAP_GEOMETRY) across all block ports."""
        port_channel_coords: set[tuple[int, int]] = set()
        violations: list[DrcViolation] = []
        max_lat = geom.pitch - 1 - geom.margin - 2
        for port in block_ports:
            cx, cy = getattr(port, "core_pos", (0, 0))
            hx, hy = getattr(port, "channel_pos", (0, 0))
            net_name = str(getattr(port, "net_name", ""))
            edge = str(getattr(port, "edge", ""))
            block = getattr(port, "block", (0, 0))
            port_channel_coords.add((hx, hy))
            if abs(cx - hx) + abs(cy - hy) != 1:
                violations.append(
                    DrcViolation(
                        code="PORT_TAP_GEOMETRY",
                        message=(
                            f"Port on {net_name} at {(cx, cy)} -> {(hx, hy)} "
                            f"is not an orthogonal 1-tile step across the cell wall"
                        ),
                        x=float(cx),
                        y=float(cy),
                        net=net_name,
                    )
                )
            if not self._is_valid_port_wall_coord(cx, cy, hx, hy, edge, block, max_lat, geom):
                violations.append(
                    DrcViolation(
                        code="PORT_TAP_GEOMETRY",
                        message=(
                            f"Port on {net_name} at {(cx, cy)} (edge {edge} of "
                            f"block {block}) violates wall coordinate bounds"
                        ),
                        x=float(cx),
                        y=float(cy),
                        net=net_name,
                    )
                )
            if entities and not self._has_valid_port_tap_entity(port, tile_map.get((cx, cy), ())):
                violations.append(
                    DrcViolation(
                        code="PORT_TAP_GEOMETRY",
                        message=(
                            f"Port on {net_name} at {(cx, cy)} is missing a "
                            f"perpendicular surface tap entity with heading {getattr(port, 'heading', 0)}"
                        ),
                        x=float(cx),
                        y=float(cy),
                        net=net_name,
                    )
                )
        return port_channel_coords, violations

    @staticmethod
    def _is_valid_port_wall_coord(
        cx: int,
        cy: int,
        hx: int,
        hy: int,
        edge: str,
        block: object,
        max_lat: int,
        geom: _ChassisGeom,
    ) -> bool:
        """Return True if port core/channel coordinates satisfy lateral wall bounds."""
        if edge in ("N", "S"):
            if block in geom.active_bays and edge == "N":
                return False
            lat = cx % geom.pitch
            return (geom.margin + 2) <= lat <= max_lat and cx == hx
        lat = cy % geom.pitch
        min_lat = BAY_PORT_MIN_LAT if block in geom.active_bays else (geom.margin + 2)
        return min_lat <= lat <= max_lat and cy == hy

    @staticmethod
    def _has_valid_port_tap_entity(port: object, core_ents: Sequence[EntityRecord]) -> bool:
        """Return True if core_pos contains the required pipe or oriented belt tap."""
        if getattr(port, "is_fluid", False):
            return any("pipe" in _ent_name(e) for e in core_ents)
        heading = int(getattr(port, "heading", DIRECTION_NORTH))
        return any(_ent_name(e) == "transport-belt" and _ent_dir(e) == heading for e in core_ents)

    def _check_floorplan_route_discipline(
        self,
        route_objs: Sequence[object],
        router_routes: Mapping[str, object] | None,
        checked_hwy_names: set[str],
        geom: _ChassisGeom,
    ) -> list[DrcViolation]:
        """Check NO_OPPOSITE_WALL_SHORTCUT and SWITCHBOX_ROUTING_DISCIPLINE on floorplan routes."""
        violations: list[DrcViolation] = []
        for route in route_objs:
            s_port = getattr(route, "source_port", None)
            if s_port is None:
                continue
            r_name = str(getattr(route, "net_name", ""))
            s_pos = tuple(getattr(s_port, "channel_pos", (0, 0)))
            s_seg = self._street_seg(s_pos, geom)
            for d_port in getattr(route, "target_ports", ()):
                d_pos = tuple(getattr(d_port, "channel_pos", (0, 0)))
                violations.extend(
                    self._check_route_target_pair(
                        r_name, s_pos, s_seg, d_pos, router_routes, checked_hwy_names, geom
                    )
                )
        return violations

    def _check_route_target_pair(
        self,
        r_name: str,
        s_pos: tuple[int, int],
        s_seg: tuple[str, int, int] | None,
        d_pos: tuple[int, int],
        router_routes: Mapping[str, object] | None,
        checked_hwy_names: set[str],
        geom: _ChassisGeom,
    ) -> list[DrcViolation]:
        """Check shortcut and switchbox discipline for a single (source_port, target_port) pair."""
        violations: list[DrcViolation] = []
        d_seg = self._street_seg(d_pos, geom)
        if s_seg is not None and s_seg == d_seg:
            violations.append(
                DrcViolation(
                    code="NO_OPPOSITE_WALL_SHORTCUT",
                    message=(
                        f"Inter-block route {r_name} connects ports on the same "
                        f"street segment {s_seg} ({s_pos} -> {d_pos})"
                    ),
                    x=float(s_pos[0]),
                    y=float(s_pos[1]),
                    net=r_name,
                )
            )
        for cand_name in (r_name, f"{r_name}_hwy"):
            v = self._check_single_highway_switchbox(
                cand_name, s_pos, d_pos, "Highway trunk", router_routes, checked_hwy_names, geom
            )
            if v is not None:
                violations.append(v)
        return violations

    def _check_highway_spec_discipline(
        self,
        highway_specs: Sequence[object],
        port_channel_coords: set[tuple[int, int]],
        router_routes: Mapping[str, object] | None,
        checked_hwy_names: set[str],
        geom: _ChassisGeom,
    ) -> list[DrcViolation]:
        """Check NO_OPPOSITE_WALL_SHORTCUT and SWITCHBOX_ROUTING_DISCIPLINE on highway specs."""
        violations: list[DrcViolation] = []
        for h_spec in highway_specs:
            h_name = str(getattr(h_spec, "name", ""))
            s_pos = tuple(getattr(h_spec, "source", (0, 0)))
            d_pos = tuple(getattr(h_spec, "sink", (0, 0)))
            if s_pos not in port_channel_coords and d_pos not in port_channel_coords:
                continue
            s_seg = self._street_seg(s_pos, geom)
            d_seg = self._street_seg(d_pos, geom)
            if s_seg is not None and s_seg == d_seg:
                violations.append(
                    DrcViolation(
                        code="NO_OPPOSITE_WALL_SHORTCUT",
                        message=(
                            f"Highway net {h_name} connects endpoints on the same "
                            f"street segment {s_seg} ({s_pos} -> {d_pos})"
                        ),
                        x=float(s_pos[0]),
                        y=float(s_pos[1]),
                        net=h_name,
                    )
                )
            v = self._check_single_highway_switchbox(
                h_name, s_pos, d_pos, "Highway net", router_routes, checked_hwy_names, geom
            )
            if v is not None:
                violations.append(v)
        return violations

    def _check_single_highway_switchbox(
        self,
        h_name: str,
        s_pos: tuple[int, int],
        d_pos: tuple[int, int],
        label: str,
        router_routes: Mapping[str, object] | None,
        checked_hwy_names: set[str],
        geom: _ChassisGeom,
    ) -> DrcViolation | None:
        """Verify that a routed highway net passes through at least one switchbox."""
        if not router_routes or h_name not in router_routes or h_name in checked_hwy_names:
            return None
        checked_hwy_names.add(h_name)
        coords = self._expand_route_coords(router_routes[h_name])
        if (
            "_hwy_mst_" in h_name
            and coords
            and not any(self._in_switchbox(tx, ty, geom) for tx, ty in coords)
        ):
            mst_prefix = h_name.rsplit("_hwy_mst_", 1)[0] + "_hwy_mst_"
            for k_name, k_route in router_routes.items():
                if k_name != h_name and k_name.startswith(mst_prefix):
                    coords.extend(self._expand_route_coords(k_route))
        if coords and not any(self._in_switchbox(tx, ty, geom) for tx, ty in coords):
            return DrcViolation(
                code="SWITCHBOX_ROUTING_DISCIPLINE",
                message=(
                    f"{label} {h_name} ({s_pos} -> {d_pos}) "
                    f"does not pass through any 6x6 corner switchbox"
                ),
                x=float(s_pos[0]),
                y=float(s_pos[1]),
                net=h_name,
            )
        return None

    @staticmethod
    def _extract_named_records(
        container: Mapping[str, object] | None,
        key: str,
    ) -> dict[str, Mapping[str, object]]:
        """Extract a list of named mapping records from container[key] into a dict by name."""
        if not container:
            return {}
        raw_items = container.get(key, ())
        if not isinstance(raw_items, Sequence):
            return {}
        return {
            str(item["name"]): item
            for item in raw_items
            if isinstance(item, Mapping) and "name" in item
        }

    @staticmethod
    def _extract_def_nets(def_data: Mapping[str, object]) -> list[Mapping[str, object]]:
        """Extract list of net mappings from def_data."""
        raw_nets = def_data.get("nets", ())
        if not isinstance(raw_nets, Sequence):
            return []
        return [n for n in raw_nets if isinstance(n, Mapping)]

    @staticmethod
    def _extract_net_connections(net: Mapping[str, object]) -> list[tuple[str, str]]:
        """Extract (instance_name, port_name) pairs from a DEF net mapping."""
        raw_conns = net.get("connections", ())
        if not isinstance(raw_conns, Sequence):
            return []
        return [
            (str(pair[0]), str(pair[1]))
            for pair in raw_conns
            if isinstance(pair, Sequence) and len(pair) >= 2
        ]

    def _check_self_contained_cell_trees(
        self,
        def_data: Mapping[str, object],
        netlist_data: Mapping[str, object] | None,
        geom: _ChassisGeom,
    ) -> list[DrcViolation]:
        """Verify Rule 4 (CELL_SELF_CONTAINED_TREES) so splitters and mergers stay inside one cell."""
        scale = 1.0 / _as_float(def_data.get("units", DEFAULT_DEF_UNITS), DEFAULT_DEF_UNITS)
        comp_map = self._extract_named_records(def_data, "components")
        inst_map = self._extract_named_records(netlist_data, "instances")
        raw_cells = (netlist_data or {}).get("cells", {})
        cells_map: Mapping[str, Mapping[str, object]] = (
            raw_cells if isinstance(raw_cells, Mapping) else {}
        )

        comp_block: dict[str, tuple[int, int] | None] = {}
        comp_pos: dict[str, tuple[int, int]] = {}
        for cname, comp in comp_map.items():
            cx = round(_as_float(comp.get("x")) * scale)
            cy = round(_as_float(comp.get("y")) * scale)
            comp_pos[cname] = (cx, cy)
            comp_block[cname] = self._core_block(cx, cy, geom)

        nets = self._extract_def_nets(def_data)
        net_drivers, net_sinks = self._classify_net_endpoints(nets, comp_map, inst_map, cells_map)
        return self._find_cross_cell_tree_violations(
            nets, comp_map, inst_map, cells_map, comp_block, comp_pos, net_drivers, net_sinks
        )

    def _classify_net_endpoints(
        self,
        nets: Sequence[Mapping[str, object]],
        comp_map: Mapping[str, Mapping[str, object]],
        inst_map: Mapping[str, Mapping[str, object]],
        cells_map: Mapping[str, Mapping[str, object]],
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """Classify DEF net connections into driver and sink component lists."""
        net_drivers: dict[str, list[str]] = {}
        net_sinks: dict[str, list[str]] = {}
        for net in nets:
            nname = str(net.get("name", ""))
            drvs: list[str] = []
            snks: list[str] = []
            for iname, port in self._extract_net_connections(net):
                if not self._is_routable_comp(iname, comp_map, inst_map, cells_map):
                    continue
                target = (
                    drvs if self._is_drv_port(iname, port, comp_map, inst_map, cells_map) else snks
                )
                target.append(iname)
            net_drivers[nname] = drvs
            net_sinks[nname] = snks
        return net_drivers, net_sinks

    def _find_cross_cell_tree_violations(
        self,
        nets: Sequence[Mapping[str, object]],
        comp_map: Mapping[str, Mapping[str, object]],
        inst_map: Mapping[str, Mapping[str, object]],
        cells_map: Mapping[str, Mapping[str, object]],
        comp_block: Mapping[str, tuple[int, int] | None],
        comp_pos: Mapping[str, tuple[int, int]],
        net_drivers: Mapping[str, list[str]],
        net_sinks: Mapping[str, list[str]],
    ) -> list[DrcViolation]:
        """Return violations for splitters or mergers connected to components in another cell."""
        violations: list[DrcViolation] = []
        for net in nets:
            nname = str(net.get("name", ""))
            for iname, port in self._extract_net_connections(net):
                blk = comp_block.get(iname)
                if blk is None or not self._is_routable_comp(iname, comp_map, inst_map, cells_map):
                    continue
                violations.extend(
                    self._check_comp_tree_locality(
                        nname,
                        iname,
                        port,
                        blk,
                        comp_pos[iname],
                        comp_map,
                        inst_map,
                        cells_map,
                        comp_block,
                        net_drivers,
                        net_sinks,
                    )
                )
        return violations

    def _check_comp_tree_locality(
        self,
        nname: str,
        iname: str,
        port: str,
        blk: tuple[int, int],
        pos: tuple[int, int],
        comp_map: Mapping[str, Mapping[str, object]],
        inst_map: Mapping[str, Mapping[str, object]],
        cells_map: Mapping[str, Mapping[str, object]],
        comp_block: Mapping[str, tuple[int, int] | None],
        net_drivers: Mapping[str, list[str]],
        net_sinks: Mapping[str, list[str]],
    ) -> list[DrcViolation]:
        """Check whether a single splitter/merger port connection crosses cell boundaries."""
        macro = self._comp_macro(iname, comp_map, inst_map)
        is_drv = self._is_drv_port(iname, port, comp_map, inst_map, cells_map)
        cx, cy = pos
        if ("split" in macro or "balancer" in macro) and is_drv:
            return [
                DrcViolation(
                    code="CELL_SELF_CONTAINED_TREES",
                    message=(
                        f"Splitter {iname} inside cell {blk} drives "
                        f"{s_name} in {comp_block.get(s_name)} on net {nname}"
                    ),
                    x=float(cx),
                    y=float(cy),
                    net=nname,
                )
                for s_name in net_sinks.get(nname, ())
                if comp_block.get(s_name) != blk
            ]
        if "merge" in macro and not is_drv:
            return [
                DrcViolation(
                    code="CELL_SELF_CONTAINED_TREES",
                    message=(
                        f"Merger {iname} inside cell {blk} receives input "
                        f"from {d_name} in {comp_block.get(d_name)} on net {nname}"
                    ),
                    x=float(cx),
                    y=float(cy),
                    net=nname,
                )
                for d_name in net_drivers.get(nname, ())
                if comp_block.get(d_name) != blk
            ]
        return []

    @classmethod
    def _is_routable_comp(
        cls,
        iname: str,
        comp_map: Mapping[str, Mapping[str, object]],
        inst_map: Mapping[str, Mapping[str, object]],
        cells_map: Mapping[str, Mapping[str, object]],
    ) -> bool:
        """Return True if iname is a placed non-PIN, non-telemetry component."""
        if iname == "PIN" or iname not in comp_map:
            return False
        return not cls._is_telemetry_comp(iname, comp_map, inst_map, cells_map)

    @staticmethod
    def _comp_macro(
        iname: str,
        comp_map: Mapping[str, Mapping[str, object]],
        inst_map: Mapping[str, Mapping[str, object]],
    ) -> str:
        """Return resolved cell/macro name for component iname."""
        comp = comp_map.get(iname, {})
        inst = inst_map.get(iname, {})
        return str(inst.get("cell", comp.get("macro", "")))

    @classmethod
    def _is_telemetry_comp(
        cls,
        iname: str,
        comp_map: Mapping[str, Mapping[str, object]],
        inst_map: Mapping[str, Mapping[str, object]],
        cells_map: Mapping[str, Mapping[str, object]],
    ) -> bool:
        """Return True if component iname is a telemetry display or circuit combinator."""
        if iname.startswith(("u_acc_", "u_disp_")):
            return True
        macro = cls._comp_macro(iname, comp_map, inst_map)
        cell_info = cells_map.get(macro, {})
        return macro in ("display_panel", "arithmetic_combinator") or bool(
            isinstance(cell_info, Mapping) and cell_info.get("circuit_cell")
        )

    @classmethod
    def _is_drv_port(
        cls,
        iname: str,
        port: str,
        comp_map: Mapping[str, Mapping[str, object]],
        inst_map: Mapping[str, Mapping[str, object]],
        cells_map: Mapping[str, Mapping[str, object]],
    ) -> bool:
        """Return True if port on component iname is a driving output port."""
        macro = cls._comp_macro(iname, comp_map, inst_map)
        if any(k in macro for k in ("split", "merge", "balancer")):
            return port in ("y", "y1", "y2")
        cell_info = cells_map.get(macro, {})
        raw_outs = cell_info.get("outputs", ()) if isinstance(cell_info, Mapping) else ()
        outs = raw_outs if isinstance(raw_outs, Sequence) else ()
        clean_outs = {str(o).replace("-", "_") for o in outs} | {str(o) for o in outs}
        return port in clean_outs or port.replace("-", "_") in clean_outs or port in ("y", "out")

    def check_chassis_matrix_discipline(
        self,
        entities: Sequence[EntityRecord],
        macro_floorplan: object,
        router_routes: Mapping[str, object] | None = None,
        highway_specs: Sequence[object] | None = None,
        def_data: Mapping[str, object] | None = None,
        netlist_data: Mapping[str, object] | None = None,
        bay_blocks: set[tuple[int, int]] | None = None,
    ) -> list[DrcViolation]:
        """Verify chassis port geometry, switchbox discipline, and self-contained cell trees."""
        geom = self._build_chassis_geom(macro_floorplan, def_data, bay_blocks)
        tile_map: dict[tuple[int, int], list[EntityRecord]] = {}
        for ent in entities:
            tile_map.setdefault(_ent_floor_coords(ent), []).append(ent)

        block_ports = list(getattr(macro_floorplan, "block_ports", ()))
        port_channel_coords, violations = self._check_port_tap_geometry(
            block_ports, entities, tile_map, geom
        )

        checked_hwy_names: set[str] = set()
        fp_routes = getattr(macro_floorplan, "routes", {})
        route_objs = list(fp_routes.values()) if isinstance(fp_routes, Mapping) else list(fp_routes)
        violations.extend(
            self._check_floorplan_route_discipline(
                route_objs, router_routes, checked_hwy_names, geom
            )
        )
        if highway_specs:
            violations.extend(
                self._check_highway_spec_discipline(
                    highway_specs, port_channel_coords, router_routes, checked_hwy_names, geom
                )
            )
        if def_data:
            violations.extend(self._check_self_contained_cell_trees(def_data, netlist_data, geom))
        return violations
