"""Physical Design Rule Check (DRC) engine for Factorio layouts."""

from __future__ import annotations

import collections
import itertools
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
    from foundry.entity_helpers import (
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
        self.max_pipeline_extent = 320
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
            elif r.get("rule_id") == "RULE-16-PIPELINE_EXTENT":
                self.max_pipeline_extent = int(r.get("max_extent_tiles", 320))

    def check_entities(
        self,
        entities: list[dict[str, Any]],
        _net_map: dict[str, Any] | None = None,
        input_pins: list[tuple[float, float]] | set[tuple[int, int]] | None = None,
        unresolved_crossings: list[tuple[float, float]] | set[tuple[float, float]] | None = None,
        output_pins: list[tuple[float, float]] | set[tuple[int, int]] | None = None,
        wires: list[list[int]] | None = None,
        tile_to_nets: dict[tuple[float, float], set[str]] | None = None,
        fluids: set[str] | frozenset[str] | None = None,
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
            elif "pump" in name:
                d = ent.get("direction", DIRECTION_NORTH)
                if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                    tile_map.setdefault((math.floor(cx), math.floor(cy - 0.5)), []).append(ent)
                    tile_map.setdefault((math.floor(cx), math.floor(cy + 0.5)), []).append(ent)
                else:
                    tile_map.setdefault((math.floor(cx - 0.5), math.floor(cy)), []).append(ent)
                    tile_map.setdefault((math.floor(cx + 0.5), math.floor(cy)), []).append(ent)
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

        # 10. Check fluid mixing between adjacent surface pipes (Rule DRC-13)
        violations.extend(
            self._check_fluid_mixing(
                entities,
                tile_map,
                tile_to_nets=tile_to_nets,
                fluids=fluids,
                net_map=_net_map,
            )
        )

        # 11. Check pipe-to-ground pairing and maximum span (Rule DRC-14)
        violations.extend(self._check_pipe_pairing(entities, tile_map))

        # 12. Check pipe-to-ground underground span overlaps and collisions (Rule DRC-15)
        violations.extend(self._check_pipe_span_overlap(entities, tile_map))

        # 13. Check pipeline extent limit (Rule DRC-16)
        violations.extend(self._check_pipeline_extent(entities, tile_map))

        # 14. Check electric pole power coverage and PDN connectivity (Rule DRC-17)
        if any(is_electric_pole(e.get("name", "")) for e in entities):
            violations.extend(self._check_power_coverage(entities, wires or []))

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

        # 4. Determine BFS supply sources and validate external input pin loader clearance
        sources: set[tuple[int, int]] = set(machine_output_drop_tiles) | container_tiles
        reserved_loader_tiles: set[tuple[int, int]] = set()
        if input_pins is not None:
            for pin in input_pins:
                if isinstance(pin, (list, tuple)) and len(pin) >= 2:
                    px, py = math.floor(pin[0]), math.floor(pin[1])
                    pin_ents = tile_map.get((px, py), [])
                    belt_ent = next(
                        (
                            e
                            for e in pin_ents
                            if "transport-belt" in e.get("name", "")
                            or "underground-belt" in e.get("name", "")
                        ),
                        None,
                    )
                    if belt_ent is not None:
                        b_dir = belt_ent.get("direction", DIRECTION_NORTH)
                        dx, dy = DIR_STEPS.get(b_dir, (0, -1))
                        loader_tiles = (
                            (px - dx, py - dy),
                            (px - 2 * dx, py - 2 * dy),
                            (px - 3 * dx, py - 3 * dy),
                        )
                        blocked_loader = any(
                            bool(tile_map.get(lt)) or lt in reserved_loader_tiles
                            for lt in loader_tiles
                        )
                        bad_boundary_dir = (px == 0 and b_dir != DIRECTION_EAST) or (
                            py == 0 and px > 0 and b_dir != DIRECTION_SOUTH
                        )
                        if blocked_loader or bad_boundary_dir:
                            violations.append(
                                DrcViolation(
                                    code="DRC-05",
                                    message=(
                                        f"External input pin at ({px}, {py}) with direction {b_dir} "
                                        f"has blocked or misoriented external loader/infinity-chest "
                                        f"tiles {loader_tiles}"
                                    ),
                                    x=px,
                                    y=py,
                                )
                            )
                            continue
                        reserved_loader_tiles.update(loader_tiles)
                    sources.add((px, py))
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
                pu_desc = [
                    f"{e.get('name')}(dir={e.get('direction')},type={e.get('type')})"
                    for e in pickup_ents
                ]
                nb_desc = []
                for ndx, ndy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                    np = (pickup_tile[0] + ndx, pickup_tile[1] + ndy)
                    n_ents = tile_map.get(np, [])
                    if n_ents:
                        nb_desc.append(
                            f"{np}:vis={np in visited}:"
                            + ",".join(
                                f"{e.get('name')}(d={e.get('direction')},t={e.get('type')})"
                                for e in n_ents
                            )
                        )
                # Trace upstream to find where the unvisited chain starts
                trace_curr = pickup_tile
                trace_seen = {trace_curr}
                trace_path = [trace_curr]
                for _ in range(500):
                    found_prev = None
                    t_ents = tile_map.get(trace_curr, [])
                    for te in t_ents:
                        if "underground-belt" in te.get("name", "") and te.get("type") == "output":
                            td = te.get("direction", DIRECTION_NORTH)
                            tdx, tdy = DIR_STEPS.get(td, (0, 0))
                            max_s = self.span_limits.get(te.get("name", ""), 5)
                            for sp in range(1, max_s + 1):
                                in_p = (trace_curr[0] - tdx * sp, trace_curr[1] - tdy * sp)
                                if any(
                                    ce.get("name") == te.get("name")
                                    and ce.get("direction") == td
                                    and ce.get("type") == "input"
                                    for ce in tile_map.get(in_p, [])
                                ):
                                    found_prev = in_p
                                    break
                        elif "splitter" in te.get("name", ""):
                            spos = te.get("position", {})
                            scx = (
                                float(spos.get("x", 0))
                                if isinstance(spos, dict)
                                else float(spos[0])
                            )
                            scy = (
                                float(spos.get("y", 0))
                                if isinstance(spos, dict)
                                else float(spos[1])
                            )
                            sd = te.get("direction", DIRECTION_NORTH)
                            sdx, sdy = DIR_STEPS.get(sd, (0, 0))
                            if sd in (DIRECTION_NORTH, DIRECTION_SOUTH):
                                h1 = (math.floor(scx - 0.5), math.floor(scy))
                                h2 = (math.floor(scx + 0.5), math.floor(scy))
                            else:
                                h1 = (math.floor(scx), math.floor(scy - 0.5))
                                h2 = (math.floor(scx), math.floor(scy + 0.5))
                            for hp in (h1, h2):
                                bp = (hp[0] - sdx, hp[1] - sdy)
                                if tile_map.get(bp):
                                    found_prev = bp
                                    break
                    if found_prev is None:
                        for ndx, ndy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                            prev_p = (trace_curr[0] - ndx, trace_curr[1] - ndy)
                            for pe in tile_map.get(prev_p, []):
                                pname = pe.get("name", "")
                                pdir = pe.get("direction", DIRECTION_NORTH)
                                if DIR_STEPS.get(pdir) == (ndx, ndy) and (
                                    "transport-belt" in pname
                                    or ("underground-belt" in pname and pe.get("type") == "output")
                                    or "splitter" in pname
                                ):
                                    found_prev = prev_p
                                    break
                            if found_prev is not None:
                                break
                    if found_prev is None or found_prev in trace_seen:
                        break
                    trace_seen.add(found_prev)
                    trace_path.append(found_prev)
                    trace_curr = found_prev
                    if trace_curr in visited:
                        break
                root_ents = [
                    f"{e.get('name')}(d={e.get('direction')},t={e.get('type')})"
                    for e in tile_map.get(trace_curr, [])
                ]
                root_nbs = []
                for ndx, ndy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                    rnp = (trace_curr[0] + ndx, trace_curr[1] + ndy)
                    rn_ents = tile_map.get(rnp, [])
                    if rn_ents:
                        root_nbs.append(
                            f"{rnp}:vis={rnp in visited}:"
                            + ",".join(
                                f"{e.get('name')}(d={e.get('direction')},t={e.get('type')})"
                                for e in rn_ents
                            )
                        )
                violations.append(
                    DrcViolation(
                        code="DRC-05",
                        message=(
                            f"Machine input inserter at ({ix}, {iy}) is not reachable "
                            f"from upstream supply (pickup at {pickup_tile}, ents={pu_desc}, nbs={nb_desc}, "
                            f"upstream_root={trace_curr}(vis={trace_curr in visited}, len={len(trace_path)}, "
                            f"ents={root_ents}, nbs={root_nbs}))"
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
        reserved_out_loader_tiles: set[tuple[int, int]] = set()
        if output_pins is not None:
            for pin in output_pins:
                if isinstance(pin, (list, tuple)) and len(pin) >= 2:
                    px, py = math.floor(pin[0]), math.floor(pin[1])
                    pin_ents = tile_map.get((px, py), [])
                    belt_ent = next(
                        (
                            e
                            for e in pin_ents
                            if "transport-belt" in e.get("name", "")
                            or "underground-belt" in e.get("name", "")
                        ),
                        None,
                    )
                    if belt_ent is not None:
                        b_dir = belt_ent.get("direction", DIRECTION_NORTH)
                        dx, dy = DIR_STEPS.get(b_dir, (0, -1))
                        loader_tiles = (
                            (px + dx, py + dy),
                            (px + 2 * dx, py + 2 * dy),
                            (px + 3 * dx, py + 3 * dy),
                        )
                        blocked_loader = any(
                            bool(tile_map.get(lt)) or lt in reserved_out_loader_tiles
                            for lt in loader_tiles
                        )
                        if blocked_loader:
                            violations.append(
                                DrcViolation(
                                    code="DRC-06",
                                    message=(
                                        f"External output pin at ({px}, {py}) with direction {b_dir} "
                                        f"has blocked external loader/infinity-chest tiles {loader_tiles}"
                                    ),
                                    x=px,
                                    y=py,
                                )
                            )
                            continue
                        reserved_out_loader_tiles.update(loader_tiles)
                    sink_tiles.add((px, py))

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
        known_reaching_sink: set[tuple[int, int]] = set(sink_tiles)
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
                if drop_tile in known_reaching_sink:
                    continue
                visited: set[tuple[int, int]] = {drop_tile}
                parent_tile: dict[tuple[int, int], tuple[int, int]] = {}
                queue = collections.deque([drop_tile])
                reached = False
                reached_from: tuple[int, int] | None = None
                dead_end_tile: tuple[int, int] | None = None

                while queue:
                    curr = queue.popleft()
                    if curr in known_reaching_sink:
                        reached = True
                        reached_from = curr
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
                            if next_tile in known_reaching_sink:
                                reached = True
                                reached_from = curr
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
                                    parent_tile[next_tile] = curr
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
                                                parent_tile[out_pos] = curr
                                                queue.append(out_pos)
                                            front_tile = (out_pos[0] + e_dx, out_pos[1] + e_dy)
                                            if front_tile in known_reaching_sink:
                                                reached = True
                                                reached_from = out_pos
                                                break
                                            if front_tile not in visited:
                                                visited.add(front_tile)
                                                parent_tile[front_tile] = out_pos
                                                queue.append(front_tile)
                                            break
                                    if reached:
                                        break
                            elif u_type == "output":
                                front_tile = (curr[0] + e_dx, curr[1] + e_dy)
                                if front_tile in known_reaching_sink:
                                    reached = True
                                    reached_from = curr
                                    break
                                if front_tile not in visited:
                                    visited.add(front_tile)
                                    parent_tile[front_tile] = curr
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
                                if t in known_reaching_sink:
                                    reached = True
                                    reached_from = curr
                                    break
                                if t not in visited:
                                    visited.add(t)
                                    parent_tile[t] = curr
                                    queue.append(t)
                            if reached:
                                break
                    if reached:
                        break

                if reached:
                    trace_curr = reached_from
                    while trace_curr is not None:
                        known_reaching_sink.add(trace_curr)
                        trace_curr = parent_tile.get(trace_curr)
                else:
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

                # Rule DRC-09: Wire reach distance <= entity wire reach limit
                e1 = ent_by_num.get(id1)
                e2 = ent_by_num.get(id2)
                if e1 and e2:
                    p1 = e1.get("position", {})
                    p2 = e2.get("position", {})
                    x1, y1 = float(p1.get("x", 0)), float(p1.get("y", 0))
                    x2, y2 = float(p2.get("x", 0)), float(p2.get("y", 0))
                    dist = math.hypot(x1 - x2, y1 - y2)
                    reach_limit = min(
                        get_pole_wire_reach(e1.get("name", ""), 9.0),
                        get_pole_wire_reach(e2.get("name", ""), 9.0),
                    )
                    if dist > reach_limit:
                        violations.append(
                            DrcViolation(
                                code="DRC-09",
                                message=f"Wire distance {dist:.2f} between {e1.get('name')} (#{id1}) and {e2.get('name')} (#{id2}) exceeds {reach_limit:.1f} tiles limit",
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

    def _check_fluid_mixing(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
        tile_to_nets: dict[tuple[float, float], set[str]] | None = None,
        fluids: set[str] | frozenset[str] | None = None,
        net_map: dict[str, Any] | None = None,
    ) -> list[DrcViolation]:
        """Detect adjacent surface pipes or pipe connections carrying different fluids (Rule DRC-13)."""
        violations: list[DrcViolation] = []
        known_fluids = set(fluids) if fluids else set()

        # Map each tile to its fluid material if known
        tile_fluids: dict[tuple[int, int], str] = {}

        for ent in entities:
            pos = ent.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            itile = (math.floor(cx), math.floor(cy))

            # 1. Direct fluid annotation
            fluid = ent.get("fluid")
            if fluid:
                tile_fluids[itile] = str(fluid)
                continue

            # 2. Lookup through tile_to_nets
            if tile_to_nets:
                nets = tile_to_nets.get((cx, cy), set()) or tile_to_nets.get(itile, set())
                for n in nets:
                    mat = n.split("__")[0].replace("_", "-")
                    if mat in known_fluids:
                        tile_fluids[itile] = mat
                        break

            # 3. Lookup through net_map
            if itile not in tile_fluids and net_map:
                net_val = net_map.get(itile) or net_map.get((cx, cy))
                if isinstance(net_val, str):
                    mat = net_val.split("__")[0].replace("_", "-")
                    if mat in known_fluids:
                        tile_fluids[itile] = mat
                elif isinstance(net_val, dict):
                    mat = str(net_val.get("material", "")).replace("_", "-")
                    if mat in known_fluids:
                        tile_fluids[itile] = mat

        reported_pairs: set[tuple[tuple[int, int], tuple[int, int]]] = set()

        for ent in entities:
            name = ent.get("name", "")
            if name != "pipe":
                continue
            pos = ent.get("position", {})
            x = math.floor(float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0]))
            y = math.floor(float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1]))
            f1 = tile_fluids.get((x, y))
            if not f1:
                continue

            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                nb_tile = (x + dx, y + dy)
                pair_key = (min((x, y), nb_tile), max((x, y), nb_tile))
                if pair_key in reported_pairs:
                    continue

                nb_ents = tile_map.get(nb_tile, [])
                for nb in nb_ents:
                    nb_name = nb.get("name", "")
                    f2 = tile_fluids.get(nb_tile)
                    if not f2:
                        continue
                    if f1 == f2:
                        continue

                    # Pipe-to-pipe connection
                    if nb_name == "pipe":
                        reported_pairs.add(pair_key)
                        violations.append(
                            DrcViolation(
                                code="DRC-13",
                                message=(
                                    f"Fluid mixing: adjacent pipes carry different fluids "
                                    f"'{f1}' at ({x}, {y}) and '{f2}' at {nb_tile}"
                                ),
                                x=float(x),
                                y=float(y),
                            )
                        )
                        break

                    # Pipe-to-ground connection: only connects on its surface side (where direction points)
                    if nb_name == "pipe-to-ground":
                        nb_dir = nb.get("direction", DIRECTION_NORTH)
                        surf_dx, surf_dy = DIR_STEPS.get(nb_dir, (0, 0))
                        surf_conn = (nb_tile[0] + surf_dx, nb_tile[1] + surf_dy)
                        if surf_conn == (x, y):
                            reported_pairs.add(pair_key)
                            violations.append(
                                DrcViolation(
                                    code="DRC-13",
                                    message=(
                                        f"Fluid mixing: pipe with fluid '{f1}' at ({x}, {y}) "
                                        f"connects into pipe-to-ground with fluid '{f2}' at {nb_tile}"
                                    ),
                                    x=float(x),
                                    y=float(y),
                                )
                            )
                            break

        return violations

    def _check_pipe_pairing(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Verify that every pipe-to-ground entity pairs with a facing pipe within maximum span (Rule DRC-14)."""
        violations: list[DrcViolation] = []
        pipes = [e for e in entities if e.get("name") == "pipe-to-ground"]
        matched: set[int] = set()
        max_span = get_underground_span("pipe-to-ground", 10)

        for p in pipes:
            if id(p) in matched:
                continue
            pos = p.get("position", {})
            px = math.floor(float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0]))
            py = math.floor(float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1]))
            d = p.get("direction", DIRECTION_NORTH)
            ug_d = OPPOSITE_DIR.get(d, d)
            dx, dy = DIR_STEPS.get(ug_d, (0, 0))

            found = None
            for span in range(1, max_span + 1):
                chk_pos = (px + dx * span, py + dy * span)
                candidates = tile_map.get(chk_pos, [])
                for c in candidates:
                    if (
                        c.get("name") == "pipe-to-ground"
                        and c.get("direction") == ug_d
                        and id(c) not in matched
                    ):
                        found = c
                        break
                if found is not None:
                    break

            if found is not None:
                matched.add(id(p))
                matched.add(id(found))
            else:
                violations.append(
                    DrcViolation(
                        code="DRC-14",
                        message=(
                            f"Pipe-to-ground at ({px}, {py}) facing {d} has no matching "
                            f"facing pipe-to-ground within max span 10"
                        ),
                        x=float(px),
                        y=float(py),
                    )
                )

        return violations

    def _check_pipe_span_overlap(
        self,
        entities: list[dict[str, Any]],
        tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Detect pipe-to-ground hoods inside active underground spans or parallel span overlaps (Rule DRC-15)."""
        violations: list[DrcViolation] = []
        pipes = [e for e in entities if e.get("name") == "pipe-to-ground"]
        max_span = get_underground_span("pipe-to-ground", 10)
        matched: set[int] = set()

        spans: list[tuple[tuple[int, int], tuple[int, int], int, list[tuple[int, int]]]] = []
        for p in pipes:
            if id(p) in matched:
                continue
            pos = p.get("position", {})
            px = math.floor(float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0]))
            py = math.floor(float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1]))
            d = p.get("direction", DIRECTION_NORTH)
            ug_d = OPPOSITE_DIR.get(d, d)
            dx, dy = DIR_STEPS.get(ug_d, (0, 0))

            found = None
            found_pos = None
            found_span = 0
            for span in range(1, max_span + 1):
                chk_pos = (px + dx * span, py + dy * span)
                for c in tile_map.get(chk_pos, []):
                    if (
                        c.get("name") == "pipe-to-ground"
                        and c.get("direction") == ug_d
                        and id(c) not in matched
                    ):
                        found = c
                        found_pos = chk_pos
                        found_span = span
                        break
                if found is not None:
                    break

            if found is not None and found_pos is not None:
                matched.add(id(p))
                matched.add(id(found))
                span_tiles = [(px + dx * s, py + dy * s) for s in range(1, found_span)]
                spans.append(((px, py), found_pos, ug_d, span_tiles))

        # Check A: No pipe-to-ground hood on an intermediate span tile
        for (ix, iy), (ox, oy), _d, span_tiles in spans:
            for sx, sy in span_tiles:
                violations.extend(
                    [
                        DrcViolation(
                            code="DRC-15",
                            message=(
                                f"Pipe-to-ground at ({sx}, {sy}) collides with "
                                f"underground pipe span from ({ix}, {iy}) to ({ox}, {oy})"
                            ),
                            x=float(sx),
                            y=float(sy),
                        )
                        for ent in tile_map.get((sx, sy), [])
                        if ent.get("name") == "pipe-to-ground"
                    ]
                )

        # Check B: Parallel underground pipe span collisions
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
                            code="DRC-15",
                            message=(
                                f"Parallel {axis_name} underground pipe span collision at {st} "
                                f"between span ({ix}, {iy})->({ox}, {oy}) and {other_in}->{other_out}"
                            ),
                            x=float(st[0]),
                            y=float(st[1]),
                        )
                    )
                else:
                    axis_map[st] = ((ix, iy), (ox, oy))

        return violations

    def _check_pipeline_extent(
        self,
        entities: list[dict[str, Any]],
        _tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Verify that every connected fluid network does not exceed maximum pipeline extent (Rule DRC-16)."""
        violations: list[DrcViolation] = []
        max_limit = getattr(self, "max_pipeline_extent", 320)
        adj, _, _ = build_conduit_graph(entities)
        components = find_fluid_components(adj)

        for comp in components:
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

    def _check_power_coverage(
        self,
        entities: list[dict[str, Any]],
        wires: list[list[int]],
    ) -> list[DrcViolation]:
        """Verify electric pole coverage and copper PDN connectivity (Rule DRC-17)."""
        violations: list[DrcViolation] = []
        ent_by_num: dict[int, dict[str, Any]] = {}
        for ent in entities:
            num = ent.get("entity_number")
            if num is not None:
                inum = int(num)
                if inum in ent_by_num:
                    pos = ent.get("position", {})
                    x = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                    y = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                    prev = ent_by_num[inum]
                    violations.append(
                        DrcViolation(
                            code="DRC-17",
                            message=(
                                f"Duplicate entity_number #{inum} shared by {ent.get('name')} "
                                f"at ({x}, {y}) and {prev.get('name')}"
                            ),
                            x=x,
                            y=y,
                        )
                    )
                else:
                    ent_by_num[inum] = ent

        poles: list[dict[str, Any]] = [e for e in entities if is_electric_pole(e.get("name", ""))]
        if not poles:
            return violations

        # Build copper wire adjacency across poles and power switches
        copper_adj: dict[int, set[int]] = collections.defaultdict(set)
        for w in wires:
            if len(w) >= 4:
                id1, c1, id2, c2 = int(w[0]), int(w[1]), int(w[2]), int(w[3])
                if c1 in (5, 6) or c2 in (5, 6):
                    e1 = ent_by_num.get(id1)
                    e2 = ent_by_num.get(id2)
                    n1 = e1.get("name", "") if e1 else ""
                    n2 = e2.get("name", "") if e2 else ""
                    valid1 = e1 is not None and (is_electric_pole(n1) or n1 == "power-switch")
                    valid2 = e2 is not None and (is_electric_pole(n2) or n2 == "power-switch")
                    if not valid1 or not valid2:
                        bad_ent = e1 or e2
                        bpos = bad_ent.get("position", {}) if bad_ent else {}
                        bx = float(bpos.get("x", 0)) if isinstance(bpos, dict) else 0.0
                        by = float(bpos.get("y", 0)) if isinstance(bpos, dict) else 0.0
                        violations.append(
                            DrcViolation(
                                code="DRC-17",
                                message=(
                                    f"Copper wire [{id1}, {c1}, {id2}, {c2}] connects invalid "
                                    f"endpoint(s) ({n1 or 'missing'} #{id1}, {n2 or 'missing'} #{id2})"
                                ),
                                x=bx,
                                y=by,
                            )
                        )
                    else:
                        assert e1 is not None and e2 is not None
                        p1 = e1.get("position", {})
                        p2 = e2.get("position", {})
                        x1 = float(p1.get("x", 0)) if isinstance(p1, dict) else float(p1[0])
                        y1 = float(p1.get("y", 0)) if isinstance(p1, dict) else float(p1[1])
                        x2 = float(p2.get("x", 0)) if isinstance(p2, dict) else float(p2[0])
                        y2 = float(p2.get("y", 0)) if isinstance(p2, dict) else float(p2[1])
                        w_dist = math.hypot(x1 - x2, y1 - y2)
                        max_wire = min(get_pole_wire_reach(n1), get_pole_wire_reach(n2))
                        if w_dist > max_wire + 1e-4:
                            violations.append(
                                DrcViolation(
                                    code="DRC-17",
                                    message=(
                                        f"Copper wire [{id1}, {c1}, {id2}, {c2}] length {w_dist:.2f} "
                                        f"exceeds maximum wire reach {max_wire:.1f} between "
                                        f"{n1} #{id1} and {n2} #{id2}"
                                    ),
                                    x=x1,
                                    y=y1,
                                )
                            )
                        else:
                            copper_adj[id1].add(id2)
                            copper_adj[id2].add(id1)

        # Identify main connected component of electric poles when copper wires are present
        main_pole_ids: set[int] = set()
        numbered_poles = [p for p in poles if p.get("entity_number") is not None]
        if wires and len(numbered_poles) > 1:
            visited: set[int] = set()
            best_comp: set[int] = set()
            for p in numbered_poles:
                pnum = int(p["entity_number"])
                if pnum in visited:
                    continue
                comp_poles: set[int] = set()
                queue = collections.deque([pnum])
                visited.add(pnum)
                while queue:
                    curr = queue.popleft()
                    curr_ent = ent_by_num.get(curr)
                    if curr_ent and is_electric_pole(curr_ent.get("name", "")):
                        comp_poles.add(curr)
                    for nb in copper_adj.get(curr, ()):
                        if nb not in visited:
                            visited.add(nb)
                            queue.append(nb)
                if len(comp_poles) > len(best_comp):
                    best_comp = comp_poles
            main_pole_ids = best_comp

        pole_buckets: dict[tuple[int, int], list[tuple[dict[str, Any], float, float, float]]] = {}
        max_pole_radius = 0.0
        for p in poles:
            ppos = p.get("position", {})
            px = float(ppos.get("x", 0)) if isinstance(ppos, dict) else float(ppos[0])
            py = float(ppos.get("y", 0)) if isinstance(ppos, dict) else float(ppos[1])
            p_radius = get_pole_supply_radius(p.get("name", ""))
            if p_radius > max_pole_radius:
                max_pole_radius = p_radius
            pole_buckets.setdefault((int(px // 16), int(py // 16)), []).append(
                (p, px, py, p_radius)
            )

        # Check every electric consumer for pole supply coverage and main PDN connectivity
        consumers = [e for e in entities if is_electric_consumer(e.get("name", ""))]
        for ent in consumers:
            ename = ent.get("name", "")
            pos = ent.get("position", {})
            ex = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            ey = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            cx, cy = entity_collision_half_size(ename, ent.get("direction", DIRECTION_NORTH))
            search_r = max_pole_radius + max(cx, cy)
            min_bx = int((ex - search_r) // 16)
            max_bx = int((ex + search_r) // 16)
            min_by = int((ey - search_r) // 16)
            max_by = int((ey + search_r) // 16)

            covering_poles: list[dict[str, Any]] = []
            for bx in range(min_bx, max_bx + 1):
                for by in range(min_by, max_by + 1):
                    for p, px, py, p_radius in pole_buckets.get((bx, by), ()):
                        max_reach = p_radius + max(cx, cy)
                        if abs(ex - px) > max_reach or abs(ey - py) > max_reach:
                            continue
                        if (
                            max(
                                max(0.0, abs(ex - px) - cx),
                                max(0.0, abs(ey - py) - cy),
                            )
                            <= p_radius
                        ):
                            covering_poles.append(p)

            if not covering_poles:
                violations.append(
                    DrcViolation(
                        code="DRC-17",
                        message=(
                            f"Unpowered entity {ename} at ({ex}, {ey}) is outside "
                            f"electric pole supply area"
                        ),
                        x=ex,
                        y=ey,
                    )
                )
            elif main_pole_ids and not any(
                int(p["entity_number"]) in main_pole_ids
                for p in covering_poles
                if p.get("entity_number") is not None
            ):
                violations.append(
                    DrcViolation(
                        code="DRC-17",
                        message=(
                            f"Entity {ename} at ({ex}, {ey}) is covered only by "
                            f"disconnected electric pole(s) not wired to the main PDN"
                        ),
                        x=ex,
                        y=ey,
                    )
                )

        return violations

    def check_chassis_matrix_discipline(
        self,
        entities: list[dict[str, Any]],
        macro_floorplan: Any,
        router_routes: dict[str, Any] | None = None,
        highway_specs: list[Any] | None = None,
        def_data: dict[str, Any] | None = None,
        netlist_data: dict[str, Any] | None = None,
        bay_blocks: set[tuple[int, int]] | None = None,
    ) -> list[DrcViolation]:
        """Verify chassis port geometry, switchbox discipline, and self-contained cell trees."""
        violations: list[DrcViolation] = []
        active_bays = bay_blocks or set()

        grid_w = int(getattr(macro_floorplan, "grid_w", 1))
        grid_h = int(getattr(macro_floorplan, "grid_h", 1))
        if def_data and (grid_w * grid_h <= 1):
            units = float(def_data.get("units", 1000))
            die_w = def_data.get("die_area", [0, 0, 100 * units, 100 * units])[2] / units
            die_h = def_data.get("die_area", [0, 0, 100 * units, 100 * units])[3] / units
            grid_w = max(1, round(die_w / 100.0))
            grid_h = max(1, round(die_h / 100.0))

        tile_map: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for ent in entities:
            pos = ent.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            tile_map.setdefault((math.floor(cx), math.floor(cy)), []).append(ent)

        chassis_obj = getattr(macro_floorplan, "chassis", None)
        margin = int(getattr(chassis_obj, "core_margin_x", 3))
        chan_w = int(getattr(chassis_obj, "channel_width", 2 * margin))

        def _in_switchbox(tx: int, ty: int) -> bool:
            if (tx + margin) % 100 >= chan_w:
                return False
            if (ty + margin) % 100 < chan_w:
                return True
            if margin <= (ty % 100) <= 16:
                ix = (tx + margin) // 100
                by_i = ty // 100
                return (ix, by_i) in active_bays or (ix - 1, by_i) in active_bays
            return False

        def _street_seg(
            coord: tuple[int, int],
        ) -> tuple[str, int, int] | None:
            x, y = coord
            bx, by = x // 100, y // 100
            rx, ry = x % 100, y % 100
            if (bx, by) in active_bays and 11 <= rx <= 88 and margin <= ry <= 16:
                return ("H", bx, by)
            in_v = (x + margin) % 100 < chan_w
            in_h = (y + margin) % 100 < chan_w
            if in_v and not in_h and 0 <= by < grid_h:
                return ("V", (x + margin) // 100, by)
            if in_h and not in_v and 0 <= bx < grid_w:
                return ("H", bx, (y + margin) // 100)
            return None

        def _core_block(cx: int, cy: int) -> tuple[int, int] | None:
            bx, by = cx // 100, cy // 100
            if not (0 <= bx < grid_w and 0 <= by < grid_h):
                return None
            rx, ry = cx % 100, cy % 100
            min_ry = 17 if (bx, by) in active_bays else margin
            if margin <= rx <= (99 - margin) and min_ry <= ry <= (99 - margin):
                return (bx, by)
            return None

        # 1. PORT_TAP_GEOMETRY
        block_ports = list(getattr(macro_floorplan, "block_ports", []))
        port_channel_coords: set[tuple[int, int]] = set()
        for port in block_ports:
            cx, cy = port.core_pos
            hx, hy = port.channel_pos
            port_channel_coords.add((hx, hy))
            if abs(cx - hx) + abs(cy - hy) != 1:
                violations.append(
                    DrcViolation(
                        code="PORT_TAP_GEOMETRY",
                        message=(
                            f"Port on {port.net_name} at {port.core_pos} -> {port.channel_pos} "
                            f"is not an orthogonal 1-tile step across the cell wall"
                        ),
                        x=float(cx),
                        y=float(cy),
                        net=port.net_name,
                    )
                )
            if port.edge in ("N", "S"):
                lat = cx % 100
                valid_lat = (margin + 2) <= lat <= (99 - margin - 2) and cx == hx
                if port.block in active_bays and port.edge == "N":
                    valid_lat = False
            else:
                lat = cy % 100
                min_lat = 19 if port.block in active_bays else (margin + 2)
                valid_lat = min_lat <= lat <= (99 - margin - 2) and cy == hy
            if not valid_lat:
                violations.append(
                    DrcViolation(
                        code="PORT_TAP_GEOMETRY",
                        message=(
                            f"Port on {port.net_name} at {port.core_pos} (edge {port.edge} of "
                            f"block {port.block}) violates wall coordinate bounds"
                        ),
                        x=float(cx),
                        y=float(cy),
                        net=port.net_name,
                    )
                )
            if entities:
                core_ents = tile_map.get((cx, cy), [])
                if port.is_fluid:
                    ok_ent = any("pipe" in e.get("name", "") for e in core_ents)
                else:
                    ok_ent = any(
                        e.get("name") == "transport-belt"
                        and int(e.get("direction", DIRECTION_NORTH)) == int(port.heading)
                        for e in core_ents
                    )
                if not ok_ent:
                    violations.append(
                        DrcViolation(
                            code="PORT_TAP_GEOMETRY",
                            message=(
                                f"Port on {port.net_name} at {port.core_pos} is missing a "
                                f"perpendicular surface tap entity with heading {port.heading}"
                            ),
                            x=float(cx),
                            y=float(cy),
                            net=port.net_name,
                        )
                    )

        def _route_coords(r_obj: Any) -> list[tuple[int, int]]:
            r_nodes = getattr(r_obj, "nodes", None)
            raw_pts = (
                [(int(n.x), int(n.y)) for n in r_nodes]
                if r_nodes is not None
                else [(int(pt[0]), int(pt[1])) for pt in getattr(r_obj, "path", [])]
            )
            if len(raw_pts) <= 1:
                return raw_pts
            expanded: list[tuple[int, int]] = [raw_pts[0]]
            for (x0, y0), (x1, y1) in itertools.pairwise(raw_pts):
                if x0 == x1 and y0 != y1:
                    step_y = 1 if y1 > y0 else -1
                    expanded.extend((x0, cy_i) for cy_i in range(y0 + step_y, y1 + step_y, step_y))
                elif y0 == y1 and x0 != x1:
                    step_x = 1 if x1 > x0 else -1
                    expanded.extend((cx_i, y0) for cx_i in range(x0 + step_x, x1 + step_x, step_x))
                else:
                    expanded.append((x1, y1))
            return expanded

        # 2. NO_OPPOSITE_WALL_SHORTCUT & 3. SWITCHBOX_ROUTING_DISCIPLINE
        checked_hwy_names: set[str] = set()
        fp_routes = getattr(macro_floorplan, "routes", {})
        route_objs = list(fp_routes.values()) if isinstance(fp_routes, dict) else list(fp_routes)
        for route in route_objs:
            s_port = getattr(route, "source_port", None)
            if s_port is None:
                continue
            s_pos = s_port.channel_pos
            s_seg = _street_seg(s_pos)
            t_ports = list(getattr(route, "target_ports", []))
            for d_port in t_ports:
                d_pos = d_port.channel_pos
                d_seg = _street_seg(d_pos)
                if s_seg is not None and d_seg is not None and s_seg == d_seg:
                    violations.append(
                        DrcViolation(
                            code="NO_OPPOSITE_WALL_SHORTCUT",
                            message=(
                                f"Inter-block route {route.net_name} connects ports on the same "
                                f"street segment {s_seg} ({s_pos} -> {d_pos})"
                            ),
                            x=float(s_pos[0]),
                            y=float(s_pos[1]),
                            net=route.net_name,
                        )
                    )
                for cand_name in (route.net_name, f"{route.net_name}_hwy"):
                    if (
                        router_routes
                        and cand_name in router_routes
                        and cand_name not in checked_hwy_names
                    ):
                        checked_hwy_names.add(cand_name)
                        coords = _route_coords(router_routes[cand_name])
                        if coords and not any(_in_switchbox(tx, ty) for tx, ty in coords):
                            violations.append(
                                DrcViolation(
                                    code="SWITCHBOX_ROUTING_DISCIPLINE",
                                    message=(
                                        f"Highway trunk {cand_name} ({s_pos} -> {d_pos}) "
                                        f"does not pass through any 6x6 corner switchbox"
                                    ),
                                    x=float(s_pos[0]),
                                    y=float(s_pos[1]),
                                    net=cand_name,
                                )
                            )

        for h_spec in highway_specs or []:
            h_name = getattr(h_spec, "name", "")
            s_pos = tuple(getattr(h_spec, "source", (0, 0)))
            d_pos = tuple(getattr(h_spec, "sink", (0, 0)))
            if s_pos not in port_channel_coords and d_pos not in port_channel_coords:
                continue
            s_seg = _street_seg(s_pos)
            d_seg = _street_seg(d_pos)
            if s_seg is not None and d_seg is not None and s_seg == d_seg:
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
            if router_routes and h_name in router_routes and h_name not in checked_hwy_names:
                checked_hwy_names.add(h_name)
                coords = _route_coords(router_routes[h_name])
                if coords and not any(_in_switchbox(tx, ty) for tx, ty in coords):
                    violations.append(
                        DrcViolation(
                            code="SWITCHBOX_ROUTING_DISCIPLINE",
                            message=(
                                f"Highway net {h_name} ({s_pos} -> {d_pos}) "
                                f"does not pass through any 6x6 corner switchbox"
                            ),
                            x=float(s_pos[0]),
                            y=float(s_pos[1]),
                            net=h_name,
                        )
                    )

        # 4. CELL_SELF_CONTAINED_TREES
        if def_data:
            scale = 1.0 / float(def_data.get("units", 1000))
            comp_map = {c["name"]: c for c in def_data.get("components", [])}
            inst_map = {i["name"]: i for i in (netlist_data or {}).get("instances", [])}
            cells_map = (netlist_data or {}).get("cells", {})

            comp_block: dict[str, tuple[int, int] | None] = {}
            comp_pos: dict[str, tuple[int, int]] = {}
            for cname, comp in comp_map.items():
                cx = round(comp["x"] * scale)
                cy = round(comp["y"] * scale)
                comp_pos[cname] = (cx, cy)
                comp_block[cname] = _core_block(cx, cy)

            def _is_telemetry(iname: str) -> bool:
                if iname.startswith(("u_acc_", "u_disp_")):
                    return True
                comp = comp_map.get(iname, {})
                macro = inst_map.get(iname, {}).get("cell", comp.get("macro", ""))
                return macro in ("display_panel", "arithmetic_combinator") or bool(
                    cells_map.get(macro, {}).get("circuit_cell")
                )

            def _is_drv_port(iname: str, port: str) -> bool:
                comp = comp_map.get(iname, {})
                macro = inst_map.get(iname, {}).get("cell", comp.get("macro", ""))
                if any(k in macro for k in ("split", "merge", "balancer")):
                    return port in ("y", "y1", "y2")
                outs = cells_map.get(macro, {}).get("outputs", [])
                clean_outs = {o.replace("-", "_") for o in outs} | set(outs)
                return (
                    port in clean_outs
                    or port.replace("-", "_") in clean_outs
                    or port
                    in (
                        "y",
                        "out",
                    )
                )

            net_drivers: dict[str, list[str]] = {}
            net_sinks: dict[str, list[str]] = {}
            for net in def_data.get("nets", []):
                nname = net.get("name", "")
                drvs: list[str] = []
                snks: list[str] = []
                for iname, port in net.get("connections", []):
                    if iname == "PIN" or iname not in comp_map or _is_telemetry(iname):
                        continue
                    if _is_drv_port(iname, port):
                        drvs.append(iname)
                    else:
                        snks.append(iname)
                net_drivers[nname] = drvs
                net_sinks[nname] = snks

            for net in def_data.get("nets", []):
                nname = net.get("name", "")
                for iname, port in net.get("connections", []):
                    if iname == "PIN" or iname not in comp_map or _is_telemetry(iname):
                        continue
                    blk = comp_block.get(iname)
                    if blk is None:
                        continue
                    macro = inst_map.get(iname, {}).get("cell", comp_map[iname].get("macro", ""))
                    cx, cy = comp_pos[iname]
                    if ("split" in macro or "balancer" in macro) and _is_drv_port(iname, port):
                        for s_name in net_sinks.get(nname, []):
                            s_blk = comp_block.get(s_name)
                            if s_blk != blk:
                                violations.append(
                                    DrcViolation(
                                        code="CELL_SELF_CONTAINED_TREES",
                                        message=(
                                            f"Splitter {iname} inside cell {blk} drives "
                                            f"{s_name} in {s_blk} on net {nname}"
                                        ),
                                        x=float(cx),
                                        y=float(cy),
                                        net=nname,
                                    )
                                )
                    elif "merge" in macro and not _is_drv_port(iname, port):
                        for d_name in net_drivers.get(nname, []):
                            d_blk = comp_block.get(d_name)
                            if d_blk != blk:
                                violations.append(
                                    DrcViolation(
                                        code="CELL_SELF_CONTAINED_TREES",
                                        message=(
                                            f"Merger {iname} inside cell {blk} receives input "
                                            f"from {d_name} in {d_blk} on net {nname}"
                                        ),
                                        x=float(cx),
                                        y=float(cy),
                                        net=nname,
                                    )
                                )

        return violations
