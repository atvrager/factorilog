"""Layout versus schematic (LVS) and physical design rule check (DRC) verifier."""

from __future__ import annotations

import collections
import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from foundry.belt_library import (
    DIR_STEPS,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
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


@dataclass(frozen=True)
class NetSpec:
    """Specification of a logical net connection between source and sink pins."""

    name: str
    commodity: str
    source_pos: tuple[float, float]
    sink_pos: tuple[float, float]
    is_fluid: bool = False
    required_rate: float = 1.0


@dataclass(frozen=True)
class DrcViolation:
    """Physical design rule violation reported on layout geometry."""

    code: str
    message: str
    position: tuple[float, float]
    severity: str = "ERROR"


@dataclass(frozen=True)
class LvsError:
    """Logical netlist mismatch discovered during physical simulation."""

    net_name: str
    message: str
    position: tuple[float, float]
    severity: str = "ERROR"


@dataclass
class VerificationResult:
    """Combined report of DRC checks, LVS netlist matching, and simulation stats."""

    is_valid: bool
    drc_violations: list[DrcViolation] = field(default_factory=list)
    lvs_errors: list[LvsError] = field(default_factory=list)
    delivered_items: dict[str, int] = field(default_factory=dict)
    delivered_fluids: dict[str, float] = field(default_factory=dict)
    simulation_ticks: int = 0


def _round_pos(pos: tuple[float, float] | dict[str, float]) -> tuple[int, int]:
    """Round floating-point tile coordinates to grid integers using floor."""
    if isinstance(pos, dict):
        return math.floor(pos["x"]), math.floor(pos["y"])
    return math.floor(pos[0]), math.floor(pos[1])


def load_game_prototypes(
    dump_source: dict[str, Any] | str | Path | None = None,
    game_bin: Path | None = None,
) -> dict[str, Any]:
    """Load Factorio prototype database dynamically from the game environment."""
    if isinstance(dump_source, dict):
        return dump_source

    if dump_source is not None:
        path = Path(dump_source)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))

    # Check live game dump from script-output
    default_dump = Path("~/.factorio/script-output/data-raw-dump.json").expanduser()
    if default_dump.is_file():
        try:
            return json.loads(default_dump.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Check hermetic repository fixture
    repo_fixture = (
        Path(__file__).resolve().parent.parent.parent / "test" / "data" / "dump-fixture.json"
    )
    if repo_fixture.is_file():
        try:
            return json.loads(repo_fixture.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Generate fresh dump using host game binary
    bin_path = game_bin or resolve_factorio_binary()
    if bin_path and bin_path.is_file():
        subprocess.run([str(bin_path), "--dump-data"], check=False, capture_output=True)
        if default_dump.is_file():
            return json.loads(default_dump.read_text(encoding="utf-8"))

    return {}


class NativeLayoutVerifier:
    """Validates physical layout connectivity, DRC compliance, and net arrival."""

    def __init__(
        self,
        engine: FactorioNativeEngine | None = None,
        prototypes: dict[str, Any] | str | Path | None = None,
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

    def get_entity_span(self, entity_name: str) -> int:
        """Query maximum underground span for an entity from game prototype data."""
        # 1. Search underground-belt prototypes
        ug_proto = self.prototypes.get("underground-belt", {}).get(entity_name)
        if ug_proto and "max_distance" in ug_proto:
            return int(ug_proto["max_distance"])

        # 2. Search pipe-to-ground prototypes
        pg_proto = self.prototypes.get("pipe-to-ground", {}).get(entity_name)
        if pg_proto:
            fluid_box = pg_proto.get("fluid_box")
            if isinstance(fluid_box, dict):
                for conn in fluid_box.get("pipe_connections", []):
                    if (
                        conn.get("connection_type") == "underground"
                        and "max_underground_distance" in conn
                    ):
                        return int(conn["max_underground_distance"])

        # 3. Query entity_helpers registered prototype spans
        return get_underground_span(entity_name)

    def get_belt_speed(self, entity_name: str) -> int:
        """Query belt speed in fixed-point units per tick from game prototypes."""
        belt_proto = self.prototypes.get("transport-belt", {}).get(entity_name)
        if belt_proto and "speed" in belt_proto:
            return int(float(belt_proto["speed"]) * 256)
        return 8

    def parse_entities(
        self,
        blueprint_input: str | dict[str, Any] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Extract entity list from exchange string, dict document, or entity array."""
        if isinstance(blueprint_input, str):
            data = decode(blueprint_input)
            return data.get("blueprint", {}).get("entities", [])
        if isinstance(blueprint_input, dict):
            if "blueprint" in blueprint_input:
                return blueprint_input["blueprint"].get("entities", [])
            return blueprint_input.get("entities", [])
        return blueprint_input

    def check_drc(
        self,
        entities: list[dict[str, Any]],
        nets: list[NetSpec] | None = None,
    ) -> list[DrcViolation]:
        """Perform physical design rule checks across all layout entities."""
        violations: list[DrcViolation] = []

        # 1. Inspect underground belts
        ug_inputs: list[dict[str, Any]] = []
        ug_outputs: list[dict[str, Any]] = []
        for ent in entities:
            name = ent.get("name", "")
            if "underground-belt" in name:
                ug_type = ent.get("type", "input")
                if ug_type == "input":
                    ug_inputs.append(ent)
                else:
                    ug_outputs.append(ent)

        paired_outputs: set[int] = set()

        for in_ent in ug_inputs:
            in_pos = _round_pos(in_ent["position"])
            in_dir = in_ent.get("direction", DIRECTION_NORTH)
            dx, dy = DIR_STEPS.get(in_dir, (0, -1))
            name = in_ent.get("name", "underground-belt")
            max_span = self.get_entity_span(name)

            found_output = False
            found_mismatched_output = None
            for step in range(1, 32):
                target_pos = (in_pos[0] + dx * step, in_pos[1] + dy * step)

                # Same-tier entrance along the ray blocks downstream pairing
                same_tier_block = any(
                    other is not in_ent
                    and other.get("name") == name
                    and _round_pos(other["position"]) == target_pos
                    for other in ug_inputs
                )
                if same_tier_block:
                    break

                for idx, out_ent in enumerate(ug_outputs):
                    if idx in paired_outputs:
                        continue
                    out_name = out_ent.get("name", "")
                    out_pos = _round_pos(out_ent["position"])
                    if out_pos == target_pos:
                        out_dir = out_ent.get("direction", DIRECTION_NORTH)
                        if out_name == name:
                            if out_dir != in_dir:
                                violations.append(
                                    DrcViolation(
                                        code="DRC_UG_DIRECTION_MISMATCH",
                                        message=f"Output underground facing wrong direction at {target_pos}.",
                                        position=target_pos,
                                    )
                                )
                            elif step > max_span:
                                violations.append(
                                    DrcViolation(
                                        code="DRC_UG_SPAN_EXCEEDED",
                                        message=f"Underground span {step} exceeds prototype limit {max_span}.",
                                        position=target_pos,
                                    )
                                )
                            paired_outputs.add(idx)
                            found_output = True
                            break
                        if out_dir == in_dir and found_mismatched_output is None:
                            found_mismatched_output = target_pos
                if found_output:
                    break

            if not found_output:
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

        # 2. Inspect pipe-to-ground spans
        pipe_grounds = [e for e in entities if "pipe-to-ground" in e.get("name", "")]
        paired_pipe_grounds: set[int] = set()

        for i, pg_a in enumerate(pipe_grounds):
            if i in paired_pipe_grounds:
                continue
            pos_a = _round_pos(pg_a["position"])
            dir_a = pg_a.get("direction", DIRECTION_NORTH)
            dx, dy = DIR_STEPS.get(dir_a, (0, -1))
            opp_dir = OPPOSITE_DIR.get(dir_a, DIRECTION_SOUTH)
            pg_name = pg_a.get("name", "pipe-to-ground")
            max_pipe_span = self.get_entity_span(pg_name)

            matched = False
            for step in range(1, 32):
                target_pos = (pos_a[0] + dx * step, pos_a[1] + dy * step)
                for j, pg_b in enumerate(pipe_grounds):
                    if i == j or j in paired_pipe_grounds:
                        continue
                    if pg_b.get("name") != pg_name:
                        # Different pipe tier: weaves on independent subterranean layer
                        continue
                    pos_b = _round_pos(pg_b["position"])
                    if pos_b == target_pos:
                        dir_b = pg_b.get("direction", DIRECTION_NORTH)
                        if dir_b == opp_dir:
                            if step > max_pipe_span:
                                violations.append(
                                    DrcViolation(
                                        code="DRC_PIPE_SPAN_EXCEEDED",
                                        message=f"Pipe span {step} exceeds prototype limit {max_pipe_span}.",
                                        position=target_pos,
                                    )
                                )
                            paired_pipe_grounds.add(i)
                            paired_pipe_grounds.add(j)
                            matched = True
                        break
                if matched:
                    break

        # 3. Check fluid isolation across nets
        if nets:
            fluid_nets = [n for n in nets if n.is_fluid]
            if len(fluid_nets) > 1:
                # Build adjacency graph among pipe entities
                pipe_positions = {
                    _round_pos(e["position"]) for e in entities if "pipe" in e.get("name", "")
                }
                visited: set[tuple[int, int]] = set()
                components: list[set[tuple[int, int]]] = []

                tile_map = {_round_pos(e["position"]): e for e in entities}
                for p_pos in pipe_positions:
                    if p_pos in visited:
                        continue
                    comp: set[tuple[int, int]] = set()
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

                # Check if multiple distinct fluid commodities touch the same component
                for comp in components:
                    commodities_in_comp: set[str] = set()
                    for f_net in fluid_nets:
                        src_r = _round_pos(f_net.source_pos)
                        snk_r = _round_pos(f_net.sink_pos)
                        if src_r in comp or snk_r in comp:
                            commodities_in_comp.add(f_net.commodity)
                    if len(commodities_in_comp) > 1:
                        violations.append(
                            DrcViolation(
                                code="DRC_FLUID_CONTAMINATION",
                                message=f"Conflicting fluids {sorted(commodities_in_comp)} connect to same pipe network.",
                                position=next(iter(comp)),
                            )
                        )

        return violations

    def _get_pipe_neighbors(
        self,
        pos: tuple[int, int],
        tile_map: dict[tuple[int, int], dict[str, Any]],
    ) -> list[tuple[int, int]]:
        """Return connected adjacent and subterranean fluid neighbors for entity at pos."""
        ent = tile_map.get(pos)
        if not ent or "pipe" not in ent.get("name", ""):
            return []

        neighbors: set[tuple[int, int]] = set()
        name = ent.get("name", "")

        # 1. Surface orthogonal neighbors
        for dx, dy in DIR_STEPS.values():
            nbr_pos = (pos[0] + dx, pos[1] + dy)
            nbr_ent = tile_map.get(nbr_pos)
            if nbr_ent and "pipe" in nbr_ent.get("name", ""):
                neighbors.add(nbr_pos)

        # 2. Subterranean underground leap
        if "pipe-to-ground" in name:
            dir_a = ent.get("direction", DIRECTION_NORTH)
            dx, dy = DIR_STEPS.get(dir_a, (0, -1))
            opp_dir = (dir_a + 8) % 16
            max_span = self.get_entity_span(name)

            for step in range(1, max_span + 1):
                target_pos = (pos[0] + dx * step, pos[1] + dy * step)
                cand = tile_map.get(target_pos)
                if cand:
                    cand_name = cand.get("name", "")
                    if cand_name == name:
                        cand_dir = cand.get("direction", DIRECTION_NORTH)
                        if cand_dir == opp_dir:
                            neighbors.add(target_pos)
                        # Intervening same-tier pipe ends underground span
                        break
                    # Different tier pipe-to-ground or belt weaves through without blocking

        return sorted(neighbors)

    def simulate_and_verify_lvs(
        self,
        entities: list[dict[str, Any]],
        nets: list[NetSpec],
        ticks: int = 120,
    ) -> tuple[list[LvsError], dict[str, int], dict[str, float]]:
        """Simulate item and fluid propagation to verify net arrival and isolation."""
        lvs_errors: list[LvsError] = []
        delivered_items: dict[str, int] = {}
        delivered_fluids: dict[str, float] = {}

        # Map entities by rounded coordinate
        tile_map: dict[tuple[int, int], dict[str, Any]] = {}
        for ent in entities:
            tile_map[_round_pos(ent["position"])] = ent

        engine = self._get_engine()

        for net in nets:
            src = _round_pos(net.source_pos)
            snk = _round_pos(net.sink_pos)

            if net.is_fluid:
                # Trace connected pipe path
                pipe_path = self._trace_fluid_path(src, snk, tile_map)
                if not pipe_path:
                    lvs_errors.append(
                        LvsError(
                            net_name=net.name,
                            message=f"No connected pipe path found from {src} to {snk}.",
                            position=src,
                        )
                    )
                    continue

                # Simulate pipe flow with native engine
                path_len = len(pipe_path)
                buffers = [
                    make_fluid_buffer(fluid_id=1, amount=0.0, capacity=100.0)
                    for _ in range(path_len)
                ]
                # Seed source with 100 units of fluid
                buffers[0] = make_fluid_buffer(fluid_id=1, amount=100.0, capacity=100.0)

                sim_ticks = min(ticks, 60)
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
            else:
                # Trace belt path
                belt_path = self._trace_belt_path(src, snk, tile_map)
                if not belt_path:
                    lvs_errors.append(
                        LvsError(
                            net_name=net.name,
                            message=f"No continuous belt path found from {src} to {snk}.",
                            position=src,
                        )
                    )
                    continue

                path_len = len(belt_path)
                first_name = tile_map.get(belt_path[0], {}).get("name", "")
                speed = self.get_belt_speed(first_name)

                # Total units to traverse path from source to sink
                total_distance_units = max(256, (path_len - 1) * 256)
                ticks_needed = math.ceil(total_distance_units / speed)

                # Simulate discrete movement along line
                line, _ = make_transport_line_with_items(
                    [64],
                    front_distance=total_distance_units,
                    line_length=total_distance_units,
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

        return lvs_errors, delivered_items, delivered_fluids

    def _trace_fluid_path(
        self,
        src: tuple[int, int],
        snk: tuple[int, int],
        tile_map: dict[tuple[int, int], dict[str, Any]],
    ) -> list[tuple[int, int]]:
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

    def _trace_belt_path(
        self,
        src: tuple[int, int],
        snk: tuple[int, int],
        tile_map: dict[tuple[int, int], dict[str, Any]],
        max_hops: int = 500,
    ) -> list[tuple[int, int]]:
        """Trace downstream belt route from source to sink."""
        path: list[tuple[int, int]] = [src]
        curr = src
        visited: set[tuple[int, int]] = {src}

        for _ in range(max_hops):
            if curr == snk:
                return path

            ent = tile_map.get(curr)
            if not ent:
                break

            name = ent.get("name", "")
            d = ent.get("direction", DIRECTION_NORTH)
            dx, dy = DIR_STEPS.get(d, (0, -1))

            if "underground-belt" in name and ent.get("type") == "input":
                # Advance across underground span to matching output
                max_span = self.get_entity_span(name)
                found_out = None
                for step in range(1, 32):
                    target = (curr[0] + dx * step, curr[1] + dy * step)
                    cand = tile_map.get(target)
                    if cand and cand.get("name") == name:
                        if (
                            cand.get("type") == "output"
                            and cand.get("direction", DIRECTION_NORTH) == d
                            and step <= max_span
                        ):
                            found_out = target
                        # Intervening same-tier underground entity blocks/ends connection
                        break
                if not found_out:
                    break
                curr = found_out
                path.append(curr)
                visited.add(curr)
            else:
                nxt = (curr[0] + dx, curr[1] + dy)
                if nxt in visited:
                    break  # Cycle detected
                curr = nxt
                path.append(curr)
                visited.add(curr)

        return path if path[-1] == snk else []

    def verify_blueprint(
        self,
        blueprint_input: str | dict[str, Any] | list[dict[str, Any]],
        nets: list[NetSpec] | None = None,
        ticks: int = 120,
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
