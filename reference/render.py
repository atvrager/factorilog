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

import argparse

from PIL import Image, ImageDraw

import base as base_planner
import pnr
import solve

DEFAULT_TILE = 6

# Colour per entity family.
COLOURS = {
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
BELTS = ("turbo-transport-belt", "express-transport-belt", "fast-transport-belt",
         "transport-belt")
TUNNELS = ("turbo-underground-belt", "express-underground-belt",
           "fast-underground-belt", "underground-belt")
INSERTERS = ("bulk-inserter", "fast-inserter", "long-handed-inserter", "inserter")


def colour_of(name):
    for family, colour in COLOURS.items():
        if name.startswith(family) or family in name:
            return colour
    if name in BELTS:
        return COLOURS["transport-belt"]
    if name in TUNNELS:
        return COLOURS["underground-belt"]
    if name in INSERTERS:
        return COLOURS["inserter"]
    return (255, 255, 255)


def draw(entities, path, tile):
    """One coloured tile per entity, plus a belt arrow in the tile."""
    global TILE
    TILE = tile
    xs = [e["position"]["x"] for e in entities]
    ys = [e["position"]["y"] for e in entities]
    left, top = min(xs) - 2, min(ys) - 2
    width = int(max(xs) - left) + 3
    height = int(max(ys) - top) + 3

    image = Image.new("RGB", (width * TILE, height * TILE), (16, 16, 16))
    pen = ImageDraw.Draw(image)

    for entity in entities:
        x = int(entity["position"]["x"] - left) * TILE
        y = int(entity["position"]["y"] - top) * TILE
        pen.rectangle([x, y, x + TILE - 1, y + TILE - 1], fill=colour_of(entity["name"]))
        direction = entity.get("direction")
        if entity["name"] in BELTS or entity["name"] in TUNNELS:
            mid = TILE // 2
            if direction in (pnr.EAST, pnr.WEST):
                pen.line([x + 1, y + mid, x + TILE - 2, y + mid], fill=(20, 20, 20))
            elif direction in (pnr.NORTH, pnr.SOUTH):
                pen.line([x + mid, y + 1, x + mid, y + TILE - 2], fill=(20, 20, 20))
        if entity["name"] in TUNNELS:
            pen.point([x + 1, y + 1], fill=(255, 255, 0))

    image.save(path)
    return image.size


def entities_of(args):
    data = solve.Data(args.dump)
    if args.mode == "block":
        block = pnr.build_block(data, args.target, args.rate, args.belt, args.inserter)
        return pnr.block_entities(block, args.belt, args.inserter)
    nodes, rates, raw, surplus = base_planner.plan(data, args.demand, args.belt, args.inserter)
    lane_index, lanes = base_planner.assign_lanes(nodes)
    entities, block_x, height, vias = base_planner.build(
        data, nodes, lane_index, args.demand, args.belt, args.inserter)
    return entities


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["base", "block"])
    parser.add_argument("target", nargs="?", default="copper-cable")
    parser.add_argument("demand", nargs="*")
    parser.add_argument("--rate", type=float, default=2000.0)
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
