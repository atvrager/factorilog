"""Factory place and route, first cut: blocks and their test rig.

A block holds one recipe's machines, one input belt, one output belt, and an
inserter pair per machine side. The plan fixes the machine count. The belt and
inserter capacity fix how many machines fit in one block, so a recipe with
more demand splits into several branches.

The output is a blueprint exchange string: zlib plus base64 of the blueprint
JSON, with a version byte in front.

usage:
  pnr.py block <recipe> <crafts_per_min> [--ascii] [--out FILE]
  pnr.py measure <recipe> <crafts_per_min> --mod DIR [--ticks N]

`block` prints the checks and writes the blueprint string. `measure` writes a
probe mod whose `on_init` builds the blueprint and its rig, and whose tick
handler reads the produced items back, so the plan rate can be compared with
the game's rate.
"""

import argparse
import base64
import json
import math
import os
import pathlib
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solve

# Blueprint direction values, doubled in 2.0: north, east, south, west.
NORTH, EAST, SOUTH, WEST = 0, 4, 8, 12

# Rows of one block, relative to the block origin. A machine holds three tiles
# per side, so up to three inserters fit on a side.
INPUT_BELT_Y = 4
INPUT_INSERTER_Y = 3
MACHINE_Y = 1.5
OUTPUT_INSERTER_Y = -1
OUTPUT_BELT_Y = -2
MACHINE_SPACING = 3
EDGE_OFFSETS = [-1.0, 0.0, 1.0]

# Rows between two blocks, so the rig of one block clears the next.
BLOCK_ROW_HEIGHT = 20

# Inserter throughput, items per second, for the checks. Measured on 2.1.20
# with no inserter-capacity research: a bulk inserter moves one item per swing
# until the research lands, so it matches a fast inserter. A stack inserter
# always uses its stack.
INSERTER_ITEMS_PER_SECOND = {
    "inserter": 0.83,
    "fast-inserter": 2.57,
    "bulk-inserter": 2.57,
    "stack-inserter": 12.83,
}

# Test rig. Solar panels with a raised multiplier power the block, so the
# measurement does not need a power plant.
SUBSTATION_Y = 6.0
PANEL_Y = 11.5
RIG_PANEL_COLUMNS = 6
RIG_PANEL_ROWS = 2
RIG_SOLAR_MULTIPLIER = 50
POWER_SPACING = 17


class Block:
    """One branch: a row of machines with its belts, inserters, and ports."""

    def __init__(self, recipe, total_rate, branches, machines, machine_name, speed):
        self.recipe = recipe
        self.total_rate = total_rate  # crafts per minute, all branches
        self.branches = branches
        self.machines = machines  # machines per branch
        self.machine_name = machine_name
        self.speed = speed
        self.ingredients = []
        self.results = []
        self.belt = ""
        self.inserter = ""
        self.input_inserters = 1
        self.output_inserters = 1

    def origins(self):
        return [(0, BLOCK_ROW_HEIGHT * b) for b in range(self.branches)]

    def branch_rate(self):
        return self.total_rate / self.branches

    def width(self):
        return MACHINE_SPACING * self.machines


def belt_capacity(data, belt):
    """Items per second on one belt, from the prototype speed."""
    speed = float(data.prototypes["transport-belt"][belt]["speed"])
    return speed * 60.0 * 8.0


