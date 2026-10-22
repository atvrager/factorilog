"""Layout versus schematic (LVS) and physical design rule check (DRC) verifier."""

from __future__ import annotations

import collections
import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias

from foundry.belt_library import (
    DIR_STEPS,
    DIRECTION_NORTH,
    OPPOSITE_DIR,
)
from foundry.blueprint import decode
from foundry.entity_helpers import get_underground_span, register_prototype_boxes
from foundry.native_engine.engine import (
    FactorioNativeEngine,
    make_fluid_buffer,
    make_transport_line_with_items,
    read_fluid_buffer,
    read_transport_line,
    simulate_pipe_network,
)
from foundry.native_engine.symbol_finder import resolve_factorio_binary

EntityDict: TypeAlias = dict[str, Any]
ProtoData: TypeAlias = dict[str, Any]
BlueprintSource: TypeAlias = str | ProtoData | list[EntityDict]
DumpSource: TypeAlias = ProtoData | str | Path | None
GridPos: TypeAlias = tuple[int, int]
TileMap: TypeAlias = dict[GridPos, EntityDict]

FIXED_POINT_SCALE = 256
FALLBACK_BELT_TICKS = 8
RAY_SCAN_LIMIT = 32
SIMULATION_DEFAULT_TICKS = 120
PIPE_SIMULATION_CAP = 60
ITEM_OFFSET_UNITS = 64
DEFAULT_MAX_HOPS = 5000
EXPANSION_MODS = ("space-age", "elevated-rails", "quality")


@dataclass(frozen=True, slots=True)
class NetSpec:
    """Specification of a logical net connection between source and sink pins."""

    name: str
    commodity: str
    source_pos: tuple[float, float]
    sink_pos: tuple[float, float]
    is_fluid: bool = False
    required_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class DrcViolation:
    """Physical design rule violation reported on layout geometry."""

    code: str
    message: str
    position: tuple[float, float]
    severity: str = "ERROR"


@dataclass(frozen=True, slots=True)
class LvsError:
    """Logical netlist mismatch discovered during physical simulation."""

    net_name: str
    message: str
    position: tuple[float, float]
    severity: str = "ERROR"


@dataclass(slots=True)
class VerificationResult:
    """Combined report of DRC checks, LVS netlist matching, and simulation stats."""

    is_valid: bool
    drc_violations: list[DrcViolation] = field(default_factory=list)
    lvs_errors: list[LvsError] = field(default_factory=list)
    delivered_items: dict[str, int] = field(default_factory=dict)
    delivered_fluids: dict[str, float] = field(default_factory=dict)
    simulation_ticks: int = 0


def _round_pos(pos: tuple[float, float] | dict[str, float]) -> GridPos:
    """Round floating-point tile coordinates to grid integers using floor."""
    if isinstance(pos, dict):
        return math.floor(pos["x"]), math.floor(pos["y"])
    return math.floor(pos[0]), math.floor(pos[1])


def _is_core_prototype_entry(name: str, proto: object) -> bool:
    """Return True if a prototype entry belongs to the core base game."""
    if not isinstance(proto, dict):
        return True
    if "turbo" in name:
        return False
    return proto.get("mod") not in EXPANSION_MODS


def filter_core_prototypes(raw_data: ProtoData) -> ProtoData:
    """Filter prototype database to retain only core Factorio base game prototypes."""
    filtered: ProtoData = {}
    for category, items in raw_data.items():
        if isinstance(items, dict):
            filtered[category] = {
                name: proto
                for name, proto in items.items()
                if _is_core_prototype_entry(name, proto)
            }
        else:
            filtered[category] = items
    return filtered


