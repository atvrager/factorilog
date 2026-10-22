"""Render Factorio blueprint entities to high-resolution PNG using game sprites."""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

DEFAULT_SPRITES_DIR = Path("/scratch/entity")
CANVAS_PADDING_TILES = 3
TILE_SIZE_PX = 64
HALF_TILE_OFFSET = 0.5
CANVAS_BG_RGBA: tuple[int, int, int, int] = (22, 24, 28, 255)
GRID_LINE_RGBA: tuple[int, int, int, int] = (34, 38, 44, 255)
TUNNEL_LINE_RGBA: tuple[int, int, int, int] = (243, 156, 18, 120)
TUNNEL_LINE_WIDTH = 4
LABEL_BG_RGBA: tuple[int, int, int, int] = (0, 0, 0, 190)
LABEL_OUTLINE_RGBA: tuple[int, int, int, int] = (255, 255, 255, 220)
WHITE_RGBA: tuple[int, int, int, int] = (255, 255, 255, 255)
INPUT_PIN_FILL_RGBA: tuple[int, int, int, int] = (39, 174, 96, 220)
INPUT_PIN_TEXT_RGBA: tuple[int, int, int, int] = (46, 204, 113, 255)
OUTPUT_PIN_RGBA: tuple[int, int, int, int] = (231, 76, 60, 220)
OUTPUT_PIN_TEXT_RGBA: tuple[int, int, int, int] = (231, 76, 60, 255)

DIR_NORTH = 0
DIR_EAST = 4
DIR_SOUTH = 8
DIR_WEST = 12
DIR_DIVISOR = 4
DEFAULT_BELT_ROW = 2
BELT_DIR_ROW: dict[int, int] = {
    DIR_NORTH: 2,
    DIR_EAST: 0,
    DIR_SOUTH: 3,
    DIR_WEST: 1,
}

SPLITTER_CROP_BOX: tuple[int, int, int, int] = (0, 0, 90, 84)
SPLITTER_OFFSET_X = 45
SPLITTER_OFFSET_Y = 42
BELT_SPRITE_SIZE = 128
UG_SPRITE_SIZE = 192
UG_RESIZE_PX = 96
UG_OFFSET_PX = 48
ASSEMBLER_ANIM_CROP_BOX: tuple[int, int, int, int] = (0, 0, 196, 192)
LABEL_HALF_WIDTH = 42
LABEL_HALF_HEIGHT = 10
PIN_HALF_SIZE = 14
PIN_LABEL_OFFSET_Y = 20
PIN_OUTLINE_WIDTH = 2
MAX_TUNNEL_SPAN = 10
COORD_MATCH_TOLERANCE = 0.1


def _decode_blueprint_entities(blueprint_file: Path) -> list[dict[str, Any]]:
    """Decode and return the entity list from a blueprint exchange file."""
    raw = blueprint_file.read_text().strip()
    data = json.loads(zlib.decompress(base64.b64decode(raw[1:])).decode("utf-8"))
    bp = data.get("blueprint", {})
    ents = bp.get("entities", [])
    if not ents:
        raise ValueError("No entities found in blueprint.")
    return list(ents)


def _draw_grid(
    draw: ImageDraw.ImageDraw,
    min_x: int,
    max_x: int,
    min_y: int,
    max_y: int,
    w_px: int,
    h_px: int,
) -> None:
    """Draw background tile grid lines on the canvas."""
    for x in range(min_x, max_x + 2):
        gx = (x - min_x) * TILE_SIZE_PX
        draw.line([(gx, 0), (gx, h_px)], fill=GRID_LINE_RGBA, width=1)
    for y in range(min_y, max_y + 2):
        gy = (y - min_y) * TILE_SIZE_PX
        draw.line([(0, gy), (w_px, gy)], fill=GRID_LINE_RGBA, width=1)