def block_entities(block, belt, inserter):
    """Every entity of the blocks, as blueprint JSON."""
    entities = []

    def add(name, x, y, direction=None, extra=None):
        entity = {
            "entity_number": len(entities) + 1,
            "name": name,
            "position": {"x": round(x, 2), "y": round(y, 2)},
        }
        if direction is not None:
            entity["direction"] = direction
        if extra:
            entity.update(extra)
        entities.append(entity)

    for origin_x, origin_y in block.origins():
        # Belts run one tile past the outer machines, so a loader reaches them.
        for x in range(-2, MACHINE_SPACING * block.machines):
            add(belt, origin_x + x + 0.5, origin_y + INPUT_BELT_Y + 0.5, EAST)
            add(belt, origin_x + x + 0.5, origin_y + OUTPUT_BELT_Y + 0.5, EAST)

        for index in range(block.machines):
            centre = origin_x + 1.5 + MACHINE_SPACING * index
            add(block.machine_name, centre, origin_y + MACHINE_Y, extra={"recipe": block.recipe})
            # An inserter picks from the side it faces. The input inserter is
            # north of the input belt, so it faces south at the belt. The
            # output inserter is north of the machine, so it faces south at
            # the machine.
            for offset in EDGE_OFFSETS[: block.input_inserters]:
                add(inserter, centre + offset, origin_y + INPUT_INSERTER_Y + 0.5, SOUTH)
            for offset in EDGE_OFFSETS[: block.output_inserters]:
                add(inserter, centre + offset, origin_y + OUTPUT_INSERTER_Y + 0.5, SOUTH)

    return entities


def rig_entities(block, input_items, output_rate):
    """The test rig: feed, drain, and power. Not part of a factory.

    The drain needs one inserter per 4.62 items/s, because a single inserter
    caps the whole block. The measurement rig therefore drains along the belt,
    not at its end.
    """
    entities = []
    counter = {"n": 0}
    drain = max(
        1, math.ceil(output_rate / block.branches / INSERTER_ITEMS_PER_SECOND["bulk-inserter"])
    )

    def add(name, x, y, direction=None, extra=None):
        counter["n"] += 1
        entity = {
            "entity_number": counter["n"],
            "name": name,
            "position": {"x": round(x, 2), "y": round(y, 2)},
        }
        if direction is not None:
            entity["direction"] = direction
        if extra:
            entity.update(extra)
        entities.append(entity)

    filters = [
        {"name": item, "count": 1000, "mode": "at-least", "index": i + 1}
        for i, item in enumerate(input_items)
    ]
    for origin_x, origin_y in block.origins():
        right = origin_x + MACHINE_SPACING * block.machines
        # Feed: a chest, a loader, then the input belt.
        add(
            "infinity-chest",
            origin_x - 4.5,
            origin_y + INPUT_BELT_Y + 0.5,
            extra={"infinity_settings": {"remove_unfiltered_items": True, "filters": filters}},
        )
        add("turbo-loader", origin_x - 3.0, origin_y + INPUT_BELT_Y + 0.5, EAST)
        # Drain: inserters along the output belt, each facing it, each with a
        # chest behind it.
        for index in range(drain):
            x = right - 1 - index
            add("bulk-inserter", x + 0.5, origin_y + OUTPUT_BELT_Y - 0.5, SOUTH)
            add("steel-chest", x + 0.5, origin_y + OUTPUT_BELT_Y - 1.5)
        # Power: substations above the block, panels above them. A substation
        # covers 18 tiles around itself, so one row reaches the whole block.
        for column in range(int(math.ceil((right + 7) / POWER_SPACING))):
            add("substation", origin_x + 4.5 + POWER_SPACING * column, origin_y + SUBSTATION_Y)
        for row in range(RIG_PANEL_ROWS):
            for column in range(RIG_PANEL_COLUMNS):
                add("solar-panel", origin_x + 1.5 + 3 * column, origin_y + PANEL_Y + 3 * row)

    return entities


def blueprint_string(entities):
    document = {
        "blueprint": {
            "item": "blueprint",
            "label": "pnr block",
            "version": 562949953421312,
            "entities": entities,
        }
    }
    raw = json.dumps(document, separators=(",", ":")).encode()
    return "0" + base64.b64encode(zlib.compress(raw, 9)).decode()


