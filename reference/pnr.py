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

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import pathlib
import sys
import zlib
from collections.abc import Iterable, Sequence

sys.path.extend(
    [
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "foundry"),
    ]
)
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
EDGE_OFFSETS: list[float] = [-1.0, 0.0, 1.0]
TILE_CENTRE_OFFSET = 0.5
MACHINE_CENTRE_OFFSET = 1.5

# Rows between two blocks, so the rig of one block clears the next.
BLOCK_ROW_HEIGHT = 20

# Inserter throughput, items per second, for the checks. Measured on 2.1.20
# with no inserter-capacity research: a bulk inserter moves one item per swing
# until the research lands, so it matches a fast inserter. A stack inserter
# always uses its stack.
INSERTER_ITEMS_PER_SECOND: dict[str, float] = {
    "inserter": 0.83,
    "fast-inserter": 2.57,
    "bulk-inserter": 2.57,
    "stack-inserter": 12.83,
}
DEFAULT_INSERTER_CAPACITY = 4.62
SECONDS_PER_MINUTE = 60.0
BELT_TILES_TO_ITEMS_PER_SECOND = 8.0

# Test rig. Solar panels with a raised multiplier power the block, so the
# measurement does not need a power plant.
SUBSTATION_Y = 6.0
PANEL_Y = 11.5
RIG_PANEL_COLUMNS = 6
RIG_PANEL_ROWS = 2
RIG_SOLAR_MULTIPLIER = 50
POWER_SPACING = 17
INFINITY_FILTER_COUNT = 1000
INFINITY_CHEST_X_OFFSET = 4.5
LOADER_X_OFFSET = 3.0
SUBSTATION_X_OFFSET = 4.5
SUBSTATION_RIGHT_MARGIN = 7
PANEL_SPACING = 3
BLUEPRINT_VERSION = 562949953421312
ZLIB_COMPRESSION_LEVEL = 9
CAPACITY_TOLERANCE = 1e-9
ASCII_MIN_Y = -4
ASCII_MAX_Y = 24
WARMUP_DIVISOR = 4
DEFAULT_TICKS = 1800
MAX_REPORTED_CLASHES = 5

