"""Render Factorio blueprint entities to high-resolution PNG using game sprites."""

from __future__ import annotations

import argparse
import base64
import json
import math
import zlib
from pathlib import Path

from PIL import Image, ImageDraw


def render_blueprint(
    blueprint_file: Path,
    output_png: Path,
    pins_file: Path | None = None,
    sprites_dir: Path = Path("/scratch/entity"),
) -> None:
    """Render blueprint entities using real game sprites from sprites_dir."""
    raw = blueprint_file.read_text().strip()
    data = json.loads(zlib.decompress(base64.b64decode(raw[1:])).decode("utf-8"))
    bp = data.get("blueprint", {})
    ents = bp.get("entities", [])
    if not ents:
        raise ValueError("No entities found in blueprint.")

    xs = [e["position"]["x"] for e in ents]
    ys = [e["position"]["y"] for e in ents]
    pad = 3
    min_x = math.floor(min(xs)) - pad
    max_x = math.ceil(max(xs)) + pad
    min_y = math.floor(min(ys)) - pad
    max_y = math.ceil(max(ys)) + pad

    tile_size = 64
    w_px = (max_x - min_x + 1) * tile_size
    h_px = (max_y - min_y + 1) * tile_size

    canvas = Image.new("RGBA", (w_px, h_px), (22, 24, 28, 255))
    draw = ImageDraw.Draw(canvas)

    # Grid lines
    for x in range(min_x, max_x + 2):
        gx = (x - min_x) * tile_size
        draw.line([(gx, 0), (gx, h_px)], fill=(34, 38, 44, 255), width=1)
    for y in range(min_y, max_y + 2):
        gy = (y - min_y) * tile_size
        draw.line([(0, gy), (w_px, gy)], fill=(34, 38, 44, 255), width=1)

    # Load sprites
    belt_src = Image.open(sprites_dir / "transport-belt" / "transport-belt.png")
    ug_src = Image.open(sprites_dir / "underground-belt" / "underground-belt-structure.png")
    furnace_src = Image.open(sprites_dir / "electric-furnace" / "electric-furnace.png")
    assembler_base = Image.open(
        sprites_dir / "assembling-machine-3" / "assembling-machine-3-base.png"
    )
    assembler_anim = Image.open(
        sprites_dir / "assembling-machine-3" / "assembling-machine-3-anim.png"
    )
    sp_east = Image.open(sprites_dir / "splitter" / "splitter-east.png")
    sp_east_f0 = sp_east.crop((0, 0, 90, 84))

    dir_row = {0: 2, 4: 0, 8: 3, 12: 1}

    # Layer 1: Transport belts
    for e in ents:
        if e["name"] == "transport-belt":
            ex, ey = e["position"]["x"], e["position"]["y"]
            px = int((ex - min_x - 0.5) * tile_size)
            py = int((ey - min_y - 0.5) * tile_size)
            d = e.get("direction", 0)
            row = dir_row.get(d, 2)
            frame = belt_src.crop((0, row * 128, 128, (row + 1) * 128)).resize(
                (tile_size, tile_size), Image.Resampling.LANCZOS
            )
            canvas.alpha_composite(frame, (px, py))

    # Layer 2: Splitters
    for e in ents:
        if e["name"] == "splitter":
            ex, ey = e["position"]["x"], e["position"]["y"]
            cx = int((ex - min_x) * tile_size)
            cy = int((ey - min_y) * tile_size)
            canvas.alpha_composite(sp_east_f0, (cx - 45, cy - 42))

    # Layer 3: Underground tunnels (connecting lines)
    inputs_ug = [e for e in ents if e["name"] == "underground-belt" and e.get("type") == "input"]
    outputs_ug = [e for e in ents if e["name"] == "underground-belt" and e.get("type") == "output"]
    for ue_in in inputs_ug:
        ix, iy = ue_in["position"]["x"], ue_in["position"]["y"]
        d = ue_in.get("direction", 0)
        # Find matching output
        for ue_out in outputs_ug:
            ox, oy = ue_out["position"]["x"], ue_out["position"]["y"]
            if ue_out.get("direction", 0) == d:
                if (
                    (d == 4 and abs(iy - oy) < 0.1 and ox > ix and ox - ix <= 10)
                    or (d == 12 and abs(iy - oy) < 0.1 and ox < ix and ix - ox <= 10)
                    or (d == 8 and abs(ix - ox) < 0.1 and oy > iy and oy - iy <= 10)
                    or (d == 0 and abs(ix - ox) < 0.1 and oy < iy and iy - oy <= 10)
                ):
                    p1 = (int((ix - min_x) * tile_size), int((iy - min_y) * tile_size))
                    p2 = (int((ox - min_x) * tile_size), int((oy - min_y) * tile_size))
                    draw.line([p1, p2], fill=(243, 156, 18, 120), width=4)
                    break

    # Layer 4: Underground hoods
    for e in ents:
        if e["name"] == "underground-belt":
            ex, ey = e["position"]["x"], e["position"]["y"]
            cx = int((ex - min_x) * tile_size)
            cy = int((ey - min_y) * tile_size)
            d = e.get("direction", 0)
            col = d // 4
            utype = e.get("type", "input")
            row = 1 if utype == "input" else 0
            hood = ug_src.crop((col * 192, row * 192, (col + 1) * 192, (row + 1) * 192)).resize(
                (96, 96), Image.Resampling.LANCZOS
            )
            canvas.alpha_composite(hood, (cx - 48, cy - 48))

    # Layer 5: Machines
    for e in ents:
        ex, ey = e["position"]["x"], e["position"]["y"]
        cx = int((ex - min_x) * tile_size)
        cy = int((ey - min_y) * tile_size)
        if e["name"] == "electric-furnace":
            fw, fh = furnace_src.size
            canvas.alpha_composite(furnace_src, (cx - fw // 2, cy - fh // 2))
        elif e["name"] == "assembling-machine-3":
            aw, ah = assembler_base.size
            canvas.alpha_composite(assembler_base, (cx - aw // 2, cy - ah // 2))
            anim_f0 = assembler_anim.crop((0, 0, 196, 192))
            canvas.alpha_composite(anim_f0, (cx - aw // 2, cy - ah // 2))

    # Layer 6: Machine Labels
    for e in ents:
        if e["name"] in ("electric-furnace", "assembling-machine-3"):
            ex, ey = e["position"]["x"], e["position"]["y"]
            cx = int((ex - min_x) * tile_size)
            cy = int((ey - min_y) * tile_size)
            rec = (
                e.get("recipe", e["name"])
                .replace("automation-science-pack", "RED SCIENCE")
                .replace("iron-gear-wheel", "GEAR")
                .replace("copper-plate", "COPPER")
                .replace("iron-plate", "IRON")
            )
            draw.rectangle(
                [cx - 42, cy - 10, cx + 42, cy + 10],
                fill=(0, 0, 0, 190),
                outline=(255, 255, 255, 220),
                width=1,
            )
            draw.text((cx, cy), rec, fill=(255, 255, 255, 255), anchor="mm")

    # Layer 7: Boundary pins
    if pins_file and pins_file.is_file():
        pins_data = json.loads(pins_file.read_text())
        for pin in pins_data.get("inputs", []):
            px = int((pin["x"] - min_x) * tile_size)
            py = int((pin["y"] - min_y) * tile_size)
            draw.rectangle(
                [px - 14, py - 14, px + 14, py + 14],
                fill=(39, 174, 96, 220),
                outline=(255, 255, 255, 255),
                width=2,
            )
            draw.text((px, py - 20), pin["name"], fill=(46, 204, 113, 255), anchor="mb")
        for pin in pins_data.get("outputs", []):
            px = int((pin["x"] - min_x) * tile_size)
            py = int((pin["y"] - min_y) * tile_size)
            draw.rectangle(
                [px - 14, py - 14, px + 14, py + 14],
                fill=(231, 76, 60, 220),
                outline=(255, 255, 255, 255),
                width=2,
            )
            draw.text((px, py + 20), pin["name"], fill=(231, 76, 60, 255), anchor="mt")

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
        default=Path("/scratch/entity"),
        help="Path to entity graphics directory",
    )
    args = parser.parse_args()

    render_blueprint(args.blueprint, args.output, args.pins, args.sprites_dir)
    print(f"Rendered blueprint to {args.output}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