def footprints_of(data, names):
    """Tile footprint of each entity, from its collision box.

    A collision box is the physical size; the tile footprint is that size
    rounded up. An entity whose long axis is horizontal when it faces east
    needs the axes swapped, which is how the loader works.
    """
    result = {}
    for kind in (
        "transport-belt",
        "loader",
        "inserter",
        "assembling-machine",
        "furnace",
        "container",
        "logistic-container",
        "infinity-container",
        "electric-pole",
        "solar-panel",
        "accumulator",
    ):
        for name, prototype in data.prototypes.get(kind, {}).items():
            box = prototype.get("collision_box")
            if name in names and box is not None:
                result[name] = (math.ceil(box[1][0] - box[0][0]), math.ceil(box[1][1] - box[0][1]))
    return result


def footprint_of(footprints, entity):
    """Footprint of one placed entity, with the axes turned with the entity."""
    box = footprints.get(entity["name"])
    if box is None:
        return None
    width, height = box
    if width != height and entity.get("direction", NORTH) in (EAST, WEST):
        width, height = height, width
    return width, height


def tiles_of(entities, footprints):
    """Every tile an entity covers, plus the tiles that two entities share."""
    tiles = set()
    clashes = []
    for entity in entities:
        box = footprint_of(footprints, entity)
        if box is None:
            continue
        width, height = box
        corner = (entity["position"]["x"] - width / 2, entity["position"]["y"] - height / 2)
        for dx in range(width):
            for dy in range(height):
                tile = (int(round(corner[0])) + dx, int(round(corner[1])) + dy)
                if tile in tiles:
                    clashes.append((entity["name"], tile))
                tiles.add(tile)
    return tiles, clashes


def item_rate(block, amounts_per_craft):
    """Items per second of one side of the block, over every branch."""
    return block.total_rate / 60.0 * sum(amount for _, amount in amounts_per_craft)


def build_block(data, recipe, crafts_per_min, belt, inserter):
    """Plan the branches of one recipe at one rate."""
    recipe_data = data.recipes[recipe]
    machine = data.machine_for(recipe_data)
    if machine is None:
        raise SystemExit(f"no machine holds the categories of {recipe}")
    machine_name, speed = machine

    crafts_per_machine = 60.0 * speed / data.craft_seconds(recipe_data)
    machines = max(1, math.ceil(crafts_per_min / crafts_per_machine))

    ingredients = [(solve.name_of(e), solve.amount_of(e)) for e in data.ingredients_of(recipe_data)]
    results = [(solve.name_of(e), solve.amount_of(e)) for e in data.results_of(recipe_data)]
    capacity = belt_capacity(data, belt)

    draft = Block(recipe, crafts_per_min, 1, machines, machine_name, speed)
    draft.ingredients = ingredients
    draft.results = results
    in_rate = item_rate(draft, ingredients)
    out_rate = item_rate(draft, results)
    branches = max(math.ceil(in_rate / capacity), math.ceil(out_rate / capacity), 1)

    block = Block(
        recipe, crafts_per_min, branches, math.ceil(machines / branches), machine_name, speed
    )
    block.ingredients = ingredients
    block.results = results
    block.belt = belt
    block.inserter = inserter

    held = INSERTER_ITEMS_PER_SECOND.get(inserter, 4.62)
    per_machine = block.branch_rate() / 60.0 / block.machines
    block.input_inserters = min(
        len(EDGE_OFFSETS), max(1, math.ceil(per_machine * sum(a for _, a in ingredients) / held))
    )
    block.output_inserters = min(
        len(EDGE_OFFSETS), max(1, math.ceil(per_machine * sum(a for _, a in results) / held))
    )
    return block


