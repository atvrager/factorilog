"""Multi-block base: a bus, its taps and merges, and the vias they need.

The solver gives the rates. This file turns them into one blueprint: blocks in
topological order, a bus of lanes to their west, an inserter bridge between a
lane and a belt, and an underground belt pair wherever a belt has to cross a
lane. See pnr.md.

```
base.py plan  automation-science-pack:1 logistic-science-pack:1
base.py build automation-science-pack:1 logistic-science-pack:1 --out base.txt
base.py measure automation-science-pack:1 logistic-science-pack:1 --mod DIR
```

Geometry, bus on the west, blocks on the east:

```
    lanes (one per item, flowing south)
      |   :::== tap belt, underground under every lane in the way
      |   ||
  +---+---++--------- block row: input belt north, machines, output belt south
  +---+-------------- 
      |   :::== merge belt, the same vias, running west
      v
```

A tap is an inserter that takes from its lane and drops onto the tap belt. A
merge is an inserter that takes from the merge belt and drops onto its lane.
Neither needs a splitter: the consumer's belt backs up, the bridge stops, and
the lane keeps its items for the consumers further south.
"""

import argparse
import collections
import math
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pnr
import solve

# One lane occupies LANE_PITCH tiles: the lane, a bridge column, and slack.
# A tunnel of span LANE_PITCH crosses one lane.
LANE_PITCH = 5
BRIDGE_OFFSET = 1

# Rows of one block, relative to the row origin. The near input belt sits south
# of the machines, the second one tile further south, and a third ingredient
# comes from a belt north of the block.
INPUT_ROW = 4
INPUT_INSERTER_ROW = 3
MACHINE_ROW = 0
OUTPUT_INSERTER_ROW = -1
OUTPUT_ROW = -2
NORTH_ROW = -3
ROW_PITCH = 16

# A machine reaches two input belt rows: a normal inserter takes the near row,
# a long-handed inserter reaches the second. The taps of the two rows need
# different columns, so the near row starts one tile west of the block.
ROWS_SOUTH = 2

# Power strips: a row of substations and panels beside the bus, and a row south
# of each block. Their tiles are blocked for tunnels, which would otherwise
# land on them.
POWER_STEP = 8
MAX_VIAS = 6

# Virtual signals of the rate clock: the counter, and its value modulo the
# period.
CLOCK_SIGNAL = "signal-A"
GATE_SIGNAL = "signal-B"

# Ticks that an inserter needs for one swing. The clock enables a gated
# inserter for this many ticks, so it moves one item per period.
INSERTER_SWING_TICKS = 120


class Node:
    """One recipe of the plan: its machines, its row, its belts."""

    def __init__(self, recipe, crafts_per_min, machines, machine_name, speed):
        self.recipe = recipe
        self.crafts_per_min = crafts_per_min
        self.machines = machines
        self.machine_name = machine_name
        self.speed = speed
        self.ingredients = []
        self.results = []
        self.row = 0

    def width(self):
        return pnr.MACHINE_SPACING * self.machines


def plan(data, demand, belt, inserter):
    """Rates and machine counts for the demanded items."""
    allowed = data.allowed_recipes(["nauvis"])
    rates, raw, surplus = solve.solve_rates(data, demand, allowed)

    nodes = []
    for recipe, crafts in sorted(rates.items()):
        recipe_data = data.recipes[recipe]
        machine = data.machine_for(recipe_data)
        if machine is None:
            raise SystemExit(f"no machine holds the categories of {recipe}")
        machine_name, speed = machine
        per_machine = 60.0 * speed / data.craft_seconds(recipe_data)
        node = Node(recipe, crafts, max(1, math.ceil(crafts / per_machine)),
                    machine_name, speed)
        node.ingredients = [(solve.name_of(e), solve.amount_of(e))
                            for e in data.ingredients_of(recipe_data)]
        node.results = [(solve.name_of(e), solve.amount_of(e))
                        for e in data.results_of(recipe_data)]
        nodes.append(node)

    order_rows(nodes)
    return nodes, rates, raw, surplus