def _entity_xy(ent: Mapping[str, object]) -> tuple[float, float]:
    """Extract (x, y) float coordinates from an entity dictionary."""
    pos = ent.get("position", {})
    if isinstance(pos, Mapping):
        x = pos.get("x", 0.0)
        y = pos.get("y", 0.0)
        return (
            float(x) if isinstance(x, (int, float, str)) else 0.0,
            float(y) if isinstance(y, (int, float, str)) else 0.0,
        )
    return 0.0, 0.0


def _entity_dir(ent: Mapping[str, object]) -> int:
    """Extract direction integer from an entity dictionary."""
    d = ent.get("direction", DIR_NORTH)
    return int(d) if isinstance(d, (int, float, str)) else DIR_NORTH


def _render_belts_and_splitters(
    canvas: Image.Image,
    ents: Sequence[Mapping[str, object]],
    sprites_dir: Path,
    min_x: int,
    min_y: int,
) -> None:
    """Composite transport belt and splitter sprites onto the canvas."""
    belt_src = Image.open(sprites_dir / "transport-belt" / "transport-belt.png")
    sp_east = Image.open(sprites_dir / "splitter" / "splitter-east.png")
    sp_east_f0 = sp_east.crop(SPLITTER_CROP_BOX)

    for e in ents:
        if e["name"] == "transport-belt":
            ex, ey = _entity_xy(e)
            px = int((ex - min_x - HALF_TILE_OFFSET) * TILE_SIZE_PX)
            py = int((ey - min_y - HALF_TILE_OFFSET) * TILE_SIZE_PX)
            row = BELT_DIR_ROW.get(_entity_dir(e), DEFAULT_BELT_ROW)
            crop_box = (
                0,
                row * BELT_SPRITE_SIZE,
                BELT_SPRITE_SIZE,
                (row + 1) * BELT_SPRITE_SIZE,
            )
            frame = belt_src.crop(crop_box).resize(
                (TILE_SIZE_PX, TILE_SIZE_PX), Image.Resampling.LANCZOS
            )
            canvas.alpha_composite(frame, (px, py))

    for e in ents:
        if e["name"] == "splitter":
            ex, ey = _entity_xy(e)
            cx = int((ex - min_x) * TILE_SIZE_PX)
            cy = int((ey - min_y) * TILE_SIZE_PX)
            canvas.alpha_composite(sp_east_f0, (cx - SPLITTER_OFFSET_X, cy - SPLITTER_OFFSET_Y))


def _is_matching_tunnel_pair(
    d: int,
    ix: float,
    iy: float,
    ox: float,
    oy: float,
) -> bool:
    """Return True when underground input (ix, iy) pairs with output (ox, oy) in direction d."""
    if d == DIR_EAST:
        return abs(iy - oy) < COORD_MATCH_TOLERANCE and 0 < ox - ix <= MAX_TUNNEL_SPAN
    if d == DIR_WEST:
        return abs(iy - oy) < COORD_MATCH_TOLERANCE and 0 < ix - ox <= MAX_TUNNEL_SPAN
    if d == DIR_SOUTH:
        return abs(ix - ox) < COORD_MATCH_TOLERANCE and 0 < oy - iy <= MAX_TUNNEL_SPAN
    if d == DIR_NORTH:
        return abs(ix - ox) < COORD_MATCH_TOLERANCE and 0 < iy - oy <= MAX_TUNNEL_SPAN
    return False


def _draw_tunnel_lines(
    draw: ImageDraw.ImageDraw,
    ents: Sequence[Mapping[str, object]],
    min_x: int,
    min_y: int,
) -> None:
    """Draw connecting lines between paired underground belt inputs and outputs."""
    inputs_ug = [e for e in ents if e["name"] == "underground-belt" and e.get("type") == "input"]
    outputs_ug = [e for e in ents if e["name"] == "underground-belt" and e.get("type") == "output"]
    for ue_in in inputs_ug:
        ix, iy = _entity_xy(ue_in)
        d = _entity_dir(ue_in)
        for ue_out in outputs_ug:
            ox, oy = _entity_xy(ue_out)
            if _entity_dir(ue_out) == d and _is_matching_tunnel_pair(d, ix, iy, ox, oy):
                p1 = (int((ix - min_x) * TILE_SIZE_PX), int((iy - min_y) * TILE_SIZE_PX))
                p2 = (int((ox - min_x) * TILE_SIZE_PX), int((oy - min_y) * TILE_SIZE_PX))
                draw.line([p1, p2], fill=TUNNEL_LINE_RGBA, width=TUNNEL_LINE_WIDTH)
                break