def report_block(data, block, entities, footprints):
    """The static checks of the sketch, stage F."""
    capacity = belt_capacity(data, block.belt)
    in_rate = item_rate(block, block.ingredients)
    out_rate = item_rate(block, block.results)
    lines = [
        f"machine     {block.machine_name} at speed {block.speed}, "
        f"{block.machines * block.branches} machines in {block.branches} branch(es)",
        f"split       {block.branches} branch(es) of {block.machines} machines, "
        f"because the output belt holds {capacity:.0f} items/s",
        f"input load  {in_rate:8.2f} items/s against {capacity:.2f} per belt",
        f"output load {out_rate:8.2f} items/s against {capacity:.2f} per belt",
    ]
    per_machine = block.branch_rate() / 60.0 / block.machines
    for side, amounts, count in (
        ("input", block.ingredients, block.input_inserters),
        ("output", block.results, block.output_inserters),
    ):
        demand = per_machine * sum(amount for _, amount in amounts)
        held = INSERTER_ITEMS_PER_SECOND.get(block.inserter, 0) * count
        verdict = "ok" if demand <= held + 1e-9 else "short"
        lines.append(
            f"{side:6}      {demand:6.2f} items/s per machine with "
            f"{count} x {block.inserter} at {held:5.2f} -> {verdict}"
        )
    _, clashes = tiles_of(entities, footprints)
    lines.append(f"overlap     {len(clashes)} tiles claimed twice")
    lines.append(f"power       {check_power(data, entities)} entities outside every supply area")
    return lines


def check_power(data, entities):
    """Count the power users that no substation reaches.

    A machine or a loader with no supply area is a dead entity, which the
    in-game measurement would report as a zero rate.
    """
    powered_kinds = ("assembling-machine", "furnace", "inserter", "loader")
    names = set()
    for kind in powered_kinds:
        names |= set(data.prototypes.get(kind, {}))
    supply = float(data.prototypes["electric-pole"]["substation"]["supply_area_distance"])
    # The game keeps the boundary tile out of the supply area, so the check
    # asks for half a tile of margin. A boundary entity stays unpowered.
    reach = supply - 0.5

    supplies = [e["position"] for e in entities if e["name"] == "substation"]
    outside = 0
    for entity in entities:
        if entity["name"] not in names:
            continue
        position = entity["position"]
        if not any(
            abs(position["x"] - s["x"]) <= reach and abs(position["y"] - s["y"]) <= reach
            for s in supplies
        ):
            outside += 1
    return outside


def ascii_map(entities, block, footprints):
    """A picture of every branch, drawn from the footprints."""
    lines = []
    glyphs = {
        "assembling-machine-3": "A",
        "electric-furnace": "F",
        "chemical-plant": "C",
        "oil-refinery": "R",
        "rocket-silo": "S",
        "turbo-transport-belt": "=",
        "transport-belt": "=",
        "fast-transport-belt": "=",
        "express-transport-belt": "=",
        "bulk-inserter": ">",
        "fast-inserter": ">",
        "inserter": ">",
        "turbo-loader": "L",
        "infinity-chest": "I",
        "steel-chest": "X",
        "substation": "P",
        "solar-panel": "o",
    }
    for _, origin_y in block.origins():
        grid = {}
        for entity in entities:
            box = footprint_of(footprints, entity)
            if box is None:
                continue
            width, height = box
            y = entity["position"]["y"] - origin_y
            if not (-4 <= y <= 24):
                continue
            corner = (entity["position"]["x"] - width / 2, y - height / 2)
            for dx in range(width):
                for dy in range(height):
                    grid[(int(round(corner[0])) + dx, int(round(corner[1])) + dy)] = glyphs.get(
                        entity["name"], "?"
                    )
        if not grid:
            continue
        xs = [x for x, _ in grid]
        ys = [y for _, y in grid]
        for y in range(min(ys), max(ys) + 1):
            row = "".join(grid.get((x, y), " ") for x in range(min(xs), max(xs) + 1))
            lines.append(f"{y:4} {row}")
        lines.append("")
    return lines