def order_rows(nodes):
    """Topological depth: a producer sits above every consumer of its item."""
    producer = {}
    for node in nodes:
        for item, _ in node.results:
            producer[item] = node

    depth = {}

    def visit(node):
        if node.recipe in depth:
            return depth[node.recipe]
        depth[node.recipe] = 0        # guards a cycle; the plan has none
        best = 0
        for item, _ in node.ingredients:
            source = producer.get(item)
            if source is not None and source is not node:
                best = max(best, visit(source) + 1)
        depth[node.recipe] = best
        return best

    for node in nodes:
        visit(node)
    nodes.sort(key=lambda n: (depth[n.recipe], n.recipe))
    for index, node in enumerate(nodes):
        node.row = index


def assign_lanes(nodes):
    """One lane per item, the busiest lane nearest the blocks.

    A tap tunnels under every lane between its own and the block, so a widely
    used item belongs on the east side of the bus where nothing is in the way.
    """
    consumers = collections.Counter()
    items = set()
    for node in nodes:
        for item, _ in node.ingredients:
            consumers[item] += 1
            items.add(item)
        for item, _ in node.results:
            items.add(item)
    lanes = sorted(items, key=lambda item: (-consumers.get(item, 0), item))
    return {item: index for index, item in enumerate(lanes)}, lanes


def lane_x(index):
    return index * LANE_PITCH


