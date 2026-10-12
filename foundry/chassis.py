"""Chassis abstraction and factory layout templates.

Defines the parameterizable ChassisTemplate interface and the Type 1
orthogonal CityBlockChassis reference implementation.
"""

from __future__ import annotations

import json
import math
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from foundry.blueprint import decode, encode
from foundry.channel_router import RouterIntersection, StreetChannel
from foundry.entity_helpers import entity_size


class ChassisTemplate(ABC):
    """Abstract interface for structured factory chassis templates."""

    @abstractmethod
    def get_pitch(self) -> tuple[int, int]:
        """Return (pitch_x, pitch_y) in tiles between repeating grid sites."""
        pass

    @abstractmethod
    def get_core_bounds(self, block_x: int, block_y: int) -> tuple[int, int, int, int]:
        """Return (min_x, min_y, width, height) of the usable machine core."""
        pass

    @abstractmethod
    def emit_base_blueprint(self, grid_w: int, grid_h: int) -> dict[str, Any]:
        """Emit base entities and tiles for an N x M chassis grid."""
        pass

    @abstractmethod
    def get_placement_blockages(self, grid_w: int, grid_h: int) -> list[tuple[int, int, int, int]]:
        """Return (x, y, width, height) rectangles covering non-core transit channels."""
        pass


