"""Physical Design Rule Check (DRC) engine for Factorio layouts."""

from __future__ import annotations

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
    ) -> list[DrcViolation]:
        """Verify all physical rules on a list of placed entities."""
        violations: list[DrcViolation] = []

        # Map entities by integer tile coordinate
        tile_map: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for ent in entities:
            pos = ent.get("position", {})
            if isinstance(pos, dict):
                x = int(pos.get("x", 0))
                y = int(pos.get("y", 0))
            else:
                x = int(pos[0])
                y = int(pos[1])
            tile_map.setdefault((x, y), []).append(ent)

        # 1. Check for head-to-head belt collisions (Rule DRC-01)
        violations.extend(self._check_head_to_head(entities, tile_map))

        # 2. Check underground pairing and maximum span (Rule DRC-02)
        violations.extend(self._check_underground_pairing(entities, tile_map))

        # 3. Check hood clearance for underground belts (Rule DRC-03)
        if not self.allow_peeling:
            violations.extend(self._check_hood_clearance(entities, tile_map))

        # 4. Check splitter side feeds (Rule DRC-04)
        violations.extend(self._check_splitter_clearance(entities, tile_map))

        # 5. Check machine input connectivity (Rule DRC-05)
        violations.extend(self._check_machine_input_connectivity(entities, tile_map))

        # 6. Check machine output connectivity (Rule DRC-06)
        violations.extend(self._check_machine_output_connectivity(entities, tile_map))

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
                        if nb.get("name") == "transport-belt" and nb.get("direction") == s_dir
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
        _tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Verify that every machine input inserter has a supply entity at its pickup location."""
        violations: list[DrcViolation] = []

        machine_sizes: dict[str, tuple[int, int]] = {
            "stone-furnace": (2, 2),
            "steel-furnace": (2, 2),
            "electric-furnace": (3, 3),
            "assembling-machine-1": (3, 3),
            "assembling-machine-2": (3, 3),
            "assembling-machine-3": (3, 3),
            "chemical-plant": (3, 3),
            "oil-refinery": (5, 5),
            "centrifuge": (3, 3),
            "lab": (3, 3),
            "rocket-silo": (9, 9),
            "boiler": (2, 3),
            "steam-engine": (3, 5),
            "steam-turbine": (3, 5),
            "nuclear-reactor": (5, 5),
        }

        # 1. Map all machine-occupied tiles
        machine_tiles: set[tuple[int, int]] = set()
        for ent in entities:
            name = ent.get("name", "")
            if (
                name in machine_sizes
                or "furnace" in name
                or "assembling-machine" in name
                or "lab" in name
            ):
                w, h = machine_sizes.get(name, (3, 3))
                pos = ent.get("position", {})
                cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                min_x = math.floor(cx - w / 2.0)
                min_y = math.floor(cy - h / 2.0)
                for tx in range(min_x, min_x + w):
                    for ty in range(min_y, min_y + h):
                        machine_tiles.add((tx, ty))

        # 2. Map all transport and supply tiles
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

        # 3. Check every inserter dropping into a machine
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

            if (
                drop_tile in machine_tiles
                and pickup_tile not in supply_tiles
                and pickup_tile not in machine_tiles
            ):
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

        return violations

    def _check_machine_output_connectivity(
        self,
        entities: list[dict[str, Any]],
        _tile_map: dict[tuple[int, int], list[dict[str, Any]]],
    ) -> list[DrcViolation]:
        """Verify that every machine output inserter drops onto a valid receiver entity."""
        violations: list[DrcViolation] = []

        machine_sizes: dict[str, tuple[int, int]] = {
            "stone-furnace": (2, 2),
            "steel-furnace": (2, 2),
            "electric-furnace": (3, 3),
            "assembling-machine-1": (3, 3),
            "assembling-machine-2": (3, 3),
            "assembling-machine-3": (3, 3),
            "chemical-plant": (3, 3),
            "oil-refinery": (5, 5),
            "centrifuge": (3, 3),
            "lab": (3, 3),
            "rocket-silo": (9, 9),
            "boiler": (2, 3),
            "steam-engine": (3, 5),
            "steam-turbine": (3, 5),
            "nuclear-reactor": (5, 5),
        }

        # 1. Map all machine-occupied tiles
        machine_tiles: set[tuple[int, int]] = set()
        for ent in entities:
            name = ent.get("name", "")
            if (
                name in machine_sizes
                or "furnace" in name
                or "assembling-machine" in name
                or "lab" in name
            ):
                w, h = machine_sizes.get(name, (3, 3))
                pos = ent.get("position", {})
                cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                min_x = math.floor(cx - w / 2.0)
                min_y = math.floor(cy - h / 2.0)
                for tx in range(min_x, min_x + w):
                    for ty in range(min_y, min_y + h):
                        machine_tiles.add((tx, ty))

        # 2. Map all transport and supply tiles
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

        # 3. Check every inserter picking up from a machine
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

            if (
                pickup_tile in machine_tiles
                and drop_tile not in supply_tiles
                and drop_tile not in machine_tiles
            ):
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

        return violations