def _try_load_json_dump(path: Path) -> ProtoData | None:
    """Attempt to read and filter a prototype JSON dump from path."""
    if not path.is_file():
        return None
    try:
        return filter_core_prototypes(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def load_game_prototypes(
    dump_source: DumpSource = None,
    game_bin: Path | None = None,
) -> ProtoData:
    """Load Factorio prototype database dynamically from the game environment."""
    if isinstance(dump_source, dict):
        return filter_core_prototypes(dump_source)

    if dump_source is not None:
        loaded = _try_load_json_dump(Path(dump_source))
        if loaded is not None:
            return loaded

    repo_fixture = (
        Path(__file__).resolve().parent.parent.parent / "test" / "data" / "dump-fixture.json"
    )
    loaded = _try_load_json_dump(repo_fixture)
    if loaded is not None:
        return loaded

    default_dump = Path("~/.factorio/script-output/data-raw-dump.json").expanduser()
    loaded = _try_load_json_dump(default_dump)
    if loaded is not None:
        return loaded

    bin_path = game_bin or resolve_factorio_binary()
    if bin_path and bin_path.is_file():
        subprocess.run([str(bin_path), "--dump-data"], check=False, capture_output=True)
        loaded = _try_load_json_dump(default_dump)
        if loaded is not None:
            return loaded

    return {}


def _pipe_mouth_pos(pos: GridPos, ent: EntityDict) -> GridPos:
    """Return the adjacent tile position that a pipe-to-ground mouth faces."""
    d = ent.get("direction", DIRECTION_NORTH)
    dx, dy = DIR_STEPS[d]
    return (pos[0] + dx, pos[1] + dy)


def _is_connected_surface_pipe(pos: GridPos, nbr_pos: GridPos, nbr_ent: EntityDict | None) -> bool:
    """Check whether nbr_ent at nbr_pos connects on the surface to pipe at pos."""
    if not nbr_ent:
        return False
    nbr_name = nbr_ent.get("name", "")
    if nbr_name == "pipe":
        return True
    if "pipe-to-ground" in nbr_name:
        return _pipe_mouth_pos(nbr_pos, nbr_ent) == pos
    return False


class NativeLayoutVerifier:
    """Validates physical layout connectivity, DRC compliance, and net arrival."""

    def __init__(
        self,
        engine: FactorioNativeEngine | None = None,
        prototypes: DumpSource = None,
    ) -> None:
        self.engine = engine
        game_bin = engine.binary_path if engine else None
        self.prototypes = load_game_prototypes(prototypes, game_bin=game_bin)
        if self.prototypes:
            register_prototype_boxes(self.prototypes)

    def _get_engine(self) -> FactorioNativeEngine:
        """Obtain or initialize the native Factorio execution engine."""
        if self.engine is None:
            self.engine = FactorioNativeEngine()
        return self.engine

    def _pipe_to_ground_reach(self, entity_name: str) -> int | None:
        """Query underground reach from a pipe-to-ground prototype definition."""
        pg_proto = self.prototypes.get("pipe-to-ground", {}).get(entity_name)
        if not pg_proto:
            return None
        fluid_box = pg_proto.get("fluid_box")
        if not isinstance(fluid_box, dict):
            return None
        for conn in fluid_box.get("pipe_connections", []):
            if conn.get("connection_type") == "underground" and "max_underground_distance" in conn:
                return int(conn["max_underground_distance"])
        return None

    def get_entity_span(self, entity_name: str) -> int:
        """Query maximum underground span for an entity from game prototype data."""
        ug_proto = self.prototypes.get("underground-belt", {}).get(entity_name)
        if ug_proto and "max_distance" in ug_proto:
            return int(ug_proto["max_distance"])

        pg_reach = self._pipe_to_ground_reach(entity_name)
        if pg_reach is not None:
            return pg_reach

        return get_underground_span(entity_name)

    def get_belt_speed(self, entity_name: str) -> int:
        """Query belt speed in fixed-point units per tick from game prototypes."""
        belt_proto = self.prototypes.get("transport-belt", {}).get(entity_name)
        if belt_proto and "speed" in belt_proto:
            return int(float(belt_proto["speed"]) * FIXED_POINT_SCALE)
        return FALLBACK_BELT_TICKS

    def parse_entities(
        self,
        blueprint_input: BlueprintSource,
    ) -> list[EntityDict]:
        """Extract entity list from exchange string, dict document, or entity array."""
        if isinstance(blueprint_input, str):
            data = decode(blueprint_input)
            return data.get("blueprint", {}).get("entities", [])
        if isinstance(blueprint_input, dict):
            if "blueprint" in blueprint_input:
                return blueprint_input["blueprint"].get("entities", [])
            return blueprint_input.get("entities", [])
        return blueprint_input

    def _validate_same_tier_ug_output(
        self,
        target_pos: GridPos,
        in_dir: int,
        out_dir: int,
        step: int,
        limit_val: int,
        violations: list[DrcViolation],
    ) -> None:
        """Record DRC violation if same-tier output has bad direction or span."""
        if out_dir != in_dir:
            violations.append(
                DrcViolation(
                    code="DRC_UG_DIRECTION_MISMATCH",
                    message=f"Output underground facing wrong direction at {target_pos}.",
                    position=target_pos,
                )
            )
        elif step > limit_val:
            violations.append(
                DrcViolation(
                    code="DRC_UG_SPAN_EXCEEDED",
                    message=f"Underground span {step} exceeds prototype limit {limit_val}.",
                    position=target_pos,
                )
            )

    def _match_ug_output_at_pos(
        self,
        target_pos: GridPos,
        name: str,
        in_dir: int,
        step: int,
        limit_val: int,
        outputs_by_pos: dict[GridPos, list[tuple[int, EntityDict]]],
        paired_outputs: set[int],
        violations: list[DrcViolation],
    ) -> tuple[bool, GridPos | None]:
        """Inspect outputs at target_pos; return (found_same_tier, mismatched_pos)."""
        mismatched: GridPos | None = None
        for idx, out_ent in outputs_by_pos.get(target_pos, ()):
            if idx in paired_outputs:
                continue
            out_dir = out_ent.get("direction", DIRECTION_NORTH)
            if out_ent.get("name", "") == name:
                self._validate_same_tier_ug_output(
                    target_pos, in_dir, out_dir, step, limit_val, violations
                )
                paired_outputs.add(idx)
                return True, None
            if out_dir == in_dir and mismatched is None:
                mismatched = target_pos
        return False, mismatched

    def _trace_single_ug_input(
        self,
        in_ent: EntityDict,
        ug_input_positions: set[tuple[str, GridPos]],
        outputs_by_pos: dict[GridPos, list[tuple[int, EntityDict]]],
        paired_outputs: set[int],
        violations: list[DrcViolation],
    ) -> None:
        """Verify pairing and span for a single underground-belt input entity."""
        in_pos = _round_pos(in_ent["position"])
        in_dir = in_ent.get("direction", DIRECTION_NORTH)
        dx, dy = DIR_STEPS.get(in_dir, (0, -1))
        name = in_ent.get("name", "underground-belt")
        limit_val = self.get_entity_span(name)

        found_output = False
        found_mismatched_output: GridPos | None = None
        for step in range(1, RAY_SCAN_LIMIT):
            target_pos = (in_pos[0] + dx * step, in_pos[1] + dy * step)
            if (name, target_pos) in ug_input_positions:
                break
            matched, mismatch = self._match_ug_output_at_pos(
                target_pos,
                name,
                in_dir,
                step,
                limit_val,
                outputs_by_pos,
                paired_outputs,
                violations,
            )
            if mismatch is not None and found_mismatched_output is None:
                found_mismatched_output = mismatch
            if matched:
                found_output = True
                break

        if found_output:
            return
        if found_mismatched_output is not None:
            violations.append(
                DrcViolation(
                    code="DRC_UG_TIER_MISMATCH",
                    message=f"Mismatched tier between underground pair at {found_mismatched_output}.",
                    position=found_mismatched_output,
                )
            )
        else:
            violations.append(
                DrcViolation(
                    code="DRC_UG_UNPAIRED",
                    message=f"Input underground at {in_pos} has no matching downstream output.",
                    position=in_pos,
                )
            )

    def _check_underground_belts_drc(
        self,
        entities: list[EntityDict],
        violations: list[DrcViolation],
    ) -> None:
        """Check underground belt pairing, direction, tier, and span rules."""
        ug_inputs = [
            e
            for e in entities
            if "underground-belt" in e.get("name", "") and e.get("type", "input") == "input"
        ]
        ug_outputs = [
            e
            for e in entities
            if "underground-belt" in e.get("name", "") and e.get("type", "input") != "input"
        ]
        outputs_by_pos: dict[GridPos, list[tuple[int, EntityDict]]] = collections.defaultdict(list)
        for idx, out_ent in enumerate(ug_outputs):
            outputs_by_pos[_round_pos(out_ent["position"])].append((idx, out_ent))
        ug_input_positions = {
            (e.get("name", "underground-belt"), _round_pos(e["position"])) for e in ug_inputs
        }
        paired_outputs: set[int] = set()

        for in_ent in ug_inputs:
            self._trace_single_ug_input(
                in_ent, ug_input_positions, outputs_by_pos, paired_outputs, violations
            )

        for idx, out_ent in enumerate(ug_outputs):
            if idx not in paired_outputs:
                out_pos = _round_pos(out_ent["position"])
                violations.append(
                    DrcViolation(
                        code="DRC_UG_UNPAIRED",
                        message=f"Output underground at {out_pos} has no matching upstream input.",
                        position=out_pos,
                    )
                )

    def _try_pair_pipe_ground_at_step(
        self,
        i: int,
        pg_name: str,
        ug_dir: int,
        step: int,
        limit_val: int,
        target_pos: GridPos,
        pg_by_pos: dict[GridPos, list[tuple[int, EntityDict]]],
        paired_pipe_grounds: set[int],
        violations: list[DrcViolation],
    ) -> bool:
        """Check if a matching pipe-to-ground exists at target_pos."""
        for j, pg_b in pg_by_pos.get(target_pos, ()):
            if i == j or j in paired_pipe_grounds or pg_b.get("name") != pg_name:
                continue
            if pg_b.get("direction", DIRECTION_NORTH) == ug_dir:
                if step > limit_val:
                    violations.append(
                        DrcViolation(
                            code="DRC_PIPE_SPAN_EXCEEDED",
                            message=f"Pipe span {step} exceeds prototype limit {limit_val}.",
                            position=target_pos,
                        )
                    )
                paired_pipe_grounds.add(i)
                paired_pipe_grounds.add(j)
                return True
            return False
        return False

    def _check_pipe_grounds_drc(
        self,
        entities: list[EntityDict],
        violations: list[DrcViolation],
    ) -> None:
        """Check pipe-to-ground underground span limits."""
        pipe_grounds = [e for e in entities if "pipe-to-ground" in e.get("name", "")]
        pg_by_pos: dict[GridPos, list[tuple[int, EntityDict]]] = collections.defaultdict(list)
        for idx, pg in enumerate(pipe_grounds):
            pg_by_pos[_round_pos(pg["position"])].append((idx, pg))
        paired_pipe_grounds: set[int] = set()

        for i, pg_a in enumerate(pipe_grounds):
            if i in paired_pipe_grounds:
                continue
            pos_a = _round_pos(pg_a["position"])
            dir_a = pg_a.get("direction", DIRECTION_NORTH)
            ug_dir = OPPOSITE_DIR.get(dir_a, dir_a)
            dx, dy = DIR_STEPS.get(ug_dir, (0, -1))
            pg_name = pg_a.get("name", "pipe-to-ground")
            limit_val = self.get_entity_span(pg_name)

            for step in range(1, RAY_SCAN_LIMIT):
                target_pos = (pos_a[0] + dx * step, pos_a[1] + dy * step)
                if self._try_pair_pipe_ground_at_step(
                    i,
                    pg_name,
                    ug_dir,
                    step,
                    limit_val,
                    target_pos,
                    pg_by_pos,
                    paired_pipe_grounds,
                    violations,
                ):
                    break

    def _build_pipe_components(self, entities: list[EntityDict]) -> list[set[GridPos]]:
        """Group connected pipe entities into disjoint connected components."""
        pipe_positions = {
            _round_pos(e["position"]) for e in entities if "pipe" in e.get("name", "")
        }
        tile_map: TileMap = {_round_pos(e["position"]): e for e in entities}
        visited: set[GridPos] = set()
        components: list[set[GridPos]] = []

        for p_pos in pipe_positions:
            if p_pos in visited:
                continue
            comp: set[GridPos] = set()
            queue = collections.deque([p_pos])
            visited.add(p_pos)
            while queue:
                curr = queue.popleft()
                comp.add(curr)
                for neighbor in self._get_pipe_neighbors(curr, tile_map):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
            components.append(comp)
        return components

    def _check_fluid_isolation_drc(
        self,
        entities: list[EntityDict],
        nets: list[NetSpec] | None,
        violations: list[DrcViolation],
    ) -> None:
        """Check whether multiple distinct fluids connect to the same pipe component."""
        if not nets:
            return
        fluid_nets = [n for n in nets if n.is_fluid]
        if len(fluid_nets) <= 1:
            return
        components = self._build_pipe_components(entities)
        for comp in components:
            commodities_in_comp = {
                f_net.commodity
                for f_net in fluid_nets
                if _round_pos(f_net.source_pos) in comp or _round_pos(f_net.sink_pos) in comp
            }
            if len(commodities_in_comp) > 1:
                conflict_list = list(commodities_in_comp)
                conflict_list.sort()
                violations.append(
                    DrcViolation(
                        code="DRC_FLUID_CONTAMINATION",
                        message=f"Conflicting fluids {conflict_list} connect to same pipe network.",
                        position=next(iter(comp)),
                    )
                )

    def check_drc(
        self,
        entities: list[EntityDict],
        nets: list[NetSpec] | None = None,
    ) -> list[DrcViolation]:
        """Perform physical design rule checks across all layout entities."""
        violations: list[DrcViolation] = []
        self._check_underground_belts_drc(entities, violations)
        self._check_pipe_grounds_drc(entities, violations)
        self._check_fluid_isolation_drc(entities, nets, violations)
        return violations

    def _find_subterranean_pipe_neighbor(
        self,
        pos: GridPos,
        ent: EntityDict,
        tile_map: TileMap,
    ) -> GridPos | None:
        """Find underground matching pipe-to-ground partner if within reach."""
        name = ent.get("name", "")
        dir_a = ent.get("direction", DIRECTION_NORTH)
        ug_dir = OPPOSITE_DIR.get(dir_a, dir_a)
        dx, dy = DIR_STEPS.get(ug_dir, (0, -1))
        limit_val = self.get_entity_span(name)

        for step in range(1, limit_val + 1):
            target_pos = (pos[0] + dx * step, pos[1] + dy * step)
            cand = tile_map.get(target_pos)
            if cand and cand.get("name", "") == name:
                if cand.get("direction", DIRECTION_NORTH) == ug_dir:
                    return target_pos
                return None
        return None

    def _get_pipe_neighbors(
        self,
        pos: GridPos,
        tile_map: TileMap,
    ) -> list[GridPos]:
        """Return connected adjacent and subterranean fluid neighbors for entity at pos."""
        ent = tile_map.get(pos)
        if not ent or "pipe" not in ent.get("name", ""):
            return []

        name = ent.get("name", "")
        if name == "pipe":
            return sorted(
                (pos[0] + dx, pos[1] + dy)
                for dx, dy in DIR_STEPS.values()
                if _is_connected_surface_pipe(
                    pos, (pos[0] + dx, pos[1] + dy), tile_map.get((pos[0] + dx, pos[1] + dy))
                )
            )
        if "pipe-to-ground" not in name:
            return []

        neighbors: set[GridPos] = set()
        mouth_pos = _pipe_mouth_pos(pos, ent)
        if _is_connected_surface_pipe(pos, mouth_pos, tile_map.get(mouth_pos)):
            neighbors.add(mouth_pos)
        ug_nbr = self._find_subterranean_pipe_neighbor(pos, ent, tile_map)
        if ug_nbr is not None:
            neighbors.add(ug_nbr)
        return sorted(neighbors)

    def _verify_fluid_net(
        self,
        net: NetSpec,
        src: GridPos,
        snk: GridPos,
        tile_map: TileMap,
        engine: FactorioNativeEngine,
        ticks: int,
        lvs_errors: list[LvsError],
        delivered_fluids: dict[str, float],
    ) -> None:
        """Simulate fluid propagation for a single fluid net."""
        pipe_path = self._trace_fluid_path(src, snk, tile_map)
        if not pipe_path:
            lvs_errors.append(
                LvsError(
                    net_name=net.name,
                    message=f"No connected pipe path found from {src} to {snk}.",
                    position=src,
                )
            )
            return

        path_len = len(pipe_path)
        buffers = [
            make_fluid_buffer(fluid_id=1, amount=0.0, capacity=100.0) for _ in range(path_len)
        ]
        buffers[0] = make_fluid_buffer(fluid_id=1, amount=100.0, capacity=100.0)

        sim_ticks = min(ticks, PIPE_SIMULATION_CAP)
        simulate_pipe_network(engine, buffers, ticks=sim_ticks)

        final_sink_amount = read_fluid_buffer(buffers[-1])["amount"]
        delivered_fluids[net.name] = final_sink_amount
        if final_sink_amount <= 0.0:
            lvs_errors.append(
                LvsError(
                    net_name=net.name,
                    message=f"Fluid failed to reach sink {snk} after {sim_ticks} ticks.",
                    position=snk,
                )
            )

    def _verify_belt_net(
        self,
        net: NetSpec,
        src: GridPos,
        snk: GridPos,
        tile_map: TileMap,
        engine: FactorioNativeEngine,
        ticks: int,
        lvs_errors: list[LvsError],
        delivered_items: dict[str, int],
    ) -> None:
        """Simulate discrete item transport along a belt net."""
        belt_path = self._trace_belt_path(src, snk, tile_map)
        if not belt_path:
            lvs_errors.append(
                LvsError(
                    net_name=net.name,
                    message=f"No continuous belt path found from {src} to {snk}.",
                    position=src,
                )
            )
            return

        path_len = len(belt_path)
        first_name = tile_map.get(belt_path[0], {}).get("name", "")
        speed = self.get_belt_speed(first_name)

        total_units = max(FIXED_POINT_SCALE, (path_len - 1) * FIXED_POINT_SCALE)
        ticks_needed = math.ceil(total_units / speed)

        line, _ = make_transport_line_with_items(
            [ITEM_OFFSET_UNITS],
            front_distance=total_units,
            line_length=total_units,
        )
        steps = max(ticks, ticks_needed + 10)
        for t in range(steps):
            engine.move_transport_line(line, speed, tick=t + 1)

        state = read_transport_line(line)
        if state["front_distance"] > 0:
            lvs_errors.append(
                LvsError(
                    net_name=net.name,
                    message=f"Item did not reach sink {snk} within {steps} ticks.",
                    position=snk,
                )
            )
        else:
            delivered_items[net.name] = 1

    def simulate_and_verify_lvs(
        self,
        entities: list[EntityDict],
        nets: list[NetSpec],
        ticks: int = SIMULATION_DEFAULT_TICKS,
    ) -> tuple[list[LvsError], dict[str, int], dict[str, float]]:
        """Simulate item and fluid propagation to verify net arrival and isolation."""
        lvs_errors: list[LvsError] = []
        delivered_items: dict[str, int] = {}
        delivered_fluids: dict[str, float] = {}

        tile_map: TileMap = {_round_pos(ent["position"]): ent for ent in entities}
        engine = self._get_engine()

        for net in nets:
            src = _round_pos(net.source_pos)
            snk = _round_pos(net.sink_pos)
            if net.is_fluid:
                self._verify_fluid_net(
                    net, src, snk, tile_map, engine, ticks, lvs_errors, delivered_fluids
                )
            else:
                self._verify_belt_net(
                    net, src, snk, tile_map, engine, ticks, lvs_errors, delivered_items
                )

        return lvs_errors, delivered_items, delivered_fluids

    def _trace_fluid_path(
        self,
        src: GridPos,
        snk: GridPos,
        tile_map: TileMap,
    ) -> list[GridPos]:
        """Find a path connecting fluid source to sink across pipes."""
        queue = collections.deque([[src]])
        visited = {src}

        while queue:
            path = queue.popleft()
            curr = path[-1]
            if curr == snk:
                return path

            for nxt in self._get_pipe_neighbors(curr, tile_map):
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append([*path, nxt])

        return []

    def _find_ug_belt_exit(
        self,
        curr: GridPos,
        name: str,
        d: int,
        dx: int,
        dy: int,
        tile_map: TileMap,
    ) -> GridPos | None:
        """Locate the downstream output tile for an underground-belt input at curr."""
        limit_val = self.get_entity_span(name)
        for step in range(1, RAY_SCAN_LIMIT):
            target = (curr[0] + dx * step, curr[1] + dy * step)
            cand = tile_map.get(target)
            if cand and cand.get("name") == name:
                if (
                    cand.get("type") == "output"
                    and cand.get("direction", DIRECTION_NORTH) == d
                    and step <= limit_val
                ):
                    return target
                return None
        return None

    def _next_belt_hop(
        self,
        curr: GridPos,
        tile_map: TileMap,
        visited: set[GridPos],
    ) -> GridPos | None:
        """Compute the next downstream coordinate along a belt path."""
        ent = tile_map.get(curr)
        if not ent:
            return None
        name = ent.get("name", "")
        d = ent.get("direction", DIRECTION_NORTH)
        dx, dy = DIR_STEPS.get(d, (0, -1))

        if "underground-belt" in name and ent.get("type") == "input":
            return self._find_ug_belt_exit(curr, name, d, dx, dy, tile_map)

        nxt = (curr[0] + dx, curr[1] + dy)
        return None if nxt in visited else nxt

    def _trace_belt_path(
        self,
        src: GridPos,
        snk: GridPos,
        tile_map: TileMap,
        max_hops: int = DEFAULT_MAX_HOPS,
    ) -> list[GridPos]:
        """Trace downstream belt route from source to sink."""
        path: list[GridPos] = [src]
        curr = src
        visited: set[GridPos] = {src}

        for _ in range(max_hops):
            if curr == snk:
                return path
            nxt = self._next_belt_hop(curr, tile_map, visited)
            if nxt is None:
                break
            curr = nxt
            path.append(curr)
            visited.add(curr)

        return path if path[-1] == snk else []

    def verify_blueprint(
        self,
        blueprint_input: BlueprintSource,
        nets: list[NetSpec] | None = None,
        ticks: int = SIMULATION_DEFAULT_TICKS,
    ) -> VerificationResult:
        """Run complete DRC and LVS physical verification on layout."""
        entities = self.parse_entities(blueprint_input)
        drc_violations = self.check_drc(entities, nets=nets)

        lvs_errors: list[LvsError] = []
        delivered_items: dict[str, int] = {}
        delivered_fluids: dict[str, float] = {}

        if nets:
            lvs_errors, delivered_items, delivered_fluids = self.simulate_and_verify_lvs(
                entities, nets, ticks=ticks
            )

        is_valid = len(drc_violations) == 0 and len(lvs_errors) == 0
        return VerificationResult(
            is_valid=is_valid,
            drc_violations=drc_violations,
            lvs_errors=lvs_errors,
            delivered_items=delivered_items,
            delivered_fluids=delivered_fluids,
            simulation_ticks=ticks,
        )
