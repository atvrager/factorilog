"""Physical Design Rule Check (DRC) engine for Factorio layouts."""

from __future__ import annotations

import collections
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    from foundry.entity_helpers import entity_size
except ImportError:
    from entity_helpers import entity_size


@dataclass(frozen=True)
class DrcViolation:
    """A single physical design rule violation."""

    code: str
    message: str
    x: float
    y: float
    net: str = ""
    severity: str = "ERROR"


class BeltDrcChecker:
    """Validator for physical belt, underground, and splitter rules."""

    def __init__(
        self,
        allow_peeling: bool = False,
        rules_database: dict[str, Any] | None = None,
        rules_path: Path | str | None = None,
    ) -> None:
        self.allow_peeling = allow_peeling
        self.span_limits = dict(MAX_UNDERGROUND_SPAN)
        if rules_database is not None:
            self._apply_rules_database(rules_database)
        elif rules_path is not None and Path(rules_path).is_file():
            db = json.loads(Path(rules_path).read_text())
            self._apply_rules_database(db)

    def _apply_rules_database(self, db: dict[str, Any]) -> None:
        """Apply parameters from rule database."""
        tiers = db.get("belt_tiers", {})
        for tier_data in tiers.values():
            ug_name = tier_data.get("underground")
            max_span = tier_data.get("max_underground_span_tiles")
            if ug_name and max_span:
                self.span_limits[ug_name] = int(max_span)
        for r in db.get("derived_rules", []):
            if r.get("rule_id") == "RULE-05-UNDERGROUND_PAIR":
                spans = r.get("max_span_tiles", {})
                for k, v in spans.items():
                    self.span_limits[k] = int(v)

    def check_entities(
        self,
        entities: list[dict[str, Any]],
        _net_map: dict[str, Any] | None = None,
        input_pins: list[tuple[float, float]] | set[tuple[int, int]] | None = None,
        unresolved_crossings: list[tuple[float, float]] | set[tuple[float, float]] | None = None,
        output_pins: list[tuple[float, float]] | set[tuple[int, int]] | None = None,
        wires: list[list[int]] | None = None,
    ) -> list[DrcViolation]:
        """Verify all physical rules on a list of placed entities."""
        violations: list[DrcViolation] = []

        # Map entities by integer tile coordinate
        tile_map: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for ent in entities:
            pos = ent.get("position", {})
            if isinstance(pos, dict):
                cx = float(pos.get("x", 0))
                cy = float(pos.get("y", 0))
            else:
                cx = float(pos[0])
                cy = float(pos[1])
            name = ent.get("name", "")
            if "splitter" in name:
                d = ent.get("direction", DIRECTION_NORTH)
                if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                    tile_map.setdefault((math.floor(cx - 0.5), math.floor(cy)), []).append(ent)
                    tile_map.setdefault((math.floor(cx + 0.5), math.floor(cy)), []).append(ent)
                else:
                    tile_map.setdefault((math.floor(cx), math.floor(cy - 0.5)), []).append(ent)
                    tile_map.setdefault((math.floor(cx), math.floor(cy + 0.5)), []).append(ent)
            else:
                tile_map.setdefault((math.floor(cx), math.floor(cy)), []).append(ent)

        # 1. Check for head-to-head belt collisions (Rule DRC-01)
        violations.extend(self._check_head_to_head(entities, tile_map))

        # 2. Check underground pairing and maximum span (Rule DRC-02)
        violations.extend(self._check_underground_pairing(entities, tile_map))

        # 3. Check hood clearance for underground belts (Rule DRC-03)
        if not self.allow_peeling:
            violations.extend(self._check_hood_clearance(entities, tile_map))

        # 4. Check splitter side feeds (Rule DRC-04)
        violations.extend(self._check_splitter_clearance(entities, tile_map))

        # 5. Check machine input connectivity and reachability (Rule DRC-05)
        violations.extend(
            self._check_machine_input_connectivity(entities, tile_map, input_pins=input_pins)
        )

        # 6. Check machine output connectivity and reachability (Rule DRC-06)
        violations.extend(
            self._check_machine_output_connectivity(entities, tile_map, output_pins=output_pins)
        )

        # 7. Check underground span overlaps and hood collisions (Rule DRC-07)
        violations.extend(self._check_underground_span_overlap(entities, tile_map))

        # 8. Check surface tile collisions and unresolved crossings (Rule DRC-08)
        violations.extend(
            self._check_surface_collision(tile_map, unresolved_crossings=unresolved_crossings)
        )

        # 9. Check circuit and power switch connectivity (Rules DRC-09..12)
        if wires is not None or any(
            "combinator" in e.get("name", "") or "switch" in e.get("name", "") for e in entities
        ):
            violations.extend(self._check_circuit_connectivity(entities, wires or []))

        return violations

    def _check_head_to_head(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Detect opposing belts that point directly at each other."""
        violations: list[DrcViolation] = []
        for ent in entities:
            name = ent.get("name", "")
            if name != "transport-belt":
                continue
            d = ent.get("direction", DIRECTION_NORTH)
            if d not in DIR_STEPS:
                continue
            pos = ent.get("position", {})
            x = int(pos.get("x", 0)) if isinstance(pos, dict) else int(pos[0])
            y = int(pos.get("y", 0)) if isinstance(pos, dict) else int(pos[1])

            dx, dy = DIR_STEPS[d]
            target_pos = (x + dx, y + dy)
            target_entities = tile_map.get(target_pos, [])
            for target in target_entities:
                if target.get("name") != "transport-belt":
                    continue
                target_d = target.get("direction", DIRECTION_NORTH)
                if target_d == OPPOSITE_DIR.get(d):
                    violations.append(
                        DrcViolation(
                            code="DRC-01",
                            message=f"Opposing belts face each other at ({x}, {y}) and {target_pos}",
                            x=x,
                            y=y,
                        )
                    )
        return violations

    def _check_underground_pairing(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Verify that every underground input pairs with an output within maximum span."""
        violations: list[DrcViolation] = []
        inputs = [
            e
            for e in entities
            if "underground-belt" in e.get("name", "") and e.get("type") == "input"
        ]
        outputs = [
            e
            for e in entities
            if "underground-belt" in e.get("name", "") and e.get("type") == "output"
        ]

        matched_outputs: set[int] = set()

        for u_in in inputs:
            name = u_in.get("name", "underground-belt")
            d = u_in.get("direction", DIRECTION_NORTH)
            pos = u_in.get("position", {})
            x = int(pos.get("x", 0)) if isinstance(pos, dict) else int(pos[0])
            y = int(pos.get("y", 0)) if isinstance(pos, dict) else int(pos[1])

            max_span = self.span_limits.get(name, 5)
            dx, dy = DIR_STEPS.get(d, (0, 0))

            found = False
            for span in range(1, max_span + 1):
                chk_pos = (x + dx * span, y + dy * span)
                candidates = tile_map.get(chk_pos, [])
                for c in candidates:
                    if (
                        c.get("name") == name
                        and c.get("direction") == d
                        and c.get("type") == "output"
                    ):
                        found = True
                        matched_outputs.add(id(c))
                        break
                if found:
                    break

            if not found:
                violations.append(
                    DrcViolation(
                        code="DRC-02",
                        message=f"Underground input at ({x}, {y}) has no matching output within span {max_span}",
                        x=x,
                        y=y,
                    )
                )

        for u_out in outputs:
            if id(u_out) not in matched_outputs:
                pos = u_out.get("position", {})
                x = int(pos.get("x", 0)) if isinstance(pos, dict) else int(pos[0])
                y = int(pos.get("y", 0)) if isinstance(pos, dict) else int(pos[1])
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
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Ensure no perpendicular belt feeds into the side of an underground hood."""
        violations: list[DrcViolation] = []
        for ent in entities:
            if "underground-belt" not in ent.get("name", ""):
                continue
            hood_d = ent.get("direction", DIRECTION_NORTH)
            pos = ent.get("position", {})
            hx = int(pos.get("x", 0)) if isinstance(pos, dict) else int(pos[0])
            hy = int(pos.get("y", 0)) if isinstance(pos, dict) else int(pos[1])

            if hood_d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                side_dirs = [DIRECTION_EAST, DIRECTION_WEST]
            else:
                side_dirs = [DIRECTION_NORTH, DIRECTION_SOUTH]

            for s_dir in side_dirs:
                sdx, sdy = DIR_STEPS[s_dir]
                nb_pos = (hx - sdx, hy - sdy)
                violations.extend(
                    [
                        DrcViolation(
                            code="DRC-03",
                            message=f"Perpendicular belt at {nb_pos} feeds into underground hood side at ({hx}, {hy})",
                            x=nb_pos[0],
                            y=nb_pos[1],
                        )
                        for nb in tile_map.get(nb_pos, [])
                        if (
                            "transport-belt" in nb.get("name", "")
                            or "splitter" in nb.get("name", "")
                        )
                        and nb.get("direction") == s_dir
                    ]
                )

            # Check for surface belt feeding into rear of underground output hood
            if ent.get("type") == "output":
                dx, dy = DIR_STEPS[hood_d]
                rear_pos = (hx - dx, hy - dy)
                violations.extend(
                    [
                        DrcViolation(
                            code="DRC-03",
                            message=f"Surface belt at {rear_pos} feeds into rear of underground output hood at ({hx}, {hy})",
                            x=rear_pos[0],
                            y=rear_pos[1],
                        )
                        for nb in tile_map.get(rear_pos, [])
                        if (
                            "transport-belt" in nb.get("name", "")
                            or "splitter" in nb.get("name", "")
                        )
                        and nb.get("direction") == hood_d
                    ]
                )
        return violations

    def _check_splitter_clearance(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Ensure no perpendicular belt feeds into the flank of a splitter."""
        violations: list[DrcViolation] = []
        for ent in entities:
            if "splitter" not in ent.get("name", ""):
                continue
            d = ent.get("direction", DIRECTION_NORTH)
            pos = ent.get("position", {})
            sx = int(pos.get("x", 0)) if isinstance(pos, dict) else int(pos[0])
            sy = int(pos.get("y", 0)) if isinstance(pos, dict) else int(pos[1])

            if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                flank_dirs = [DIRECTION_EAST, DIRECTION_WEST]
            else:
                flank_dirs = [DIRECTION_NORTH, DIRECTION_SOUTH]

            for f_dir in flank_dirs:
                fdx, fdy = DIR_STEPS[f_dir]
                flank_pos = (sx - fdx, sy - fdy)
                violations.extend(
                    [
                        DrcViolation(
                            code="DRC-04",
                            message=f"Perpendicular belt at {flank_pos} feeds into splitter flank at ({sx}, {sy})",
                            x=flank_pos[0],
                            y=flank_pos[1],
                        )
                        for nb in tile_map.get(flank_pos, [])
                        if nb.get("name") == "transport-belt" and nb.get("direction") == f_dir
                    ]
                )
        return violations

    def _check_machine_input_connectivity(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
        input_pins: list[tuple[float, float]] | set[tuple[int, int]] | None = None,
    ) -> list[DrcViolation]:
        """Verify that every machine input inserter has reachable upstream supply."""
        violations: list[DrcViolation] = []

        # 1. Map all machine-occupied tiles
        machine_tiles: set[tuple[int, int]] = set()
        for ent in entities:
            name = ent.get("name", "")
            if "switch" in name or "splitter" in name or "pole" in name:
                continue
            w_f, h_f = entity_size(name)
            if w_f >= 2.0 or h_f >= 2.0:
                w, h = int(w_f), int(h_f)
                pos = ent.get("position", {})
                cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                min_x = math.floor(cx - w / 2.0)
                min_y = math.floor(cy - h / 2.0)
                for tx in range(min_x, min_x + w):
                    for ty in range(min_y, min_y + h):
                        machine_tiles.add((tx, ty))

        # 2. Map container/chest tiles
        container_tiles: set[tuple[int, int]] = set()
        for ent in entities:
            name = ent.get("name", "")
            if "chest" in name or "container" in name:
                pos = ent.get("position", {})
                cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                container_tiles.add((math.floor(cx), math.floor(cy)))

        # 3. Identify machine input inserters and machine output drop tiles
        machine_input_inserters: list[
            tuple[dict[str, Any], int, int, tuple[int, int], tuple[int, int]]
        ] = []
        machine_output_drop_tiles: set[tuple[int, int]] = set()

        for ent in entities:
            if "inserter" not in ent.get("name", ""):
                continue
            pos = ent.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            ix = math.floor(cx)
            iy = math.floor(cy)
            d = ent.get("direction", DIRECTION_NORTH)
            mult = 2 if "long-handed" in ent.get("name", "") else 1
            offsets = INSERTER_OFFSETS.get(d, ((0, -1), (0, 1)))
            pu_dx, pu_dy = offsets[0][0] * mult, offsets[0][1] * mult
            dr_dx, dr_dy = offsets[1][0] * mult, offsets[1][1] * mult
            pickup_tile = (ix + pu_dx, iy + pu_dy)
            drop_tile = (ix + dr_dx, iy + dr_dy)

            if drop_tile in machine_tiles:
                machine_input_inserters.append((ent, ix, iy, pickup_tile, drop_tile))
            elif pickup_tile in machine_tiles:
                machine_output_drop_tiles.add(drop_tile)

        # 4. Determine BFS supply sources
        sources: set[tuple[int, int]] = set(machine_output_drop_tiles) | container_tiles
        if input_pins is not None:
            for pin in input_pins:
                if isinstance(pin, (list, tuple)) and len(pin) >= 2:
                    sources.add((math.floor(pin[0]), math.floor(pin[1])))
        else:
            incoming_targets: set[tuple[int, int]] = set()
            for t_pos, ents in tile_map.items():
                for e in ents:
                    e_name = e.get("name", "")
                    e_dir = e.get("direction", DIRECTION_NORTH)
                    e_dx, e_dy = DIR_STEPS.get(e_dir, (0, 0))
                    if (
                        "transport-belt" in e_name
                        or ("underground-belt" in e_name and e.get("type") == "output")
                        or "splitter" in e_name
                    ):
                        incoming_targets.add((t_pos[0] + e_dx, t_pos[1] + e_dy))
            for t_pos, ents in tile_map.items():
                if t_pos not in incoming_targets and any(
                    "transport-belt" in e.get("name", "")
                    or ("underground-belt" in e.get("name", "") and e.get("type") == "input")
                    for e in ents
                ):
                    sources.add(t_pos)

        # 5. BFS traversal forward from sources
        visited: set[tuple[int, int]] = set(sources)
        queue = collections.deque(sources)

        while queue:
            curr = queue.popleft()
            ents = tile_map.get(curr, [])
            for ent in ents:
                e_name = ent.get("name", "")
                e_dir = ent.get("direction", DIRECTION_NORTH)
                e_dx, e_dy = DIR_STEPS.get(e_dir, (0, 0))

                if "transport-belt" in e_name:
                    next_tile = (curr[0] + e_dx, curr[1] + e_dy)
                    next_ents = tile_map.get(next_tile, [])
                    for ne in next_ents:
                        ne_name = ne.get("name", "")
                        ne_dir = ne.get("direction", DIRECTION_NORTH)
                        if next_tile not in visited and (
                            ("transport-belt" in ne_name and ne_dir != OPPOSITE_DIR.get(e_dir))
                            or (
                                "underground-belt" in ne_name
                                and ne.get("type") == "input"
                                and ne_dir == e_dir
                            )
                            or ("splitter" in ne_name and ne_dir == e_dir)
                        ):
                            visited.add(next_tile)
                            queue.append(next_tile)
                elif "underground-belt" in e_name:
                    u_type = ent.get("type", "")
                    if u_type == "input":
                        max_span = self.span_limits.get(e_name, 5)
                        for span in range(1, max_span + 1):
                            out_pos = (curr[0] + e_dx * span, curr[1] + e_dy * span)
                            for cand in tile_map.get(out_pos, []):
                                if (
                                    cand.get("name") == e_name
                                    and cand.get("direction") == e_dir
                                    and cand.get("type") == "output"
                                ):
                                    if out_pos not in visited:
                                        visited.add(out_pos)
                                        queue.append(out_pos)
                                    front_tile = (out_pos[0] + e_dx, out_pos[1] + e_dy)
                                    if front_tile not in visited:
                                        visited.add(front_tile)
                                        queue.append(front_tile)
                                    break
                    elif u_type == "output":
                        front_tile = (curr[0] + e_dx, curr[1] + e_dy)
                        if front_tile not in visited:
                            visited.add(front_tile)
                            queue.append(front_tile)
                elif "splitter" in e_name:
                    pos = ent.get("position", {})
                    scx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                    scy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                    if e_dir in (DIRECTION_NORTH, DIRECTION_SOUTH):
                        half1 = (math.floor(scx - 0.5), math.floor(scy))
                        half2 = (math.floor(scx + 0.5), math.floor(scy))
                    else:
                        half1 = (math.floor(scx), math.floor(scy - 0.5))
                        half2 = (math.floor(scx), math.floor(scy + 0.5))
                    out1 = (half1[0] + e_dx, half1[1] + e_dy)
                    out2 = (half2[0] + e_dx, half2[1] + e_dy)
                    for t in (half1, half2, out1, out2):
                        if t not in visited:
                            visited.add(t)
                            queue.append(t)

        # 6. Check machine input inserters
        for _ent, ix, iy, pickup_tile, _drop_tile in machine_input_inserters:
            pickup_ents = tile_map.get(pickup_tile, [])
            has_supply_ent = any(
                "transport-belt" in e.get("name", "")
                or "underground-belt" in e.get("name", "")
                or "splitter" in e.get("name", "")
                or "chest" in e.get("name", "")
                or "container" in e.get("name", "")
                for e in pickup_ents
            ) or (pickup_tile in machine_tiles)

            if not has_supply_ent:
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
            elif (
                pickup_tile not in visited
                and pickup_tile not in machine_tiles
                and pickup_tile not in container_tiles
            ):
                violations.append(
                    DrcViolation(
                        code="DRC-05",
                        message=(
                            f"Machine input inserter at ({ix}, {iy}) is not reachable "
                            f"from upstream supply (pickup at {pickup_tile})"
                        ),
                        x=ix,
                        y=iy,
                    )
                )

        return violations

    def _check_machine_output_connectivity(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
        output_pins: list[tuple[float, float]] | set[tuple[int, int]] | None = None,
    ) -> list[DrcViolation]:
        """Verify that every machine output inserter drops onto a valid receiver entity and reaches a sink."""
        violations: list[DrcViolation] = []

        # 1. Map all machine-occupied tiles
        machine_tiles: set[tuple[int, int]] = set()
        for ent in entities:
            name = ent.get("name", "")
            if "switch" in name or "splitter" in name or "pole" in name:
                continue
            w_f, h_f = entity_size(name)
            if w_f >= 2.0 or h_f >= 2.0:
                w, h = int(w_f), int(h_f)
                pos = ent.get("position", {})
                cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                min_x = math.floor(cx - w / 2.0)
                min_y = math.floor(cy - h / 2.0)
                for tx in range(min_x, min_x + w):
                    for ty in range(min_y, min_y + h):
                        machine_tiles.add((tx, ty))

        # 2. Map all transport and supply tiles
        container_tiles: set[tuple[int, int]] = set()
        supply_tiles: set[tuple[int, int]] = set()
        for ent in entities:
            name = ent.get("name", "")
            pos = ent.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            if "transport-belt" in name or "underground-belt" in name:
                supply_tiles.add((math.floor(cx), math.floor(cy)))
            elif "splitter" in name:
                d = ent.get("direction", DIRECTION_NORTH)
                if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                    supply_tiles.add((math.floor(cx - 0.5), math.floor(cy)))
                    supply_tiles.add((math.floor(cx + 0.5), math.floor(cy)))
                else:
                    supply_tiles.add((math.floor(cx), math.floor(cy - 0.5)))
                    supply_tiles.add((math.floor(cx), math.floor(cy + 0.5)))
            elif "chest" in name or "container" in name:
                supply_tiles.add((math.floor(cx), math.floor(cy)))
                container_tiles.add((math.floor(cx), math.floor(cy)))

        # 3. Identify machine input pickup tiles and machine output inserters
        sink_tiles: set[tuple[int, int]] = set(container_tiles)
        if output_pins is not None:
            for pin in output_pins:
                if isinstance(pin, (list, tuple)) and len(pin) >= 2:
                    sink_tiles.add((math.floor(pin[0]), math.floor(pin[1])))

        output_inserters: list[tuple[int, int, tuple[int, int]]] = []
        for ent in entities:
            if "inserter" not in ent.get("name", ""):
                continue
            pos = ent.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            ix = math.floor(cx)
            iy = math.floor(cy)
            d = ent.get("direction", DIRECTION_NORTH)
            mult = 2 if "long-handed" in ent.get("name", "") else 1
            offsets = INSERTER_OFFSETS.get(d, ((0, -1), (0, 1)))
            pu_dx, pu_dy = offsets[0][0] * mult, offsets[0][1] * mult
            dr_dx, dr_dy = offsets[1][0] * mult, offsets[1][1] * mult
            pickup_tile = (ix + pu_dx, iy + pu_dy)
            drop_tile = (ix + dr_dx, iy + dr_dy)

            if drop_tile in machine_tiles:
                sink_tiles.add(pickup_tile)
            elif pickup_tile in machine_tiles:
                output_inserters.append((ix, iy, drop_tile))

        # 4. Check connectivity and downstream reachability for machine outputs
        for ix, iy, drop_tile in output_inserters:
            if drop_tile not in supply_tiles and drop_tile not in machine_tiles:
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
                continue

            # When the design defines sinks, verify reachability from drop_tile
            if sink_tiles:
                visited: set[tuple[int, int]] = {drop_tile}
                queue = collections.deque([drop_tile])
                reached = False
                dead_end_tile: tuple[int, int] | None = None

                while queue:
                    curr = queue.popleft()
                    if curr in sink_tiles:
                        reached = True
                        break
                    ents = tile_map.get(curr, [])
                    if not ents:
                        dead_end_tile = curr
                        continue
                    for e in ents:
                        e_name = e.get("name", "")
                        e_dir = e.get("direction", DIRECTION_NORTH)
                        e_dx, e_dy = DIR_STEPS.get(e_dir, (0, 0))

                        if "transport-belt" in e_name:
                            next_tile = (curr[0] + e_dx, curr[1] + e_dy)
                            if next_tile in sink_tiles:
                                reached = True
                                break
                            next_ents = tile_map.get(next_tile, [])
                            for ne in next_ents:
                                ne_name = ne.get("name", "")
                                ne_dir = ne.get("direction", DIRECTION_NORTH)
                                if next_tile not in visited and (
                                    (
                                        "transport-belt" in ne_name
                                        and ne_dir != OPPOSITE_DIR.get(e_dir)
                                    )
                                    or (
                                        "underground-belt" in ne_name
                                        and ne.get("type") == "input"
                                        and ne_dir == e_dir
                                    )
                                    or ("splitter" in ne_name and ne_dir == e_dir)
                                ):
                                    visited.add(next_tile)
                                    queue.append(next_tile)
                        elif "underground-belt" in e_name:
                            u_type = e.get("type", "")
                            if u_type == "input":
                                max_span = self.span_limits.get(e_name, 5)
                                for span in range(1, max_span + 1):
                                    out_pos = (curr[0] + e_dx * span, curr[1] + e_dy * span)
                                    for cand in tile_map.get(out_pos, []):
                                        if (
                                            cand.get("name") == e_name
                                            and cand.get("direction") == e_dir
                                            and cand.get("type") == "output"
                                        ):
                                            if out_pos not in visited:
                                                visited.add(out_pos)
                                                queue.append(out_pos)
                                            front_tile = (out_pos[0] + e_dx, out_pos[1] + e_dy)
                                            if front_tile in sink_tiles:
                                                reached = True
                                                break
                                            if front_tile not in visited:
                                                visited.add(front_tile)
                                                queue.append(front_tile)
                                            break
                                    if reached:
                                        break
                            elif u_type == "output":
                                front_tile = (curr[0] + e_dx, curr[1] + e_dy)
                                if front_tile in sink_tiles:
                                    reached = True
                                    break
                                if front_tile not in visited:
                                    visited.add(front_tile)
                                    queue.append(front_tile)
                        elif "splitter" in e_name:
                            pos = e.get("position", {})
                            scx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                            scy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                            if e_dir in (DIRECTION_NORTH, DIRECTION_SOUTH):
                                half1 = (math.floor(scx - 0.5), math.floor(scy))
                                half2 = (math.floor(scx + 0.5), math.floor(scy))
                            else:
                                half1 = (math.floor(scx), math.floor(scy - 0.5))
                                half2 = (math.floor(scx), math.floor(scy + 0.5))
                            out1 = (half1[0] + e_dx, half1[1] + e_dy)
                            out2 = (half2[0] + e_dx, half2[1] + e_dy)
                            for t in (half1, half2, out1, out2):
                                if t in sink_tiles:
                                    reached = True
                                    break
                                if t not in visited:
                                    visited.add(t)
                                    queue.append(t)
                            if reached:
                                break
                    if reached:
                        break

                if not reached:
                    violations.append(
                        DrcViolation(
                            code="DRC-06",
                            message=(
                                f"Machine output inserter at ({ix}, {iy}) is not reachable to "
                                f"any downstream consumer, container, or output pin (dead-end near {dead_end_tile})"
                            ),
                            x=ix,
                            y=iy,
                        )
                    )

        return violations

    def _check_underground_span_overlap(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Detect underground belt hoods inside active tunnel spans or parallel span overlaps."""
        violations: list[DrcViolation] = []
        inputs = [
            e
            for e in entities
            if "underground-belt" in e.get("name", "") and e.get("type") == "input"
        ]

        # Map each valid underground pair to its intermediate span tiles
        spans: list[tuple[tuple[int, int], tuple[int, int], int, list[tuple[int, int]]]] = []
        for u_in in inputs:
            name = u_in.get("name", "underground-belt")
            d = u_in.get("direction", DIRECTION_NORTH)
            pos = u_in.get("position", {})
            x = int(pos.get("x", 0)) if isinstance(pos, dict) else int(pos[0])
            y = int(pos.get("y", 0)) if isinstance(pos, dict) else int(pos[1])

            max_span = self.span_limits.get(name, 5)
            dx, dy = DIR_STEPS.get(d, (0, 0))

            for span in range(1, max_span + 1):
                chk_pos = (x + dx * span, y + dy * span)
                candidates = tile_map.get(chk_pos, [])
                matched = any(
                    c.get("name") == name and c.get("direction") == d and c.get("type") == "output"
                    for c in candidates
                )
                if matched:
                    span_tiles = [(x + dx * s, y + dy * s) for s in range(1, span)]
                    spans.append(((x, y), chk_pos, d, span_tiles))
                    break

        # Check A: No underground belt hood may sit on an intermediate span tile
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
                    for ent in tile_map.get((sx, sy), [])
                    if "underground-belt" in ent.get("name", "")
                )

        # Check B: No parallel underground tunnel spans may overlap
        horiz_spans: dict[tuple[int, int], tuple[tuple[int, int], tuple[int, int]]] = {}
        vert_spans: dict[tuple[int, int], tuple[tuple[int, int], tuple[int, int]]] = {}

        for (ix, iy), (ox, oy), d, span_tiles in spans:
            is_horiz = d in (DIRECTION_EAST, DIRECTION_WEST)
            axis_map = horiz_spans if is_horiz else vert_spans
            axis_name = "horizontal" if is_horiz else "vertical"
            for st in span_tiles:
                if st in axis_map:
                    other_in, other_out = axis_map[st]
                    violations.append(
                        DrcViolation(
                            code="DRC-07",
                            message=(
                                f"Parallel {axis_name} underground tunnel span collision at {st} "
                                f"between span ({ix}, {iy})->({ox}, {oy}) and {other_in}->{other_out}"
                            ),
                            x=st[0],
                            y=st[1],
                        )
                    )
                else:
                    axis_map[st] = ((ix, iy), (ox, oy))

        return violations

    def _check_surface_collision(
        self,
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
        unresolved_crossings: list[tuple[float, float]] | set[tuple[float, float]] | None = None,
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
                names = [e.get("name", "unknown") for e in ents]
                violations.append(
                    DrcViolation(
                        code="DRC-08",
                        message=f"Surface tile collision between {len(ents)} entities {names} at ({pos[0]}, {pos[1]})",
                        x=float(pos[0]),
                        y=float(pos[1]),
                    )
                )

        return violations

    def _check_circuit_connectivity(
        self,
        entities: list[dict[str, Any]],
        wires: list[list[int]],
    ) -> list[DrcViolation]:
        """Verify circuit network connectivity and wire length limits (Rules DRC-09..12)."""
        violations: list[DrcViolation] = []
        ent_by_num: dict[int, dict[str, Any]] = {}
        for ent in entities:
            num = ent.get("entity_number")
            if num:
                ent_by_num[num] = ent

        # Map each entity number to its connected connector IDs
        ent_conns: dict[int, set[int]] = collections.defaultdict(set)
        for w in wires:
            if len(w) >= 4:
                id1, c1, id2, c2 = w[0], w[1], w[2], w[3]
                ent_conns[id1].add(c1)
                ent_conns[id2].add(c2)

                # Rule DRC-09: Wire reach distance <= 9.0 tiles
                e1 = ent_by_num.get(id1)
                e2 = ent_by_num.get(id2)
                if e1 and e2:
                    p1 = e1.get("position", {})
                    p2 = e2.get("position", {})
                    x1, y1 = float(p1.get("x", 0)), float(p1.get("y", 0))
                    x2, y2 = float(p2.get("x", 0)), float(p2.get("y", 0))
                    dist = math.hypot(x1 - x2, y1 - y2)
                    if dist > 9.0:
                        violations.append(
                            DrcViolation(
                                code="DRC-09",
                                message=f"Wire distance {dist:.2f} between {e1.get('name')} (#{id1}) and {e2.get('name')} (#{id2}) exceeds 9.0 tiles limit",
                                x=x1,
                                y=y1,
                            )
                        )

        for ent in entities:
            ename = ent.get("name", "")
            num = ent.get("entity_number")
            pos = ent.get("position", {})
            x = float(pos.get("x", 0))
            y = float(pos.get("y", 0))
            conns = ent_conns.get(num, set()) if num else set()

            # Rule DRC-10: Combinator input and output connectivity
            if ename in ("decider-combinator", "arithmetic-combinator"):
                has_input = (1 in conns) or (2 in conns)
                has_output = (3 in conns) or (4 in conns)
                if not has_input:
                    violations.append(
                        DrcViolation(
                            code="DRC-10",
                            message=f"Combinator {ename} (#{num}) at ({x}, {y}) has unconnected inputs",
                            x=x,
                            y=y,
                        )
                    )
                if not has_output:
                    violations.append(
                        DrcViolation(
                            code="DRC-10",
                            message=f"Combinator {ename} (#{num}) at ({x}, {y}) has unconnected outputs",
                            x=x,
                            y=y,
                        )
                    )

            # Rule DRC-11: Constant combinator connectivity
            elif ename == "constant-combinator":
                has_output = (1 in conns) or (2 in conns)
                if not has_output:
                    violations.append(
                        DrcViolation(
                            code="DRC-11",
                            message=f"Constant combinator (#{num}) at ({x}, {y}) has no wire connections",
                            x=x,
                            y=y,
                        )
                    )

            # Rule DRC-12: Power switch connectivity (circuit and both copper terminals)
            elif ename == "power-switch":
                has_circuit = (1 in conns) or (2 in conns)
                has_left_cu = 5 in conns
                has_right_cu = 6 in conns
                if not has_circuit:
                    violations.append(
                        DrcViolation(
                            code="DRC-12",
                            message=f"Power switch (#{num}) at ({x}, {y}) has no circuit wire connection",
                            x=x,
                            y=y,
                        )
                    )
                if not has_left_cu or not has_right_cu:
                    violations.append(
                        DrcViolation(
                            code="DRC-12",
                            message=f"Power switch (#{num}) at ({x}, {y}) missing copper terminal connections (left={has_left_cu}, right={has_right_cu})",
                            x=x,
                            y=y,
                        )
                    )

        return violations