MEASURE_TEMPLATE = """-- Generated by pnr.py. Builds the block and its rig, then reads the rate.
local BLUEPRINT = "BPSTRING"
local MEASURE_TICK = TICKS
local WARMUP_TICK = WARMUPTICKS
local MULTIPLIER = SOLAR
local EXPECTED = EXPECTED_TABLE
local INPUT_ITEMS = INPUT_TABLE
local OUTPUT_ITEMS = OUTPUT_TABLE
local warmup = nil

local function report(text)
    log(text)
    game.print(text)
end

local function build()
    local surface = game.surfaces[1]
    surface.request_to_generate_chunks({0, 0}, 24)
    surface.force_generate_chunk_requests()
    surface.daytime = 0.25
    surface.freeze_daytime = true
    surface.solar_power_multiplier = MULTIPLIER
    game.forces.player.enable_all_recipes()

    local inv = game.create_inventory(1)
    inv[1].set_stack{name = "blueprint", count = 1}
    local imported = inv[1].import_stack(BLUEPRINT)
    report("rig: import_stack " .. tostring(imported))
    if imported ~= 0 then return end

    local built = inv[1].build_blueprint{surface = surface, force = game.forces.player, position = {0, 0}}
    local revived = 0
    for _, ghost in pairs(surface.find_entities_filtered{name = "entity-ghost", limit = 100000}) do
        if ghost.revive() then revived = revived + 1 end
    end
    report("rig: blueprint entities " .. tostring(#built) .. ", revived " .. tostring(revived))
end

local function diagnose()
    local surface = game.surfaces[1]
    local status_names = {}
    for name, id in pairs(defines.entity_status) do status_names[id] = name end
    local machines = surface.find_entities_filtered{name = "assembling-machine-3", limit = 200}
    local statuses = {}
    for _, machine in pairs(machines) do
        local status = tostring(status_names[machine.status] or machine.status)
        statuses[status] = (statuses[status] or 0) + 1
    end
    report("diag: machines " .. #machines)
    for status, count in pairs(statuses) do
        report("diag: machine status " .. status .. " x" .. count)
    end
    local inserters = surface.find_entities_filtered{name = "bulk-inserter", limit = 500}
    local inserter_status = {}
    for _, inserter in pairs(inserters) do
        local status = tostring(status_names[inserter.status] or inserter.status)
        inserter_status[status] = (inserter_status[status] or 0) + 1
    end
    report("diag: inserters " .. #inserters)
    for status, count in pairs(inserter_status) do
        report("diag: inserter status " .. status .. " x" .. count)
    end
    local on_belts = 0
    for _, belt in pairs(surface.find_entities_filtered{name = "turbo-transport-belt", limit = 2000}) do
        for _, item in pairs(OUTPUT_ITEMS) do
            on_belts = on_belts + belt.get_transport_line(1).get_item_count(item)
                + belt.get_transport_line(2).get_item_count(item)
        end
    end
    report("diag: output items on all belts " .. on_belts)
end

local function totals()
    local totals = {}
    for _, chest in pairs(game.surfaces[1].find_entities_filtered{name = "steel-chest", limit = 200}) do
        local contents = chest.get_inventory(defines.inventory.chest)
        for _, item in pairs(OUTPUT_ITEMS) do
            totals[item] = (totals[item] or 0) + contents.get_item_count(item)
        end
    end
    return totals
end

local function measure()
    local now = totals()
    local window = (MEASURE_TICK - WARMUP_TICK) / 3600.0
    for _, item in pairs(OUTPUT_ITEMS) do
        local got = ((now[item] or 0) - ((warmup or {})[item] or 0)) / window
        local want = EXPECTED[item] or 0
        report(string.format(
            "measure: %s %.1f per minute after warm-up, plan %.1f, ratio %.3f",
            item, got, want, got / want))
    end
end

script.on_init(function()
    local ok, err = pcall(build)
    report("rig: build " .. (ok and "ok" or ("failed: " .. tostring(err))))
end)

script.on_event(defines.events.on_tick, function(event)
    if event.tick == WARMUP_TICK then
        warmup = totals()
        diagnose()
        report("rig: warm-up done at " .. WARMUP_TICK)
        return
    end
    if event.tick ~= MEASURE_TICK then return end
    local ok, err = pcall(measure)
    report("rig: measure " .. (ok and "ok" or ("failed: " .. tostring(err))))
    script.on_event(defines.events.on_tick, nil)
end)
"""