def _render_undergrounds(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    ents: Sequence[Mapping[str, object]],
    sprites_dir: Path,
    min_x: int,
    min_y: int,
) -> None:
    """Draw underground belt connection lines and composite hood sprites."""
    _draw_tunnel_lines(draw, ents, min_x, min_y)
    ug_src = Image.open(sprites_dir / "underground-belt" / "underground-belt-structure.png")
    for e in ents:
        if e["name"] == "underground-belt":
            ex, ey = _entity_xy(e)
            cx = int((ex - min_x) * TILE_SIZE_PX)
            cy = int((ey - min_y) * TILE_SIZE_PX)
            col = _entity_dir(e) // DIR_DIVISOR
            row = 1 if e.get("type", "input") == "input" else 0
            crop_box = (
                col * UG_SPRITE_SIZE,
                row * UG_SPRITE_SIZE,
                (col + 1) * UG_SPRITE_SIZE,
                (row + 1) * UG_SPRITE_SIZE,
            )
            hood = ug_src.crop(crop_box).resize(
                (UG_RESIZE_PX, UG_RESIZE_PX), Image.Resampling.LANCZOS
            )
            canvas.alpha_composite(hood, (cx - UG_OFFSET_PX, cy - UG_OFFSET_PX))


def _render_machines_and_labels(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    ents: Sequence[Mapping[str, object]],
    sprites_dir: Path,
    min_x: int,
    min_y: int,
) -> None:
    """Composite crafting machine sprites and draw recipe labels."""
    furnace_src = Image.open(sprites_dir / "electric-furnace" / "electric-furnace.png")
    assembler_base = Image.open(
        sprites_dir / "assembling-machine-3" / "assembling-machine-3-base.png"
    )
    assembler_anim = Image.open(
        sprites_dir / "assembling-machine-3" / "assembling-machine-3-anim.png"
    )
    anim_f0 = assembler_anim.crop(ASSEMBLER_ANIM_CROP_BOX)

    for e in ents:
        ex, ey = _entity_xy(e)
        cx = int((ex - min_x) * TILE_SIZE_PX)
        cy = int((ey - min_y) * TILE_SIZE_PX)
        if e["name"] == "electric-furnace":
            fw, fh = furnace_src.size
            canvas.alpha_composite(furnace_src, (cx - fw // 2, cy - fh // 2))
        elif e["name"] == "assembling-machine-3":
            aw, ah = assembler_base.size
            canvas.alpha_composite(assembler_base, (cx - aw // 2, cy - ah // 2))
            canvas.alpha_composite(anim_f0, (cx - aw // 2, cy - ah // 2))

    for e in ents:
        if e["name"] in ("electric-furnace", "assembling-machine-3"):
            ex, ey = _entity_xy(e)
            cx = int((ex - min_x) * TILE_SIZE_PX)
            cy = int((ey - min_y) * TILE_SIZE_PX)
            rec = (
                str(e.get("recipe", e["name"]))
                .replace("automation-science-pack", "RED SCIENCE")
                .replace("iron-gear-wheel", "GEAR")
                .replace("copper-plate", "COPPER")
                .replace("iron-plate", "IRON")
            )
            draw.rectangle(
                [
                    cx - LABEL_HALF_WIDTH,
                    cy - LABEL_HALF_HEIGHT,
                    cx + LABEL_HALF_WIDTH,
                    cy + LABEL_HALF_HEIGHT,
                ],
                fill=LABEL_BG_RGBA,
                outline=LABEL_OUTLINE_RGBA,
                width=1,
            )
            draw.text((cx, cy), rec, fill=WHITE_RGBA, anchor="mm")


def _render_pins(
    draw: ImageDraw.ImageDraw,
    pins_file: Path | None,
    min_x: int,
    min_y: int,
) -> None:
    """Draw input and output boundary pin markers if pins_file exists."""
    if not (pins_file and pins_file.is_file()):
        return
    pins_data = json.loads(pins_file.read_text())
    for pin in pins_data.get("inputs", []):
        px = int((float(pin["x"]) - min_x) * TILE_SIZE_PX)
        py = int((float(pin["y"]) - min_y) * TILE_SIZE_PX)
        draw.rectangle(
            [px - PIN_HALF_SIZE, py - PIN_HALF_SIZE, px + PIN_HALF_SIZE, py + PIN_HALF_SIZE],
            fill=INPUT_PIN_FILL_RGBA,
            outline=WHITE_RGBA,
            width=PIN_OUTLINE_WIDTH,
        )
        draw.text(
            (px, py - PIN_LABEL_OFFSET_Y),
            str(pin["name"]),
            fill=INPUT_PIN_TEXT_RGBA,
            anchor="mb",
        )
    for pin in pins_data.get("outputs", []):
        px = int((float(pin["x"]) - min_x) * TILE_SIZE_PX)
        py = int((float(pin["y"]) - min_y) * TILE_SIZE_PX)
        draw.rectangle(
            [px - PIN_HALF_SIZE, py - PIN_HALF_SIZE, px + PIN_HALF_SIZE, py + PIN_HALF_SIZE],
            fill=OUTPUT_PIN_RGBA,
            outline=WHITE_RGBA,
            width=PIN_OUTLINE_WIDTH,
        )
        draw.text(
            (px, py + PIN_LABEL_OFFSET_Y),
            str(pin["name"]),
            fill=OUTPUT_PIN_TEXT_RGBA,
            anchor="mt",
        )


def render_blueprint(
    blueprint_file: Path,
    output_png: Path,
    pins_file: Path | None = None,
    sprites_dir: Path = DEFAULT_SPRITES_DIR,
) -> None:
    """Render blueprint entities using real game sprites from sprites_dir."""
    ents = _decode_blueprint_entities(blueprint_file)
    xs = [float(e["position"]["x"]) for e in ents]
    ys = [float(e["position"]["y"]) for e in ents]
    min_x = math.floor(min(xs)) - CANVAS_PADDING_TILES
    max_x = math.ceil(max(xs)) + CANVAS_PADDING_TILES
    min_y = math.floor(min(ys)) - CANVAS_PADDING_TILES
    max_y = math.ceil(max(ys)) + CANVAS_PADDING_TILES

    w_px = (max_x - min_x + 1) * TILE_SIZE_PX
    h_px = (max_y - min_y + 1) * TILE_SIZE_PX

    canvas = Image.new("RGBA", (w_px, h_px), CANVAS_BG_RGBA)
    draw = ImageDraw.Draw(canvas)

    _draw_grid(draw, min_x, max_x, min_y, max_y, w_px, h_px)
    _render_belts_and_splitters(canvas, ents, sprites_dir, min_x, min_y)
    _render_undergrounds(canvas, draw, ents, sprites_dir, min_x, min_y)
    _render_machines_and_labels(canvas, draw, ents, sprites_dir, min_x, min_y)
    _render_pins(draw, pins_file, min_x, min_y)

    output_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_png)


def main() -> int:
    """CLI entrypoint for rendering factory blueprint."""
    parser = argparse.ArgumentParser(description="Render Factorio blueprint entities to PNG.")
    parser.add_argument("blueprint", type=Path, help="Path to blueprint file")
    parser.add_argument("output", type=Path, help="Output PNG path")
    parser.add_argument("--pins", type=Path, default=None, help="Optional pins JSON path")
    parser.add_argument(
        "--sprites-dir",
        type=Path,
        default=DEFAULT_SPRITES_DIR,
        help="Path to entity graphics directory",
    )
    args = parser.parse_args()

    render_blueprint(args.blueprint, args.output, args.pins, args.sprites_dir)
    print(f"Rendered blueprint to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