class Router:
    """Places belt runs, with an underground pair under every lane in the way."""

    def __init__(self, entities, add, belt, blocked, span=11):
        self.entities = entities
        self.add = add
        self.belt = belt
        self.blocked = blocked
        self.span = span
        # One tunnel covers as many blocked columns as its span allows.
        self.per_tunnel = max(1, span // LANE_PITCH)
        self.vias = 0

    def run(self, start_x, end_x, y, direction, port_start=False, port_end=False):
        """Belt from start_x to end_x, inclusive, in the given direction.

        An underground pair crosses up to `per_tunnel` blocked columns at once.
        Its ports sit on free tiles, so the tunnel itself passes under the
        lanes and under the power strip.
        """
        step = 1 if direction == pnr.EAST else -1
        if (end_x - start_x) * step < 0:
            raise SystemExit(f"bad run {start_x} -> {end_x} facing {direction}")

        x = start_x
        while (x - end_x) * step <= 0:
            if x in self.blocked:
                raise SystemExit(f"run {start_x} -> {end_x} starts on blocked tile {x}")
            crossing = sorted((c for c in self.blocked
                               if (c - x) * step > 0 and (c - end_x) * step <= 0),
                              key=lambda c: (c - x) * step)
            if not crossing:
                self.add(self.belt, x + 0.5, y + 0.5, direction)
                x += step
                continue

            chosen = None
            for count in range(min(self.per_tunnel, len(crossing)), 0, -1):
                try:
                    chosen = (crossing[count - 1],
                              self.find_exit(x, crossing[count - 1], end_x, step))
                    break
                except SystemExit:
                    continue
            if chosen is None:
                raise SystemExit(f"no tunnel from {x} over {crossing} to {end_x}")
            last, exit_x = chosen
            self.add("turbo-underground-belt", x + 0.5, y + 0.5, direction,
                     {"type": "input"})
            self.add("turbo-underground-belt", exit_x + 0.5, y + 0.5, direction,
                     {"type": "output"})
            self.vias += 1
            x = exit_x + step

    def find_exit(self, x, last, end_x, step):
        """A free tile past the columns to cross, within the underground span.

        The tile after the exit must be free as well, because the next tunnel
        starts there.
        """
        for offset in range(1, LANE_PITCH + 1):
            exit_x = last + step * offset
            if exit_x in self.blocked or (exit_x - x) * step > self.span:
                continue
            if (exit_x - end_x) * step > 0:
                continue
            next_x = exit_x + step
            if (next_x - end_x) * step <= 0 and next_x in self.blocked:
                continue
            return exit_x
        raise SystemExit(f"no tunnel from {x} past {last} to {end_x}")


def build(data, nodes, lane_index, demand, belt, inserter):
    """Every entity of the base, and the rig that feeds and drains it."""
    entities = []
    vias = {"count": 0}

    def add(name, x, y, direction=None, extra=None):
        """Position is the entity centre, so the caller rounds to the tile."""
        entity = {"entity_number": len(entities) + 1, "name": name,
                  "position": {"x": round(x, 2), "y": round(y, 2)}}
        if direction is not None:
            entity["direction"] = direction
        if extra:
            entity.update(extra)
        entities.append(entity)
        return entity

    def wire(number, port, other, other_port):
        """Join one circuit port of two entities, both ways."""
        for source, side, target in ((number, port, other), (other, other_port, number)):
            ports = entities[source - 1].setdefault("connections", {})
            ports.setdefault(str(side), {}).setdefault("red", []).append(
                {"entity_id": target, "circuit_id": 1})

    lane_columns = {lane_x(index) for index in lane_index.values()}
    lanes = len(lane_index)
    block_x = (lanes + 1) * LANE_PITCH
    # The power strips sit between the runs: above the machines, clear of the
    # tap row and the merge row.
    west_power = block_x - 4
    east_power = block_x + 14
    router = Router(entities, add, belt, lane_columns)

    width = max(node.width() for node in nodes)
    block_east = block_x + width + 2
    height = ROW_PITCH * (len(nodes) + 1)

    # Lanes flow south, from the feed at the top to the drain at the bottom.
    for index in lane_index.values():
        for y in range(0, height + 1):
            add(belt, lane_x(index) + 0.5, y + 0.5, pnr.SOUTH)

    row_y = {}
    output_inserters = collections.defaultdict(list)
    for node in nodes:
        y = ROW_PITCH * (node.row + 1)
        row_y[node.recipe] = y

        # The block: machines, an output belt, and one input belt row per
        # ingredient. The near row starts one tile west of the block so that
        # its tap column clears the second row.
        south = node.ingredients[:ROWS_SOUTH]
        north = node.ingredients[ROWS_SOUTH:]

        for x in range(block_x, block_east + 1):
            add(belt, x + 0.5, y + OUTPUT_ROW + 0.5, pnr.WEST)
        for offset, _ in enumerate(south):
            start = block_x - 1 if offset == 0 else block_x
            for x in range(start, block_east + 1):
                add(belt, x + 0.5, y + INPUT_ROW + offset + 0.5, pnr.EAST)

        for index in range(node.machines):
            centre = block_x + 1.5 + pnr.MACHINE_SPACING * index
            add(node.machine_name, centre, y + MACHINE_ROW + 1.5, None,
                {"recipe": node.recipe})
            # A normal inserter takes the near belt row, a long-handed one the
            # second, on a different tile of the machine. A third ingredient
            # comes from the north, over the output belt, so its long-handed
            # inserter faces north.
            if south:
                add("bulk-inserter", centre, y + INPUT_INSERTER_ROW + 0.5, pnr.SOUTH)
            if len(south) > 1:
                add("long-handed-inserter", centre - 1, y + INPUT_INSERTER_ROW + 0.5,
                    pnr.SOUTH)
            output_inserters[node.recipe].append(
                add("bulk-inserter", centre, y + OUTPUT_INSERTER_ROW + 0.5, pnr.SOUTH))
            if north:
                add("long-handed-inserter", centre - 1, y + OUTPUT_INSERTER_ROW + 0.5,
                    pnr.NORTH)

        # Taps: a bridge from the lane, a belt east on its own row, then a
        # north column onto the belt of its own ingredient. One row per
        # ingredient, or two taps would share a row and collide.
        for offset, (item, _) in enumerate(south):
            index = lane_index.get(item)
            if index is None:
                continue
            tap_row = y + INPUT_ROW + 2 + offset
            x = lane_x(index) + BRIDGE_OFFSET
            add("fast-inserter", x + 0.5, tap_row + 0.5, pnr.WEST)
            target = block_x - 1 + offset
            router.run(x + 1, target - 1, tap_row, pnr.EAST)
            for row in range(y + INPUT_ROW + offset + 1, tap_row + 1):
                add(belt, target + 0.5, row + 0.5, pnr.NORTH)

        # A third ingredient runs east on the north row instead, and its
        # long-handed inserter takes it two tiles in.
        for item, _ in north:
            index = lane_index.get(item)
            if index is None:
                continue
            tap_row = y + NORTH_ROW
            x = lane_x(index) + BRIDGE_OFFSET
            add("fast-inserter", x + 0.5, tap_row + 0.5, pnr.WEST)
            end = block_x + pnr.MACHINE_SPACING * (node.machines - 1)
            router.run(x + 1, end, tap_row, pnr.EAST)

        # One merge: the output belt runs west, a bridge drops onto the lane.
        item = node.results[0][0]
        index = lane_index.get(item)
        if index is None:
            continue
        x = lane_x(index) + BRIDGE_OFFSET
        router.run(block_x - 1, x + 1, y + OUTPUT_ROW, pnr.WEST)
        add("fast-inserter", x + 0.5, y + OUTPUT_ROW + 0.5, pnr.EAST)


    # A demanded item holds its plan rate. The rig feeds every raw item without
    # a limit, so a machine runs at full speed and the measured rate is far
    # above the plan. A clock holds the output inserter of the demanded machine
    # to one item each period, and the chain behind it backs up.
    for node in nodes:
        want = demand.get(node.results[0][0])
        if not want:
            continue
        period = max(2, round(3600.0 / want))
        window = min(period, INSERTER_SWING_TICKS)
        y = row_y[node.recipe]
        clock = add("arithmetic-combinator", block_x - 8.5, y + 3.5, pnr.NORTH,
                    {"control_behavior": {"arithmetic_conditions": {
                        "operation": "+",
                        "first_signal": {"type": "virtual", "name": CLOCK_SIGNAL},
                        "second_constant": 1,
                        "output_signal": {"type": "virtual", "name": CLOCK_SIGNAL}}}})
        gate = add("arithmetic-combinator", block_x - 1.5, y + 3.5, pnr.NORTH,
                   {"control_behavior": {"arithmetic_conditions": {
                       "operation": "%",
                       "first_signal": {"type": "virtual", "name": CLOCK_SIGNAL},
                       "second_constant": period,
                       "output_signal": {"type": "virtual", "name": GATE_SIGNAL}}}})
        wire(clock["entity_number"], 1, clock["entity_number"], 2)
        wire(clock["entity_number"], 2, gate["entity_number"], 1)
        for inserter in output_inserters[node.recipe]:
            inserter["control_behavior"] = {
                "circuit_enable_disable": True,
                "circuit_condition": {
                    "first_signal": {"type": "virtual", "name": GATE_SIGNAL},
                    "comparator": "<",
                    "constant": window}}
            wire(gate["entity_number"], 2, inserter["entity_number"], 1)

    # Feed every lane that nothing produces, drain every demanded item.
    produced = {item for node in nodes for item, _ in node.results}
    for item, index in lane_index.items():
        if item in produced:
            continue
        add("infinity-chest", lane_x(index) + BRIDGE_OFFSET + 1.5, 0.5, None,
            {"infinity_settings": {"remove_unfiltered_items": True,
                                   "filters": [{"name": item, "count": 1000,
                                                "mode": "at-least", "index": 1}]}})
        add("fast-inserter", lane_x(index) + BRIDGE_OFFSET + 0.5, 0.5, pnr.EAST)

    for item in demand:
        index = lane_index.get(item)
        if index is None:
            continue
        add("fast-inserter", lane_x(index) + BRIDGE_OFFSET + 0.5, height + 0.5, pnr.WEST)
        add("steel-chest", lane_x(index) + BRIDGE_OFFSET + 1.5, height + 0.5)

    # Power. A substation reaches 9 tiles, so one row of them covers the bus
    # and a block. A panel stands five tiles from its substation, so every
    # network has a source. A pair that would land on a block is dropped: the
    # block owns its tiles.
    def clear_of_block(x, width):
        first = int(round(x - width / 2.0))
        return first + width - 1 < block_x or first > block_east

    power_rows = [4] + [row_y[node.recipe] for node in nodes] + [height + 4]
    for y in power_rows:
        for x in (2.5, 17.5, 32.5, 47.5, 53.5, 71.5):
            if clear_of_block(x, 2):
                add("substation", x, y + 1.5)
        for x in (7.5, 22.5, 37.5, 56.5, 67.5):
            if clear_of_block(x, 3):
                add("solar-panel", x, y + 1.5)

    vias["count"] = router.vias
    return entities, block_x, height, vias


def report(data, nodes, entities, footprints, vias):
    lines = []
    for node in nodes:
        in_rate = sum(node.crafts_per_min / 60.0 * amount for _, amount in node.ingredients)
        out_rate = sum(node.crafts_per_min / 60.0 * amount for _, amount in node.results)
        lines.append(f"  {node.recipe:28} {node.machines:3} machine(s) "
                     f"in {in_rate:8.3f}/s out {out_rate:8.3f}/s row {node.row}")
    _, clashes = pnr.tiles_of(entities, footprints)
    lines.append(f"blocks      {len(nodes)}")
    lines.append(f"entities    {len(entities)}")
    lines.append(f"vias        {vias['count']} underground pairs")
    lines.append(f"overlap     {len(clashes)} tiles claimed twice")
    for name, tile in clashes[:8]:
        lines.append(f"clash       {name} at {tile}")
    lines.append(f"power       {pnr.check_power(data, entities)} entities outside a supply area")
    return lines


def demand_of(args):
    demand = {}
    for text in args.demand:
        item, _, rate = text.partition(":")
        demand[item] = float(rate or 1.0)
    return demand


MEASURE_TEMPLATE = '''-- Generated by base.py. Builds the base and reads the demanded items back.
local BLUEPRINT = "BPSTRING"
local MEASURE_TICK = TICKS
local WARMUP_TICK = WARMUPTICKS
local MULTIPLIER = SOLAR
local EXPECTED = EXPECTED_TABLE
local OUTPUT_ITEMS = OUTPUT_TABLE
local warmup = nil

local function report(text)
    log(text)
    game.print(text)
end

local function totals()
    local found = {}
    for _, chest in pairs(game.surfaces[1].find_entities_filtered{name = "steel-chest", limit = 200}) do
        local contents = chest.get_inventory(defines.inventory.chest)
        for _, item in pairs(OUTPUT_ITEMS) do
            found[item] = (found[item] or 0) + contents.get_item_count(item)
        end
    end
    return found
end

local function build()
    local surface = game.surfaces[1]
    surface.request_to_generate_chunks({0, 0}, 40)
    surface.force_generate_chunk_requests()
    surface.daytime = 0.25
    surface.freeze_daytime = true
    surface.solar_power_multiplier = MULTIPLIER
    game.forces.player.enable_all_recipes()

    -- One unplaceable entity stops a whole blueprint build: the game then
    -- builds nothing at all. Landfill the water, and clear the rocks and
    -- trees, or a single one of them costs the whole base.
    local tiles = {}
    for x = -200, 200 do
        for y = -200, 300 do
            tiles[#tiles + 1] = {name = "landfill", position = {x, y}}
        end
    end
    surface.set_tiles(tiles)
    for _, entity in pairs(surface.find_entities_filtered{area = {{-200, -200}, {200, 300}},
                                                          limit = 1000000}) do
        if entity.type ~= "resource" then
            entity.destroy()
        end
    end

    local inv = game.create_inventory(1)
    inv[1].set_stack{name = "blueprint", count = 1}
    local imported = inv[1].import_stack(BLUEPRINT)
    report("rig: import_stack " .. tostring(imported))
    if imported ~= 0 then return end
    local parsed = inv[1].get_blueprint_entities()
    report("rig: the string holds " .. tostring(parsed and #parsed or -1) .. " entities")
    local built = inv[1].build_blueprint{surface = surface, force = game.forces.player, position = {0, 0}}
    local revived = 0
    for _, ghost in pairs(surface.find_entities_filtered{name = "entity-ghost", limit = 100000}) do
        if ghost.revive() then revived = revived + 1 end
    end
    report("rig: blueprint entities " .. tostring(#built) .. ", revived " .. tostring(revived))
    local ghosts = surface.find_entities_filtered{name = "entity-ghost", limit = 5}
    for _, ghost in pairs(ghosts) do
        report(string.format("rig: a ghost at %.1f,%.1f", ghost.position.x, ghost.position.y))
    end
end

local function diagnose()
    local surface = game.surfaces[1]
    local names = {}
    for name, id in pairs(defines.entity_status) do names[id] = name end
    local machines = {}
    for _, name in pairs{"assembling-machine-3", "electric-furnace"} do
        for _, machine in pairs(surface.find_entities_filtered{name = name, limit = 500}) do
            machines[#machines + 1] = machine
        end
    end
    local statuses = {}
    for _, machine in pairs(machines) do
        local status = tostring(names[machine.status] or machine.status)
        statuses[status] = (statuses[status] or 0) + 1
    end
    report("diag: machines " .. #machines)
    for status, count in pairs(statuses) do
        report("diag: machine status " .. status .. " x" .. count)
    end
    -- What each machine's input inserters pick from: an item count of zero on
    -- a row names the ingredient that starves the machine.
    for _, machine in pairs(machines) do
        local fed = ""
        for _, kind in pairs{"bulk-inserter", "long-handed-inserter"} do
            for _, inserter in pairs(surface.find_entities_filtered{name = kind, limit = 2000}) do
                if inserter.drop_target == machine then
                    local source = inserter.pickup_target
                    local held = "none"
                    if source and source.type == "transport-belt" then
                        held = ""
                        for _, entry in pairs(source.get_transport_line(1).get_contents()) do
                            held = held .. entry.name .. "=" .. entry.count .. " "
                        end
                        for _, entry in pairs(source.get_transport_line(2).get_contents()) do
                            held = held .. entry.name .. "=" .. entry.count .. " "
                        end
                    elseif source then
                        held = source.name
                    end
                    fed = fed .. string.format(" [%.1f,%.1f<-%s@%.1f,%.1f %s]",
                        inserter.position.x, inserter.position.y, tostring(source and source.name),
                        source and source.position.x or -9, source and source.position.y or -9, held)
                end
            end
        end
        local ok, recipe = pcall(function() return machine.get_recipe() end)
        recipe = ok and recipe or nil
        report("diag: " .. tostring(recipe and recipe.name or machine.name) .. " fed" .. fed)
    end
    for _, e in pairs(surface.find_entities_filtered{name = "solar-panel", limit = 100}) do
        local n = e.get_circuit_network(defines.wire_type.red)
        report(string.format("diag: panel %.1f,%.1f enet %s prod %s", e.position.x, e.position.y,
            tostring(e.electric_network_id), tostring(e.power_production)))
    end
    for _, e in pairs(surface.find_entities_filtered{name = "arithmetic-combinator", limit = 10}) do
        local n = e.get_circuit_network(defines.wire_type.red)
        report(string.format("diag: comb %.1f,%.1f net %s A=%s B=%s", e.position.x, e.position.y,
            tostring(e.electric_network_id),
            n and tostring(n.get_signal({type = "virtual", name = "signal-A"})),
            n and tostring(n.get_signal({type = "virtual", name = "signal-B"}))))
    end
    for _, e in pairs(surface.find_entities_filtered{name = "bulk-inserter", limit = 100}) do
        local n = e.get_circuit_network(defines.wire_type.red)
        report(string.format("diag: bulk %.1f,%.1f %s B=%s A=%s", e.position.x, e.position.y,
            tostring(names[e.status]),
            n and tostring(n.get_signal({type = "virtual", name = "signal-B"})),
            n and tostring(n.get_signal({type = "virtual", name = "signal-A"}))))
    end
    local inserters = surface.find_entities_filtered{name = "fast-inserter", limit = 1000}
    local inserter_status = {}
    for _, inserter in pairs(inserters) do
        local status = tostring(names[inserter.status] or inserter.status)
        inserter_status[status] = (inserter_status[status] or 0) + 1
    end
    report("diag: fast inserters " .. #inserters)
    for status, count in pairs(inserter_status) do
        report("diag: inserter status " .. status .. " x" .. count)
    end
    local list = {}
    for _, inserter in pairs(surface.find_entities_filtered{name = "fast-inserter", limit = 500}) do
        list[#list + 1] = inserter
    end
    table.sort(list, function(a, b)
        if a.position.y == b.position.y then return a.position.x < b.position.x end
        return a.position.y < b.position.y
    end)
    for index = 1, #list do
        local inserter = list[index]
        local pickup = inserter.pickup_target
        local drop = inserter.drop_target
        local held = "none"
        if pickup and pickup.type == "transport-belt" then
            held = ""
            for _, entry in pairs(pickup.get_transport_line(1).get_contents()) do
                held = held .. entry.name .. "=" .. entry.count .. " "
            end
            for _, entry in pairs(pickup.get_transport_line(2).get_contents()) do
                held = held .. entry.name .. "=" .. entry.count .. " "
            end
        end
        report(string.format(
            "diag: inserter %.1f,%.1f dir %d %s pickup %s@%.1f,%.1f [%s] drop %s@%.1f,%.1f",
            inserter.position.x, inserter.position.y, inserter.direction,
            tostring(names[inserter.status]),
            tostring(pickup and pickup.name), pickup and pickup.position.x or -1,
            pickup and pickup.position.y or -1, held,
            tostring(drop and drop.name), drop and drop.position.x or -1,
            drop and drop.position.y or -1))
    end
    local on_belts = {}
    for _, belt in pairs(surface.find_entities_filtered{name = "turbo-transport-belt", limit = 5000}) do
        for _, line in pairs{1, 2} do
            local contents = belt.get_transport_line(line).get_contents()
            for _, entry in pairs(contents) do
                on_belts[entry.name] = (on_belts[entry.name] or 0) + entry.count
            end
        end
    end
    local text = ""
    for name, count in pairs(on_belts) do text = text .. name .. "=" .. count .. " " end
    report("diag: on belts " .. text)
    for _, chest in pairs(surface.find_entities_filtered{name = "infinity-chest", limit = 10}) do
        local inventory = chest.get_inventory(defines.inventory.chest)
        local held = ""
        for _, entry in pairs(inventory.get_contents()) do
            held = held .. entry.name .. "=" .. entry.count .. " "
        end
        report(string.format("diag: chest at %.1f,%.1f holds %s", chest.position.x,
            chest.position.y, held))
    end
end

local function measure()
    local now = totals()
    local window = (MEASURE_TICK - WARMUP_TICK) / 3600.0
    local stats = game.forces.player.get_item_production_statistics(game.surfaces[1])
    for _, item in pairs{"copper-ore", "iron-ore", "copper-plate", "iron-plate",
                         "copper-cable", "iron-gear-wheel", "electronic-circuit",
                         "transport-belt", "inserter",
                         "automation-science-pack", "logistic-science-pack"} do
        report(string.format("prod: %s made %d", item, stats.get_input_count(item)))
    end
    for _, item in pairs(OUTPUT_ITEMS) do
        local got = ((now[item] or 0) - ((warmup or {})[item] or 0)) / window
        local want = EXPECTED[item] or 0
        report(string.format("measure: %s %.2f per minute after warm-up, plan %.2f, ratio %.3f",
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
'''


def write_mod(entities, demand, mod_dir, ticks):
    mod = pathlib.Path(mod_dir)
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "info.json").write_text(
        '{"name": "fmgen", "version": "0.1.0", "title": "Factorio generator",\n'
        ' "author": "omp", "factorio_version": "2.1",\n'
        ' "dependencies": ["base >= 2.1", "space-age"]}\n')
    text = (MEASURE_TEMPLATE
            .replace("WARMUPTICKS", str(ticks // 4))
            .replace("TICKS", str(ticks))
            .replace("SOLAR", str(pnr.RIG_SOLAR_MULTIPLIER))
            .replace("EXPECTED_TABLE", pnr.lua_value(demand))
            .replace("OUTPUT_TABLE", pnr.lua_value(sorted(demand)))
            .replace("BPSTRING", pnr.blueprint_string(entities)))
    (mod / "control.lua").write_text(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["plan", "build", "measure"])
    parser.add_argument("demand", nargs="+")
    parser.add_argument("--dump", default=solve.DEFAULT_DUMP)
    parser.add_argument("--belt", default="turbo-transport-belt")
    parser.add_argument("--inserter", default="fast-inserter")
    parser.add_argument("--out", default=None)
    parser.add_argument("--mod", default=None)
    parser.add_argument("--ticks", type=int, default=18000)
    args = parser.parse_args()

    data = solve.Data(args.dump)
    demand = demand_of(args)
    nodes, rates, raw, surplus = plan(data, demand, args.belt, args.inserter)
    lane_index, lanes = assign_lanes(nodes)
    entities, block_x, height, vias = build(data, nodes, lane_index, demand,
                                            args.belt, args.inserter)

    footprints = pnr.footprints_of(data, {e["name"] for e in entities})
    print(f"demand      {demand}")
    print(f"geometry    bus {len(lanes)} lanes to x {block_x}, height {height}")
    for line in report(data, nodes, entities, footprints, vias):
        print(line)

    if args.mode == "build":
        string = pnr.blueprint_string(entities)
        print(f"blueprint   {len(string)} characters")
        if args.out:
            pathlib.Path(args.out).write_text(string + "\n")
            print(f"written     {args.out}")
        return

    if args.mode == "measure":
        if not args.mod:
            raise SystemExit("measure needs --mod DIR")
        write_mod(entities, demand, args.mod, args.ticks)
        print(f"mod         {args.mod}")
        print(f"expected    {demand} per minute over {args.ticks} ticks")


if __name__ == "__main__":
    main()