def lua_value(value):
    """Render a Python value as a Lua literal."""
    if isinstance(value, dict):
        pairs = ", ".join(f"[{lua_value(k)}] = {lua_value(v)}" for k, v in value.items())
        return "{" + pairs + "}"
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(lua_value(item) for item in value) + "}"
    if isinstance(value, str):
        return '"' + value + '"'
    return repr(value)


def write_mod(block, entities, mod_dir, ticks):
    """Write the probe mod that builds the blueprint and measures it."""
    mod = pathlib.Path(mod_dir)
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "info.json").write_text(
        json.dumps(
            {
                "name": "fmgen",
                "version": "0.1.0",
                "title": "Factorio generator",
                "author": "omp",
                "factorio_version": "2.1",
                "dependencies": ["base >= 2.1", "space-age"],
            },
            indent=2,
        )
    )
    (mod / "data.lua").write_text(
        "-- Unlimited ore, held at the rate the drill gives. See save.md.\n"
        'for _, name in pairs{"iron-ore", "copper-ore", "coal", "stone", "uranium-ore"} do\n'
        '    local res = data.raw["resource"][name]\n'
        "    res.infinite = true\n"
        "    res.minimum = 1000000\n"
        "    res.normal = 1000000\n"
        "end\n"
    )

    expected = {name: block.total_rate * amount for name, amount in block.results}
    text = (
        MEASURE_TEMPLATE.replace("WARMUPTICKS", str(ticks // 4))
        .replace("TICKS", str(ticks))
        .replace("SOLAR", str(RIG_SOLAR_MULTIPLIER))
        .replace("EXPECTED_TABLE", lua_value(expected))
        .replace("INPUT_TABLE", lua_value([name for name, _ in block.ingredients]))
        .replace("OUTPUT_TABLE", lua_value([name for name, _ in block.results]))
        # Last: the exchange string is opaque data and may contain a token.
        .replace("BPSTRING", blueprint_string(entities))
    )
    (mod / "control.lua").write_text(text)
    return expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["block", "measure"])
    parser.add_argument("recipe")
    parser.add_argument("crafts_per_min", type=float)
    parser.add_argument("--belt", default="turbo-transport-belt")
    parser.add_argument("--inserter", default="bulk-inserter")
    parser.add_argument("--dump", default=solve.DEFAULT_DUMP)
    parser.add_argument("--out", default=None)
    parser.add_argument("--mod", default=None)
    parser.add_argument("--ticks", type=int, default=1800)
    parser.add_argument("--ascii", action="store_true")
    args = parser.parse_args()

    data = solve.Data(args.dump)
    block = build_block(data, args.recipe, args.crafts_per_min, args.belt, args.inserter)
    entities = block_entities(block, args.belt, args.inserter)
    rig = rig_entities(
        block, [name for name, _ in block.ingredients], item_rate(block, block.results)
    )
    footprints = footprints_of(data, {e["name"] for e in entities + rig})

    _, clashes = tiles_of(entities + rig, footprints)
    print(f"recipe      {args.recipe} at {args.crafts_per_min:.0f} crafts/min")
    for line in report_block(data, block, entities + rig, footprints):
        print(line)
    for name, tile in clashes[:5]:
        print(f"clash       {name} at tile {tile}")
    if args.ascii:
        for line in ascii_map(entities + rig, block, footprints):
            print(line)

    if args.mode == "block":
        string = blueprint_string(entities)
        print(f"blueprint   {len(string)} characters")
        if args.out:
            pathlib.Path(args.out).write_text(string + "\n")
            print(f"written     {args.out}")
        return

    if not args.mod:
        raise SystemExit("measure needs --mod DIR")
    expected = write_mod(block, entities + rig, args.mod, args.ticks)
    print(f"mod         {args.mod}")
    print(f"expected    {expected} per minute over {args.ticks} ticks")


if __name__ == "__main__":
    main()
