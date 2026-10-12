"""Chassis abstraction and factory layout templates.

Defines the parameterizable ChassisTemplate interface and the Type 1
orthogonal CityBlockChassis reference implementation.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from foundry.blueprint import decode, encode


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
    ) -> list[tuple[int, int, int, int]]:
        """Return (x, y, width, height) rectangles covering all street channels.

        Generates horizontal and vertical blockage bars surrounding the
        interior machine cores.
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

        return blockages

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
                        new_e["position"] = {"x": ex, "y": ey}
                        entities_map[key] = new_e

        # Build clean sequential entities list
        sorted_ents = sorted(
            entities_map.values(), key=lambda ent: (ent["position"]["y"], ent["position"]["x"])
        )
        for idx, ent in enumerate(sorted_ents, start=1):
            ent["entity_number"] = idx

        # Build clean tiles list
        sorted_tiles = [
            {"position": {"x": x, "y": y}, "name": name}
            for (x, y), name in sorted(tiles_map.items(), key=lambda item: (item[0][1], item[0][0]))
        ]

        bp_doc: dict[str, Any] = {
            "blueprint": {
                "item": "blueprint",
                "label": f"City Block Grid {grid_w}x{grid_h}",
                "snap-to-grid": {"x": self.pitch_x, "y": self.pitch_y},
                "absolute-snapping": True,
                "entities": sorted_ents,
                "tiles": sorted_tiles,
            }
        }
        return bp_doc

    def emit_base_blueprint_string(self, grid_w: int, grid_h: int) -> str:
        """Emit base blueprint exchange string for an N x M grid."""
        doc = self.emit_base_blueprint(grid_w, grid_h)
        bp = doc["blueprint"]
        return encode(
            entities=bp.get("entities", []),
            label=bp.get("label", "City Block Chassis"),
            tiles=bp.get("tiles", []),
            snap_to_grid=bp.get("snap-to-grid"),
            absolute_snapping=bp.get("absolute-snapping"),
        )