class CityBlockChassis(ChassisTemplate):
    """Type 1 Orthogonal City Block Chassis.

    Parameters:
        pitch: Tile distance between repeating block origins (default 100).
        core_width: Width of the interior machine core (default 94).
        core_height: Height of the interior machine core (default 94).
        channel_width: Width of the straight road channels (default 6).
    """

    def __init__(
        self,
        pitch: int = 100,
        core_width: int = 94,
        core_height: int = 94,
        channel_width: int = 6,
        template_document: dict[str, Any] | None = None,
    ) -> None:
        self.pitch_x = pitch
        self.pitch_y = pitch
        self.core_width = core_width
        self.core_height = core_height
        self.channel_width = channel_width
        self.core_margin_x = (pitch - core_width) // 2
        self.core_margin_y = (pitch - core_height) // 2

        self.template_tiles: list[dict[str, Any]] = []
        self.template_entities: list[dict[str, Any]] = []
        if template_document:
            bp = template_document.get("blueprint", template_document)
            self.template_tiles = bp.get("tiles", [])
            self.template_entities = bp.get("entities", [])

    @classmethod
    def from_blueprint_file(
        cls,
        path: str | Path,
        pitch: int = 100,
        core_width: int = 94,
        core_height: int = 94,
        channel_width: int = 6,
    ) -> CityBlockChassis:
        """Construct CityBlockChassis from an exported blueprint exchange file."""
        with Path(path).open() as f:
            content = f.read().strip()
        doc = json.loads(content) if content.startswith("{") else decode(content)
        return cls(
            pitch=pitch,
            core_width=core_width,
            core_height=core_height,
            channel_width=channel_width,
            template_document=doc,
        )

    @classmethod
    def from_blueprint_string(
        cls,
        blueprint_string: str,
        pitch: int = 100,
        core_width: int = 94,
        core_height: int = 94,
        channel_width: int = 6,
    ) -> CityBlockChassis:
        """Construct CityBlockChassis from a blueprint exchange string."""
        doc = decode(blueprint_string)
        return cls(
            pitch=pitch,
            core_width=core_width,
            core_height=core_height,
            channel_width=channel_width,
            template_document=doc,
        )

    def get_pitch(self) -> tuple[int, int]:
        """Return (pitch_x, pitch_y) in tiles between repeating grid sites."""
        return self.pitch_x, self.pitch_y

    def get_core_bounds(self, block_x: int, block_y: int) -> tuple[int, int, int, int]:
        """Return (min_x, min_y, width, height) of the usable machine core."""
        min_x = block_x * self.pitch_x + self.core_margin_x
        min_y = block_y * self.pitch_y + self.core_margin_y
        return min_x, min_y, self.core_width, self.core_height

    def is_in_core(self, x: float, y: float, block_x: int, block_y: int) -> bool:
        """Check if tile (x, y) sits strictly within the core of block (block_x, block_y)."""
        min_x, min_y, w, h = self.get_core_bounds(block_x, block_y)
        return min_x <= x < min_x + w and min_y <= y < min_y + h

    def get_block_index(self, x: float, y: float) -> tuple[int, int]:
        """Return (block_x, block_y) coordinate for given physical coordinates."""
        bx = int(x // self.pitch_x)
        by = int(y // self.pitch_y)
        return bx, by

    def get_placement_blockages(
        self,
        grid_w: int,
        grid_h: int,
        empty_blocks: set[tuple[int, int]] | None = None,
    ) -> list[tuple[int, int, int, int]]:
        """Return (x, y, width, height) rectangles covering all street channels.

        Generates horizontal and vertical blockage bars surrounding the
        interior machine cores, as well as full core blockages for empty blocks.
        """
        blockages: list[tuple[int, int, int, int]] = []
        total_w = grid_w * self.pitch_x
        total_h = grid_h * self.pitch_y

        # Bottom channel / outer margin
        blockages.append((0, 0, total_w, self.core_margin_y))
        # Top channel / outer margin
        top_y = (grid_h - 1) * self.pitch_y + self.core_margin_y + self.core_height
        blockages.append((0, top_y, total_w, total_h - top_y))
        # Left channel / outer margin
        blockages.append((0, 0, self.core_margin_x, total_h))
        # Right channel / outer margin
        right_x = (grid_w - 1) * self.pitch_x + self.core_margin_x + self.core_width
        blockages.append((right_x, 0, total_w - right_x, total_h))

        # Internal horizontal street channels
        for by in range(grid_h - 1):
            y_start = by * self.pitch_y + self.core_margin_y + self.core_height
            y_height = self.channel_width
            blockages.append((0, y_start, total_w, y_height))

        # Internal vertical street channels
        for bx in range(grid_w - 1):
            x_start = bx * self.pitch_x + self.core_margin_x + self.core_width
            x_width = self.channel_width
            blockages.append((x_start, 0, x_width, total_h))

        # Blockages for unused / empty interior blocks
        if empty_blocks:
            for bx, by in sorted(empty_blocks):
                mx, my, mw, mh = self.get_core_bounds(bx, by)
                blockages.append((mx, my, mw, mh))

        return blockages

    def emit_floorplan_tcl(
        self,
        grid_w: int,
        grid_h: int,
        empty_blocks: set[tuple[int, int]] | None = None,
    ) -> str:
        """Emit OpenROAD floorplan commands with chassis die area and blockages."""
        die_w = grid_w * self.pitch_x
        die_h = grid_h * self.pitch_y
        core_lo_x = self.core_margin_x
        core_lo_y = self.core_margin_y
        core_hi_x = die_w - self.core_margin_x
        core_hi_y = die_h - self.core_margin_y

        lines = [
            f"# Floorplan for {grid_w}x{grid_h} city block chassis ({die_w}x{die_h} tiles)",
            f"initialize_floorplan -die_area [list 0 0 {die_w} {die_h}] \\",
            f"    -core_area [list {core_lo_x} {core_lo_y} {core_hi_x} {core_hi_y}] \\",
            "    -site tile",
            "",
            "# Placement blockages covering street transit channels",
        ]
        for x, y, w, h in self.get_placement_blockages(grid_w, grid_h, empty_blocks=empty_blocks):
            x2 = x + w
            y2 = y + h
            lines.append(f"create_blockage -region [list {x} {y} {x2} {y2}]")

        return "\n".join(lines) + "\n"

    def emit_def_blockages(
        self,
        grid_w: int,
        grid_h: int,
        dbu: int = 1000,
        empty_blocks: set[tuple[int, int]] | None = None,
    ) -> str:
        """Emit DEF BLOCKAGES section covering all street transit channels."""
        blockages = self.get_placement_blockages(grid_w, grid_h, empty_blocks=empty_blocks)
        lines = [f"BLOCKAGES {len(blockages)} ;"]
        for x, y, w, h in blockages:
            x1 = x * dbu
            y1 = y * dbu
            x2 = (x + w) * dbu
            y2 = (y + h) * dbu
            lines.append(f"    - PLACEMENT RECT ( {x1} {y1} ) ( {x2} {y2} ) ;")
        lines.append("END BLOCKAGES")
        return "\n".join(lines) + "\n"

    def apply_floorplan_to_def(self, def_text: str, grid_w: int, grid_h: int) -> str:
        """Update DIEAREA and insert BLOCKAGES for chassis grid in DEF text."""
        m_units = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", def_text)
        dbu = int(m_units.group(1)) if m_units else 1000

        total_w = grid_w * self.pitch_x * dbu
        total_h = grid_h * self.pitch_y * dbu
        new_die = f"DIEAREA ( 0 0 ) ( {total_w} {total_h} ) ;"

        if re.search(r"DIEAREA.*?;", def_text):
            def_text = re.sub(r"DIEAREA.*?;", new_die, def_text, count=1)
        else:
            def_text = re.sub(r"(DESIGN\s+\S+\s*;)", r"\1\n" + new_die, def_text, count=1)

        blockages_text = self.emit_def_blockages(grid_w, grid_h, dbu=dbu)
        def_text = re.sub(
            r"BLOCKAGES\s+\d+\s*;.*?END\s+BLOCKAGES\s*",
            "",
            def_text,
            flags=re.DOTALL,
        )

        if re.search(r"(NETS\s+\d+\s*;)", def_text):
            def_text = re.sub(r"(NETS\s+\d+\s*;)", blockages_text + r"\1", def_text, count=1)
        elif re.search(r"(END\s+DESIGN)", def_text):
            def_text = re.sub(r"(END\s+DESIGN)", blockages_text + r"\1", def_text, count=1)
        else:
            def_text += "\n" + blockages_text

        return def_text

    def verify_cells_in_cores(
        self,
        def_data: dict[str, Any],
        grid_w: int = 1,
        grid_h: int = 1,
        netlist_data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Verify that all placed components sit strictly within chassis machine cores.

        Returns list of violating components that spill into street channels or margins.
        """
        scale = 1.0 / float(def_data.get("units", 1000))
        violations: list[dict[str, Any]] = []

        cell_dims: dict[str, tuple[float, float]] = {}
        if netlist_data:
            for cname, cinfo in netlist_data.get("cells", {}).items():
                w = float(cinfo.get("width", 1))
                h = float(cinfo.get("height", 1))
                cell_dims[cname] = (w, h)

        core_boxes = [self.get_core_bounds(bx, by) for bx in range(grid_w) for by in range(grid_h)]

        for comp in def_data.get("components", []):
            cx = comp["x"] * scale
            cy = comp["y"] * scale
            macro = comp.get("macro", "")
            cw, ch = cell_dims.get(macro, entity_size(macro))

            in_any_core = False
            for min_x, min_y, w, h in core_boxes:
                if (
                    cx >= min_x - 1e-4
                    and (cx + cw) <= (min_x + w) + 1e-4
                    and cy >= min_y - 1e-4
                    and (cy + ch) <= (min_y + h) + 1e-4
                ):
                    in_any_core = True
                    break

            if not in_any_core:
                violations.append(
                    {
                        "component": comp["name"],
                        "macro": macro,
                        "x": cx,
                        "y": cy,
                        "width": cw,
                        "height": ch,
                    }
                )

        return violations

    def emit_base_blueprint(self, grid_w: int, grid_h: int) -> dict[str, Any]:
        """Emit tiled base entities and pavement for an N x M grid.

        Deduplicates shared boundaries between adjacent city blocks.
        """
        tiles_map: dict[tuple[int, int], str] = {}
        entities_map: dict[tuple[float, float, str], dict[str, Any]] = {}

        for bx in range(grid_w):
            for by in range(grid_h):
                offset_x = bx * self.pitch_x
                offset_y = by * self.pitch_y

                for t in self.template_tiles:
                    pos = t.get("position", {})
                    tx = int(pos.get("x", 0) + offset_x)
                    ty = int(pos.get("y", 0) + offset_y)
                    tiles_map[(tx, ty)] = t.get("name", "refined-concrete")

                for e in self.template_entities:
                    pos = e.get("position", {})
                    ex = round(float(pos.get("x", 0)) + offset_x, 2)
                    ey = round(float(pos.get("y", 0)) + offset_y, 2)
                    ename = str(e.get("name", ""))
                    key = (ex, ey, ename)
                    if key not in entities_map:
                        new_e = dict(e)
                        new_e.pop("neighbours", None)
                        new_e.pop("connections", None)
                        new_e["position"] = {"x": ex, "y": ey}
                        entities_map[key] = new_e

        # Build clean sequential entities list
        sorted_ents = sorted(
            entities_map.values(), key=lambda ent: (ent["position"]["y"], ent["position"]["x"])
        )
        for idx, ent in enumerate(sorted_ents, start=1):
            ent["entity_number"] = idx

        # Wire chassis big electric poles together into a unified power and circuit bus grid
        big_poles = [e for e in sorted_ents if e.get("name") == "big-electric-pole"]
        seen_pairs: set[tuple[int, int]] = set()
        wires: list[list[int]] = []

        for i, bp1 in enumerate(big_poles):
            p1_num = int(bp1.get("entity_number", 0))
            p1_pos = bp1.get("position", {})
            x1, y1 = float(p1_pos.get("x", 0.0)), float(p1_pos.get("y", 0.0))
            for bp2 in big_poles[i + 1 :]:
                p2_num = int(bp2.get("entity_number", 0))
                p2_pos = bp2.get("position", {})
                x2, y2 = float(p2_pos.get("x", 0.0)), float(p2_pos.get("y", 0.0))
                dx = abs(x1 - x2)
                dy = abs(y1 - y2)
                dist = math.hypot(dx, dy)
                if dist <= 30.5 and (dx < 0.1 or dy < 0.1):
                    pair = (min(p1_num, p2_num), max(p1_num, p2_num))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        # 5: copper wire, 1: red wire, 2: green wire
                        wires.append([pair[0], 5, pair[1], 5])
                        wires.append([pair[0], 1, pair[1], 1])
                        wires.append([pair[0], 2, pair[1], 2])

        # Build clean tiles list
        sorted_tiles = [
            {"position": {"x": x, "y": y}, "name": name}
            for (x, y), name in sorted(tiles_map.items(), key=lambda item: (item[0][1], item[0][0]))
        ]

        bp_content: dict[str, Any] = {
            "item": "blueprint",
            "label": f"City Block Grid {grid_w}x{grid_h}",
            "snap-to-grid": {"x": self.pitch_x, "y": self.pitch_y},
            "absolute-snapping": True,
            "entities": sorted_ents,
            "tiles": sorted_tiles,
        }
        if wires:
            bp_content["wires"] = wires

        bp_doc: dict[str, Any] = {"blueprint": bp_content}
        return bp_doc

    def emit_base_blueprint_string(self, grid_w: int, grid_h: int) -> str:
        """Emit base blueprint exchange string for an N x M grid."""
        doc = self.emit_base_blueprint(grid_w, grid_h)
        bp = doc["blueprint"]
        return encode(
            entities=bp.get("entities", []),
            label=bp.get("label", "City Block Chassis"),
            tiles=bp.get("tiles", []),
            wires=bp.get("wires"),
            snap_to_grid=bp.get("snap-to-grid"),
            absolute_snapping=bp.get("absolute-snapping"),
        )

    def get_horizontal_channel(
        self,
        block_x: int,
        block_y: int,
        default_tier: str = "fast-transport-belt",
    ) -> StreetChannel:
        """Return the horizontal street channel corridor north of (block_x, block_y)."""
        base_y = float(block_y * self.pitch_y + self.core_margin_y + self.core_height)
        span_min = float(block_x * self.pitch_x + self.core_margin_x)
        span_max = float(span_min + self.core_width)
        return StreetChannel(
            channel_id=f"h_channel_{block_x}_{block_y}",
            orientation="H",
            base_coord=base_y,
            span_min=span_min,
            span_max=span_max,
            default_tier=default_tier,
        )

    def get_vertical_channel(
        self,
        block_x: int,
        block_y: int,
        default_tier: str = "fast-transport-belt",
    ) -> StreetChannel:
        """Return the vertical street channel corridor east of (block_x, block_y)."""
        base_x = float(block_x * self.pitch_x + self.core_margin_x + self.core_width)
        span_min = float(block_y * self.pitch_y + self.core_margin_y)
        span_max = float(span_min + self.core_height)
        return StreetChannel(
            channel_id=f"v_channel_{block_x}_{block_y}",
            orientation="V",
            base_coord=base_x,
            span_min=span_min,
            span_max=span_max,
            default_tier=default_tier,
        )

    def get_intersection(
        self,
        intersection_x: int,
        intersection_y: int,
        h_channel: StreetChannel | None = None,
        v_channel: StreetChannel | None = None,
    ) -> RouterIntersection:
        """Return the router intersection junction at grid index (intersection_x, intersection_y)."""
        cx = float(intersection_x * self.pitch_x)
        cy = float(intersection_y * self.pitch_y)
        h_ch = h_channel or self.get_horizontal_channel(intersection_x - 1, intersection_y - 1)
        v_ch = v_channel or self.get_vertical_channel(intersection_x - 1, intersection_y - 1)
        return RouterIntersection(
            center_x=cx,
            center_y=cy,
            h_channel=h_ch,
            v_channel=v_ch,
        )