GLYPHS: dict[str, str] = {
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


class Block:
    """One branch: a row of machines with its belts, inserters, and ports."""

    def __init__(
        self,
        recipe: str,
        total_rate: float,
        branches: int,
        machines: int,
        machine_name: str,
        speed: float,
    ) -> None:
        """Initialize a recipe block with rate and machine parameters."""
        self.recipe = recipe
        self.total_rate = total_rate  # crafts per minute, all branches
        self.branches = branches
        self.machines = machines  # machines per branch
        self.machine_name = machine_name
        self.speed = speed
        self.ingredients: list[tuple[str, float]] = []
        self.results: list[tuple[str, float]] = []
        self.belt = ""
        self.inserter = ""
        self.input_inserters = 1
        self.output_inserters = 1

    def origins(self) -> list[tuple[int, int]]:
        """Return the origin coordinates for each branch in the block."""
        return [(0, BLOCK_ROW_HEIGHT * b) for b in range(self.branches)]

    def branch_rate(self) -> float:
        """Return the craft rate per minute for one branch."""
        return self.total_rate / self.branches

    def width(self) -> int:
        """Return the tile width of one branch."""
        return MACHINE_SPACING * self.machines


def belt_capacity(data: solve.Data, belt: str) -> float:
    """Return items per second on one belt from the prototype speed."""
    speed = float(data.prototypes["transport-belt"][belt]["speed"])
    return speed * SECONDS_PER_MINUTE * BELT_TILES_TO_ITEMS_PER_SECOND


def _append_entity(
    entities: list[dict[str, object]],
    name: str,
    x: float,
    y: float,
    direction: int | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Append a placed entity dictionary to the entity list."""
    entity: dict[str, object] = {
        "entity_number": len(entities) + 1,
        "name": name,
        "position": {"x": round(x, 2), "y": round(y, 2)},
    }
    if direction is not None:
        entity["direction"] = direction
    if extra:
        entity.update(extra)
    entities.append(entity)
    return entity


def block_entities(block: Block, belt: str, inserter: str) -> list[dict[str, object]]:
    """Return every entity of the blocks as blueprint JSON."""
    entities: list[dict[str, object]] = []

    for origin_x, origin_y in block.origins():
        # Belts run one tile past the outer machines, so a loader reaches them.
        for x in range(-2, MACHINE_SPACING * block.machines):
            _append_entity(
                entities,
                belt,
                origin_x + x + TILE_CENTRE_OFFSET,
                origin_y + INPUT_BELT_Y + TILE_CENTRE_OFFSET,
                EAST,
            )
            _append_entity(
                entities,
                belt,
                origin_x + x + TILE_CENTRE_OFFSET,
                origin_y + OUTPUT_BELT_Y + TILE_CENTRE_OFFSET,
                EAST,
            )

        for index in range(block.machines):
            centre = origin_x + MACHINE_CENTRE_OFFSET + MACHINE_SPACING * index
            _append_entity(
                entities,
                block.machine_name,
                centre,
                origin_y + MACHINE_Y,
                extra={"recipe": block.recipe},
            )
            # An inserter picks from the side it faces. The input inserter is
            # north of the input belt, so it faces south at the belt. The
            # output inserter is north of the machine, so it faces south at
            # the machine.
            for offset in EDGE_OFFSETS[: block.input_inserters]:
                _append_entity(
                    entities,
                    inserter,
                    centre + offset,
                    origin_y + INPUT_INSERTER_Y + TILE_CENTRE_OFFSET,
                    SOUTH,
                )
            for offset in EDGE_OFFSETS[: block.output_inserters]:
                _append_entity(
                    entities,
                    inserter,
                    centre + offset,
                    origin_y + OUTPUT_INSERTER_Y + TILE_CENTRE_OFFSET,
                    SOUTH,
                )

    return entities


def rig_entities(
    block: Block,
    input_items: Sequence[str],
    output_rate: float,
) -> list[dict[str, object]]:
    """Build the test rig with feed, drain, and power entities.

    The drain needs one inserter per 4.62 items/s, because a single inserter
    caps the whole block. The measurement rig therefore drains along the belt,
    not at its end.
    """
    entities: list[dict[str, object]] = []
    drain = max(
        1, math.ceil(output_rate / block.branches / INSERTER_ITEMS_PER_SECOND["bulk-inserter"])
    )

    filters = [
        {"name": item, "count": INFINITY_FILTER_COUNT, "mode": "at-least", "index": i + 1}
        for i, item in enumerate(input_items)
    ]
    for origin_x, origin_y in block.origins():
        right = origin_x + MACHINE_SPACING * block.machines
        # Feed: a chest, a loader, then the input belt.
        _append_entity(
            entities,
            "infinity-chest",
            origin_x - INFINITY_CHEST_X_OFFSET,
            origin_y + INPUT_BELT_Y + TILE_CENTRE_OFFSET,
            extra={"infinity_settings": {"remove_unfiltered_items": True, "filters": filters}},
        )
        _append_entity(
            entities,
            "turbo-loader",
            origin_x - LOADER_X_OFFSET,
            origin_y + INPUT_BELT_Y + TILE_CENTRE_OFFSET,
            EAST,
        )
        # Drain: inserters along the output belt, each facing it, each with a
        # chest behind it.
        for index in range(drain):
            x = right - 1 - index
            _append_entity(
                entities,
                "bulk-inserter",
                x + TILE_CENTRE_OFFSET,
                origin_y + OUTPUT_BELT_Y - TILE_CENTRE_OFFSET,
                SOUTH,
            )
            _append_entity(
                entities,
                "steel-chest",
                x + TILE_CENTRE_OFFSET,
                origin_y + OUTPUT_BELT_Y - MACHINE_CENTRE_OFFSET,
            )
        # Power: substations above the block, panels above them. A substation
        # covers 18 tiles around itself, so one row reaches the whole block.
        substation_cols = int(math.ceil((right + SUBSTATION_RIGHT_MARGIN) / POWER_SPACING))
        for column in range(substation_cols):
            _append_entity(
                entities,
                "substation",
                origin_x + SUBSTATION_X_OFFSET + POWER_SPACING * column,
                origin_y + SUBSTATION_Y,
            )
        for row in range(RIG_PANEL_ROWS):
            for column in range(RIG_PANEL_COLUMNS):
                _append_entity(
                    entities,
                    "solar-panel",
                    origin_x + MACHINE_CENTRE_OFFSET + PANEL_SPACING * column,
                    origin_y + PANEL_Y + PANEL_SPACING * row,
                )

    return entities


def blueprint_string(entities: Sequence[dict[str, object]]) -> str:
    """Encode the entity list as a Factorio blueprint exchange string."""
    document = {
        "blueprint": {
            "item": "blueprint",
            "label": "pnr block",
            "version": BLUEPRINT_VERSION,
            "entities": list(entities),
        }
    }
    raw = json.dumps(document, separators=(",", ":")).encode()
    return "0" + base64.b64encode(zlib.compress(raw, ZLIB_COMPRESSION_LEVEL)).decode()


def footprints_of(data: solve.Data, names: Iterable[str]) -> dict[str, tuple[int, int]]:
    """Return the tile footprint of each entity from its collision box.

    A collision box is the physical size. The tile footprint is that size
    rounded up. An entity whose long axis is horizontal when it faces east
    needs the axes swapped, which is how the loader works.
    """
    name_set = set(names)
    result: dict[str, tuple[int, int]] = {}
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
            if name in name_set and box is not None:
                result[name] = (math.ceil(box[1][0] - box[0][0]), math.ceil(box[1][1] - box[0][1]))
    return result


def footprint_of(
    footprints: dict[str, tuple[int, int]],
    entity: dict[str, object],
) -> tuple[int, int] | None:
    """Return the footprint of one placed entity turned with its direction."""
    box = footprints.get(str(entity["name"]))
    if box is None:
        return None
    width, height = box
    if width != height and entity.get("direction", NORTH) in (EAST, WEST):
        width, height = height, width
    return width, height


def _entity_tiles(
    entity: dict[str, object],
    footprints: dict[str, tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return the list of tile coordinates covered by one placed entity."""
    box = footprint_of(footprints, entity)
    pos = entity.get("position")
    if box is None or not isinstance(pos, dict):
        return []
    width, height = box
    cx = int(round(float(pos["x"]) - width / 2))
    cy = int(round(float(pos["y"]) - height / 2))
    return [(cx + dx, cy + dy) for dx in range(width) for dy in range(height)]


def tiles_of(
    entities: Sequence[dict[str, object]],
    footprints: dict[str, tuple[int, int]],
) -> tuple[set[tuple[int, int]], list[tuple[str, tuple[int, int]]]]:
    """Return every tile an entity covers and the tiles that two entities share."""
    tiles: set[tuple[int, int]] = set()
    clashes: list[tuple[str, tuple[int, int]]] = []
    for entity in entities:
        name = str(entity["name"])
        for tile in _entity_tiles(entity, footprints):
            if tile in tiles:
                clashes.append((name, tile))
            tiles.add(tile)
    return tiles, clashes


def item_rate(block: Block, amounts_per_craft: Sequence[tuple[str, float]]) -> float:
    """Return items per second of one side of the block over every branch."""
    return block.total_rate / SECONDS_PER_MINUTE * sum(amount for _, amount in amounts_per_craft)


def build_block(
    data: solve.Data,
    recipe: str,
    crafts_per_min: float,
    belt: str,
    inserter: str,
) -> Block:
    """Plan the branches of one recipe at one rate."""
    recipe_data = data.recipes[recipe]
    machine = data.machine_for(recipe_data)
    if machine is None:
        raise SystemExit(f"no machine holds the categories of {recipe}")
    machine_name, speed = machine

    crafts_per_machine = SECONDS_PER_MINUTE * speed / data.craft_seconds(recipe_data)
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

    held = INSERTER_ITEMS_PER_SECOND.get(inserter, DEFAULT_INSERTER_CAPACITY)
    per_machine = block.branch_rate() / SECONDS_PER_MINUTE / block.machines
    block.input_inserters = min(
        len(EDGE_OFFSETS), max(1, math.ceil(per_machine * sum(a for _, a in ingredients) / held))
    )
    block.output_inserters = min(
        len(EDGE_OFFSETS), max(1, math.ceil(per_machine * sum(a for _, a in results) / held))
    )
    return block


def report_block(
    data: solve.Data,
    block: Block,
    entities: Sequence[dict[str, object]],
    footprints: dict[str, tuple[int, int]],
) -> list[str]:
    """Return the static check summary lines for the block."""
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
    per_machine = block.branch_rate() / SECONDS_PER_MINUTE / block.machines
    for side, amounts, count in (
        ("input", block.ingredients, block.input_inserters),
        ("output", block.results, block.output_inserters),
    ):
        demand = per_machine * sum(amount for _, amount in amounts)
        held = INSERTER_ITEMS_PER_SECOND.get(block.inserter, 0.0) * count
        verdict = "ok" if demand <= held + CAPACITY_TOLERANCE else "short"
        lines.append(
            f"{side:6}      {demand:6.2f} items/s per machine with "
            f"{count} x {block.inserter} at {held:5.2f} -> {verdict}"
        )
    _, clashes = tiles_of(entities, footprints)
    lines.append(f"overlap     {len(clashes)} tiles claimed twice")
    lines.append(f"power       {check_power(data, entities)} entities outside every supply area")
    return lines


def check_power(data: solve.Data, entities: Sequence[dict[str, object]]) -> int:
    """Count the power users that no substation reaches.

    A machine or a loader with no supply area is a dead entity, which the
    in-game measurement would report as a zero rate.
    """
    powered_kinds = ("assembling-machine", "furnace", "inserter", "loader")
    names: set[str] = set()
    for kind in powered_kinds:
        names |= set(data.prototypes.get(kind, {}))
    supply = float(data.prototypes["electric-pole"]["substation"]["supply_area_distance"])
    # The game keeps the boundary tile out of the supply area, so the check
    # asks for half a tile of margin. A boundary entity stays unpowered.
    reach = supply - TILE_CENTRE_OFFSET

    supplies = [
        e["position"]
        for e in entities
        if e["name"] == "substation" and isinstance(e.get("position"), dict)
    ]
    outside = 0
    for entity in entities:
        if str(entity["name"]) not in names:
            continue
        position = entity.get("position")
        if not isinstance(position, dict):
            continue
        px, py = float(position["x"]), float(position["y"])
        if not any(
            isinstance(s, dict)
            and abs(px - float(s["x"])) <= reach
            and abs(py - float(s["y"])) <= reach
            for s in supplies
        ):
            outside += 1
    return outside


def _format_grid_lines(grid: dict[tuple[int, int], str]) -> list[str]:
    """Format a coordinate-to-glyph map as ASCII rows."""
    if not grid:
        return []
    xs = [x for x, _ in grid]
    ys = [y for _, y in grid]
    rows = [
        f"{y:4} {''.join(grid.get((x, y), ' ') for x in range(min(xs), max(xs) + 1))}"
        for y in range(min(ys), max(ys) + 1)
    ]
    rows.append("")
    return rows


def ascii_map(
    entities: Sequence[dict[str, object]],
    block: Block,
    footprints: dict[str, tuple[int, int]],
) -> list[str]:
    """Return an ASCII picture of every branch drawn from the footprints."""
    origins = [origin_y for _, origin_y in block.origins()]
    grids: dict[int, dict[tuple[int, int], str]] = {oy: {} for oy in origins}
    for entity in entities:
        glyph = GLYPHS.get(str(entity["name"]), "?")
        for tx, ty in _entity_tiles(entity, footprints):
            for oy in origins:
                rel_y = ty - oy
                if ASCII_MIN_Y <= rel_y <= ASCII_MAX_Y:
                    grids[oy][(tx, rel_y)] = glyph
    lines: list[str] = []
    for oy in origins:
        lines.extend(_format_grid_lines(grids[oy]))
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


def lua_value(value: object) -> str:
    """Render a Python value as a Lua literal."""
    if isinstance(value, dict):
        pairs = ", ".join(f"[{lua_value(k)}] = {lua_value(v)}" for k, v in value.items())
        return "{" + pairs + "}"
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(lua_value(item) for item in value) + "}"
    if isinstance(value, str):
        return '"' + value + '"'
    return repr(value)


def write_mod(
    block: Block,
    entities: Sequence[dict[str, object]],
    mod_dir: str | os.PathLike[str],
    ticks: int,
) -> dict[str, float]:
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
        MEASURE_TEMPLATE.replace("WARMUPTICKS", str(ticks // WARMUP_DIVISOR))
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


def main() -> None:
    """Parse CLI arguments and run block generation or measurement mod export."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["block", "measure"])
    parser.add_argument("recipe")
    parser.add_argument("crafts_per_min", type=float)
    parser.add_argument("--belt", default="turbo-transport-belt")
    parser.add_argument("--inserter", default="bulk-inserter")
    parser.add_argument("--dump", default=solve.DEFAULT_DUMP)
    parser.add_argument("--out", default=None)
    parser.add_argument("--mod", default=None)
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--ascii", action="store_true")
    args = parser.parse_args()

    data = solve.Data(args.dump)
    block = build_block(data, args.recipe, args.crafts_per_min, args.belt, args.inserter)
    entities = block_entities(block, args.belt, args.inserter)
    rig = rig_entities(
        block, [name for name, _ in block.ingredients], item_rate(block, block.results)
    )
    footprints = footprints_of(data, {str(e["name"]) for e in entities + rig})

    _, clashes = tiles_of(entities + rig, footprints)
    print(f"recipe      {args.recipe} at {args.crafts_per_min:.0f} crafts/min")
    for line in report_block(data, block, entities + rig, footprints):
        print(line)
    for name, tile in clashes[:MAX_REPORTED_CLASHES]:
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
