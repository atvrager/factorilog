"""Draw a generated layout as a PNG.

The game cannot take a screenshot in headless mode: `game.take_screenshot`
does nothing there. This file draws the layout from the entity list instead,
so every run leaves a picture.

```
render.py base automation-science-pack:1 logistic-science-pack:1 --out base.png
render.py block copper-cable 2000 --out cable.png
```

One tile is four pixels. Belts show a line in the direction of travel, and
underground belts show a dot, so a tunnel reads as a tunnel.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from PIL import Image, ImageDraw

sys.path.extend(
    [
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "foundry"),
    ]
)
import base as base_planner
import pnr
import solve

DEFAULT_TILE = 6
DEFAULT_RATE = 2000.0
DEFAULT_COLOUR: tuple[int, int, int] = (255, 255, 255)
BACKGROUND_COLOUR: tuple[int, int, int] = (16, 16, 16)
BELT_LINE_COLOUR: tuple[int, int, int] = (20, 20, 20)
TUNNEL_DOT_COLOUR: tuple[int, int, int] = (255, 255, 0)
CANVAS_MARGIN = 3
TILE = DEFAULT_TILE

# Colour per entity family.
COLOURS: dict[str, tuple[int, int, int]] = {
    "assembling-machine": (60, 120, 60),
    "electric-furnace": (110, 90, 40),
    "chemical-plant": (60, 110, 120),
    "oil-refinery": (90, 60, 120),
    "transport-belt": (150, 150, 150),
    "underground-belt": (90, 90, 90),
    "loader": (200, 200, 120),
    "inserter": (220, 140, 40),
    "substation": (60, 90, 200),
    "solar-panel": (40, 60, 110),
    "steel-chest": (200, 200, 60),
    "infinity-chest": (200, 60, 200),
}
BELTS = ("turbo-transport-belt", "express-transport-belt", "fast-transport-belt", "transport-belt")
TUNNELS = (
    "turbo-underground-belt",
    "express-underground-belt",
    "fast-underground-belt",
    "underground-belt",
)
INSERTERS = ("bulk-inserter", "fast-inserter", "long-handed-inserter", "inserter")


def colour_of(name: str) -> tuple[int, int, int]:
    """Return the RGB colour tuple for an entity prototype name."""
    for family, colour in COLOURS.items():
        if name.startswith(family) or family in name:
            return colour
    if name in BELTS:
        return COLOURS["transport-belt"]
    if name in TUNNELS:
        return COLOURS["underground-belt"]
    if name in INSERTERS:
        return COLOURS["inserter"]
    return DEFAULT_COLOUR


def _draw_entity_overlay(
    pen: ImageDraw.ImageDraw,
    name: str,
    direction: object,
    x: int,
    y: int,
    tile: int,
) -> None:
    """Draw belt direction line and underground dot for one entity."""
    if name in BELTS or name in TUNNELS:
        mid = tile // 2
        if direction in (pnr.EAST, pnr.WEST):
            pen.line([x + 1, y + mid, x + tile - 2, y + mid], fill=BELT_LINE_COLOUR)
        elif direction in (pnr.NORTH, pnr.SOUTH):
            pen.line([x + mid, y + 1, x + mid, y + tile - 2], fill=BELT_LINE_COLOUR)
    if name in TUNNELS:
        pen.point([x + 1, y + 1], fill=TUNNEL_DOT_COLOUR)


def draw(
    entities: Sequence[dict[str, object]],
    path: str,
    tile: int,
) -> tuple[int, int]:
    """Draw one coloured tile per entity and a belt arrow in the tile."""
    global TILE
    TILE = tile
    xs: list[float] = []
    ys: list[float] = []
    for ent in entities:
        pos = ent["position"]
        if isinstance(pos, dict):
            xs.append(float(pos["x"]))
            ys.append(float(pos["y"]))
    left, top = min(xs) - 2, min(ys) - 2
    width = int(max(xs) - left) + CANVAS_MARGIN
    height = int(max(ys) - top) + CANVAS_MARGIN

    image = Image.new("RGB", (width * TILE, height * TILE), BACKGROUND_COLOUR)
    pen = ImageDraw.Draw(image)

    for entity in entities:
        pos = entity["position"]
        if not isinstance(pos, dict):
            continue
        x = int(float(pos["x"]) - left) * TILE
        y = int(float(pos["y"]) - top) * TILE
        name = str(entity["name"])
        pen.rectangle([x, y, x + TILE - 1, y + TILE - 1], fill=colour_of(name))
        _draw_entity_overlay(pen, name, entity.get("direction"), x, y, TILE)

    image.save(path)
    return image.size


def entities_of(args: argparse.Namespace) -> list[dict[str, object]]:
    """Build the entity list for the requested render mode."""
    data = solve.Data(args.dump)
    if args.mode == "block":
        block = pnr.build_block(data, args.target, args.rate, args.belt, args.inserter)
        return pnr.block_entities(block, args.belt, args.inserter)
    nodes, _rates, _raw, _surplus = base_planner.plan(data, args.demand, args.belt, args.inserter)
    lane_index, _lanes = base_planner.assign_lanes(nodes)
    entities, _block_x, _height, _vias = base_planner.build(
        data, nodes, lane_index, args.demand, args.belt, args.inserter
    )
    return entities


def main() -> None:
    """Parse CLI arguments and render the layout to a PNG file."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["base", "block"])
    parser.add_argument("target", nargs="?", default="copper-cable")
    parser.add_argument("demand", nargs="*")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE)
    parser.add_argument("--tile", type=int, default=DEFAULT_TILE)
    parser.add_argument("--out", default="layout.png")
    parser.add_argument("--dump", default=solve.DEFAULT_DUMP)
    parser.add_argument("--belt", default="turbo-transport-belt")
    parser.add_argument("--inserter", default="fast-inserter")
    args = parser.parse_args()

    demand = [text for text in [args.target] + args.demand if ":" in text]
    args.demand = {text.split(":")[0]: float(text.split(":")[1]) for text in demand}

    entities = entities_of(args)
    size = draw(entities, args.out, args.tile)
    print(f"image       {args.out} {size[0]}x{size[1]} px, {len(entities)} entities")


if __name__ == "__main__":
    main()
