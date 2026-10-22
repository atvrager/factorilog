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

from __future__ import annotations

import argparse
import collections
import math
import os
import pathlib
import sys
from collections.abc import Callable, Sequence

sys.path.extend(
    [
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "foundry"),
    ]
)
import pnr
import solve

# One lane occupies LANE_PITCH tiles: the lane, a bridge column, and slack.
# A tunnel of span LANE_PITCH crosses one lane.
LANE_PITCH = 5
BRIDGE_OFFSET = 1
DEFAULT_ROUTER_SPAN = 11
TILE_CENTRE_OFFSET = 0.5
MACHINE_CENTRE_OFFSET = 1.5
SECONDS_PER_MINUTE = 60.0
TICKS_PER_MINUTE = 3600.0
INFINITY_FILTER_COUNT = 1000
CLOCK_COMBINATOR_X_OFFSET = 8.5
GATE_COMBINATOR_X_OFFSET = 1.5
COMBINATOR_Y_OFFSET = 3.5
POWER_ROW_PADDING = 4
SUBSTATION_WIDTH = 2
SOLAR_PANEL_WIDTH = 3
SUBSTATION_X_POSITIONS: tuple[float, ...] = (2.5, 17.5, 32.5, 47.5, 53.5, 71.5)
SOLAR_PANEL_X_POSITIONS: tuple[float, ...] = (7.5, 22.5, 37.5, 56.5, 67.5)
MAX_REPORTED_CLASHES = 8
WARMUP_DIVISOR = 4
DEFAULT_TICKS = 18000

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

    def __init__(
        self,
        recipe: str,
        crafts_per_min: float,
        machines: int,
        machine_name: str,
        speed: float,
    ) -> None:
        """Initialize a plan node with recipe and machine parameters."""
        self.recipe = recipe
        self.crafts_per_min = crafts_per_min
        self.machines = machines
        self.machine_name = machine_name
        self.speed = speed
        self.ingredients: list[tuple[str, float]] = []
        self.results: list[tuple[str, float]] = []
        self.row = 0

    def width(self) -> int:
        """Return the tile width of this recipe node."""
        return pnr.MACHINE_SPACING * self.machines


def plan(
    data: solve.Data,
    demand: dict[str, float],
    belt: str,
    inserter: str,
) -> tuple[list[Node], dict[str, float], dict[str, float], dict[str, float]]:
    """Compute rates and machine counts for the demanded items."""
    allowed = data.allowed_recipes(["nauvis"])
    rates, raw, surplus = solve.solve_rates(data, demand, allowed)

    nodes: list[Node] = []
    for recipe, crafts in sorted(rates.items()):
        recipe_data = data.recipes[recipe]
        machine = data.machine_for(recipe_data)
        if machine is None:
            raise SystemExit(f"no machine holds the categories of {recipe}")
        machine_name, speed = machine
        per_machine = SECONDS_PER_MINUTE * speed / data.craft_seconds(recipe_data)
        node = Node(recipe, crafts, max(1, math.ceil(crafts / per_machine)), machine_name, speed)
        node.ingredients = [
            (solve.name_of(e), solve.amount_of(e)) for e in data.ingredients_of(recipe_data)
        ]
        node.results = [
            (solve.name_of(e), solve.amount_of(e)) for e in data.results_of(recipe_data)
        ]
        nodes.append(node)

    order_rows(nodes)
    return nodes, rates, raw, surplus


def _compute_node_depth(
    node: Node,
    producer: dict[str, Node],
    depth: dict[str, int],
) -> int:
    """Recursively compute the topological depth of one recipe node."""
    if node.recipe in depth:
        return depth[node.recipe]
    depth[node.recipe] = 0
    best = 0
    for item, _ in node.ingredients:
        source = producer.get(item)
        if source is not None and source is not node:
            best = max(best, _compute_node_depth(source, producer, depth) + 1)
    depth[node.recipe] = best
    return best


def order_rows(nodes: list[Node]) -> None:
    """Sort nodes by topological depth so a producer sits above its consumers."""
    producer: dict[str, Node] = {}
    for node in nodes:
        for item, _ in node.results:
            producer[item] = node

    depth: dict[str, int] = {}
    for node in nodes:
        _compute_node_depth(node, producer, depth)
    nodes.sort(key=lambda n: (depth[n.recipe], n.recipe))
    for index, node in enumerate(nodes):
        node.row = index


def assign_lanes(nodes: Sequence[Node]) -> tuple[dict[str, int], list[str]]:
    """Assign one lane per item with the busiest lane nearest the blocks.

    A tap tunnels under every lane between its own and the block, so a widely
    used item belongs on the east side of the bus where nothing is in the way.
    """
    consumers: collections.Counter[str] = collections.Counter()
    items: set[str] = set()
    for node in nodes:
        for item, _ in node.ingredients:
            consumers[item] += 1
            items.add(item)
        for item, _ in node.results:
            items.add(item)
    lanes = sorted(items, key=lambda item: (-consumers.get(item, 0), item))
    return {item: index for index, item in enumerate(lanes)}, lanes


def lane_x(index: int) -> int:
    """Return the X tile coordinate for a bus lane index."""
    return index * LANE_PITCH


class Router:
    """Places belt runs, with an underground pair under every lane in the way."""

    def __init__(
        self,
        entities: list[dict[str, object]],
        add: Callable[..., dict[str, object]],
        belt: str,
        blocked: set[int],
        span: int = DEFAULT_ROUTER_SPAN,
    ) -> None:
        """Initialize the router with belt parameters and blocked columns."""
        self.entities = entities
        self.add = add
        self.belt = belt
        self.blocked = blocked
        self.span = span
        # One tunnel covers as many blocked columns as its span allows.
        self.per_tunnel = max(1, span // LANE_PITCH)
        self.vias = 0

    def _choose_tunnel(
        self,
        x: int,
        crossing: Sequence[int],
        end_x: int,
        step: int,
    ) -> tuple[int, int]:
        """Select the furthest reachable underground exit across blocked columns."""
        for count in range(min(self.per_tunnel, len(crossing)), 0, -1):
            try:
                last = crossing[count - 1]
                return last, self.find_exit(x, last, end_x, step)
            except SystemExit:
                continue
        raise SystemExit(f"no tunnel from {x} over {crossing} to {end_x}")

    def run(
        self,
        start_x: int,
        end_x: int,
        y: int,
        direction: int,
        port_start: bool = False,
        port_end: bool = False,
    ) -> None:
        """Place a belt run from start_x to end_x in the given direction.

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
            crossing = sorted(
                (c for c in self.blocked if (c - x) * step > 0 and (c - end_x) * step <= 0),
                key=lambda c: (c - x) * step,
            )
            if not crossing:
                self.add(self.belt, x + TILE_CENTRE_OFFSET, y + TILE_CENTRE_OFFSET, direction)
                x += step
                continue

            _last, exit_x = self._choose_tunnel(x, crossing, end_x, step)
            self.add(
                "turbo-underground-belt",
                x + TILE_CENTRE_OFFSET,
                y + TILE_CENTRE_OFFSET,
                direction,
                {"type": "input"},
            )
            self.add(
                "turbo-underground-belt",
                exit_x + TILE_CENTRE_OFFSET,
                y + TILE_CENTRE_OFFSET,
                direction,
                {"type": "output"},
            )
            self.vias += 1
            x = exit_x + step

    def find_exit(self, x: int, last: int, end_x: int, step: int) -> int:
        """Find a free tile past the columns to cross within the underground span.

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


class _BaseBuilder:
    """Stateful helper that constructs the entities of a multi-block base."""

    def __init__(
        self,
        nodes: Sequence[Node],
        lane_index: dict[str, int],
        demand: dict[str, float],
        belt: str,
    ) -> None:
        """Initialize builder geometry and routing structures."""
        self.nodes = nodes
        self.lane_index = lane_index
        self.demand = demand
        self.belt = belt
        self.entities: list[dict[str, object]] = []
        self.row_y: dict[str, int] = {}
        self.output_inserters: dict[str, list[dict[str, object]]] = collections.defaultdict(list)
        lane_columns = {lane_x(index) for index in lane_index.values()}
        self.block_x = (len(lane_index) + 1) * LANE_PITCH
        width = max(node.width() for node in nodes)
        self.block_east = self.block_x + width + 2
        self.height = ROW_PITCH * (len(nodes) + 1)
        self.router = Router(self.entities, self.add, belt, lane_columns)

    def add(
        self,
        name: str,
        x: float,
        y: float,
        direction: int | None = None,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Add an entity centered at (x, y) to the base entity list."""
        entity: dict[str, object] = {
            "entity_number": len(self.entities) + 1,
            "name": name,
            "position": {"x": round(x, 2), "y": round(y, 2)},
        }
        if direction is not None:
            entity["direction"] = direction
        if extra:
            entity.update(extra)
        self.entities.append(entity)
        return entity

    def wire(self, number: int, port: int, other: int, other_port: int) -> None:
        """Join one circuit port of two entities in both directions."""
        for source, side, target in ((number, port, other), (other, other_port, number)):
            ent = self.entities[source - 1]
            ports = ent.setdefault("connections", {})
            if isinstance(ports, dict):
                side_dict = ports.setdefault(str(side), {})
                if isinstance(side_dict, dict):
                    red_list = side_dict.setdefault("red", [])
                    if isinstance(red_list, list):
                        red_list.append({"entity_id": target, "circuit_id": 1})

    def place_lanes(self) -> None:
        """Place the south-flowing bus lanes."""
        for index in self.lane_index.values():
            for y in range(0, self.height + 1):
                self.add(
                    self.belt,
                    lane_x(index) + TILE_CENTRE_OFFSET,
                    y + TILE_CENTRE_OFFSET,
                    pnr.SOUTH,
                )

    def _place_single_machine(
        self,
        node: Node,
        centre: float,
        y: int,
        south_count: int,
        has_north: bool,
    ) -> None:
        """Place one machine and its input and output inserters."""
        self.add(
            node.machine_name,
            centre,
            y + MACHINE_ROW + MACHINE_CENTRE_OFFSET,
            None,
            {"recipe": node.recipe},
        )
        if south_count > 0:
            self.add(
                "bulk-inserter",
                centre,
                y + INPUT_INSERTER_ROW + TILE_CENTRE_OFFSET,
                pnr.SOUTH,
            )
        if south_count > 1:
            self.add(
                "long-handed-inserter",
                centre - 1,
                y + INPUT_INSERTER_ROW + TILE_CENTRE_OFFSET,
                pnr.SOUTH,
            )
        self.output_inserters[node.recipe].append(
            self.add(
                "bulk-inserter",
                centre,
                y + OUTPUT_INSERTER_ROW + TILE_CENTRE_OFFSET,
                pnr.SOUTH,
            )
        )
        if has_north:
            self.add(
                "long-handed-inserter",
                centre - 1,
                y + OUTPUT_INSERTER_ROW + TILE_CENTRE_OFFSET,
                pnr.NORTH,
            )

    def _place_node_machines_and_belts(
        self,
        node: Node,
        y: int,
        south: Sequence[tuple[str, float]],
        north: Sequence[tuple[str, float]],
    ) -> None:
        """Place the machines, inserters, and local belts for one recipe row."""
        for x in range(self.block_x, self.block_east + 1):
            self.add(
                self.belt,
                x + TILE_CENTRE_OFFSET,
                y + OUTPUT_ROW + TILE_CENTRE_OFFSET,
                pnr.WEST,
            )
        for offset, _ in enumerate(south):
            start = self.block_x - 1 if offset == 0 else self.block_x
            for x in range(start, self.block_east + 1):
                self.add(
                    self.belt,
                    x + TILE_CENTRE_OFFSET,
                    y + INPUT_ROW + offset + TILE_CENTRE_OFFSET,
                    pnr.EAST,
                )

        for index in range(node.machines):
            centre = self.block_x + MACHINE_CENTRE_OFFSET + pnr.MACHINE_SPACING * index
            self._place_single_machine(node, centre, y, len(south), bool(north))

    def _place_node_taps_and_merge(
        self,
        node: Node,
        y: int,
        south: Sequence[tuple[str, float]],
        north: Sequence[tuple[str, float]],
    ) -> None:
        """Route the bus taps and output merge for one recipe row."""
        for offset, (item, _) in enumerate(south):
            index = self.lane_index.get(item)
            if index is None:
                continue
            tap_row = y + INPUT_ROW + 2 + offset
            x = lane_x(index) + BRIDGE_OFFSET
            self.add(
                "fast-inserter",
                x + TILE_CENTRE_OFFSET,
                tap_row + TILE_CENTRE_OFFSET,
                pnr.WEST,
            )
            target = self.block_x - 1 + offset
            self.router.run(x + 1, target - 1, tap_row, pnr.EAST)
            for row in range(y + INPUT_ROW + offset + 1, tap_row + 1):
                self.add(
                    self.belt,
                    target + TILE_CENTRE_OFFSET,
                    row + TILE_CENTRE_OFFSET,
                    pnr.NORTH,
                )

        for item, _ in north:
            index = self.lane_index.get(item)
            if index is None:
                continue
            tap_row = y + NORTH_ROW
            x = lane_x(index) + BRIDGE_OFFSET
            self.add(
                "fast-inserter",
                x + TILE_CENTRE_OFFSET,
                tap_row + TILE_CENTRE_OFFSET,
                pnr.WEST,
            )
            end = self.block_x + pnr.MACHINE_SPACING * (node.machines - 1)
            self.router.run(x + 1, end, tap_row, pnr.EAST)

        item = node.results[0][0]
        index = self.lane_index.get(item)
        if index is not None:
            x = lane_x(index) + BRIDGE_OFFSET
            self.router.run(self.block_x - 1, x + 1, y + OUTPUT_ROW, pnr.WEST)
            self.add(
                "fast-inserter",
                x + TILE_CENTRE_OFFSET,
                y + OUTPUT_ROW + TILE_CENTRE_OFFSET,
                pnr.EAST,
            )

    def place_blocks(self) -> None:
        """Place all recipe blocks and their bus connections."""
        for node in self.nodes:
            y = ROW_PITCH * (node.row + 1)
            self.row_y[node.recipe] = y
            south = node.ingredients[:ROWS_SOUTH]
            north = node.ingredients[ROWS_SOUTH:]
            self._place_node_machines_and_belts(node, y, south, north)
            self._place_node_taps_and_merge(node, y, south, north)

    def place_clocks(self) -> None:
        """Place rate-limiting clock and gate combinators on demanded outputs."""
        for node in self.nodes:
            want = self.demand.get(node.results[0][0])
            if not want:
                continue
            period = max(2, round(TICKS_PER_MINUTE / want))
            window = min(period, INSERTER_SWING_TICKS)
            y = self.row_y[node.recipe]
            clock = self.add(
                "arithmetic-combinator",
                self.block_x - CLOCK_COMBINATOR_X_OFFSET,
                y + COMBINATOR_Y_OFFSET,
                pnr.NORTH,
                {
                    "control_behavior": {
                        "arithmetic_conditions": {
                            "operation": "+",
                            "first_signal": {"type": "virtual", "name": CLOCK_SIGNAL},
                            "second_constant": 1,
                            "output_signal": {"type": "virtual", "name": CLOCK_SIGNAL},
                        }
                    }
                },
            )
            gate = self.add(
                "arithmetic-combinator",
                self.block_x - GATE_COMBINATOR_X_OFFSET,
                y + COMBINATOR_Y_OFFSET,
                pnr.NORTH,
                {
                    "control_behavior": {
                        "arithmetic_conditions": {
                            "operation": "%",
                            "first_signal": {"type": "virtual", "name": CLOCK_SIGNAL},
                            "second_constant": period,
                            "output_signal": {"type": "virtual", "name": GATE_SIGNAL},
                        }
                    }
                },
            )
            clock_num = int(str(clock["entity_number"]))
            gate_num = int(str(gate["entity_number"]))
            self.wire(clock_num, 1, clock_num, 2)
            self.wire(clock_num, 2, gate_num, 1)
            for ins in self.output_inserters[node.recipe]:
                ins["control_behavior"] = {
                    "circuit_enable_disable": True,
                    "circuit_condition": {
                        "first_signal": {"type": "virtual", "name": GATE_SIGNAL},
                        "comparator": "<",
                        "constant": window,
                    },
                }
                self.wire(gate_num, 2, int(str(ins["entity_number"])), 1)

    def place_rig(self) -> None:
        """Place infinity feeds and output drains on bus lanes."""
        produced = {item for node in self.nodes for item, _ in node.results}
        for item, index in self.lane_index.items():
            if item in produced:
                continue
            self.add(
                "infinity-chest",
                lane_x(index) + BRIDGE_OFFSET + MACHINE_CENTRE_OFFSET,
                TILE_CENTRE_OFFSET,
                None,
                {
                    "infinity_settings": {
                        "remove_unfiltered_items": True,
                        "filters": [
                            {
                                "name": item,
                                "count": INFINITY_FILTER_COUNT,
                                "mode": "at-least",
                                "index": 1,
                            }
                        ],
                    }
                },
            )
            self.add(
                "fast-inserter",
                lane_x(index) + BRIDGE_OFFSET + TILE_CENTRE_OFFSET,
                TILE_CENTRE_OFFSET,
                pnr.EAST,
            )

        for item in self.demand:
            index = self.lane_index.get(item)
            if index is None:
                continue
            self.add(
                "fast-inserter",
                lane_x(index) + BRIDGE_OFFSET + TILE_CENTRE_OFFSET,
                self.height + TILE_CENTRE_OFFSET,
                pnr.WEST,
            )
            self.add(
                "steel-chest",
                lane_x(index) + BRIDGE_OFFSET + MACHINE_CENTRE_OFFSET,
                self.height + TILE_CENTRE_OFFSET,
            )

    def place_power(self) -> None:
        """Place power substations and solar panels clear of recipe blocks."""
        power_rows = (
            [POWER_ROW_PADDING]
            + [self.row_y[node.recipe] for node in self.nodes]
            + [self.height + POWER_ROW_PADDING]
        )
        for y in power_rows:
            for x in SUBSTATION_X_POSITIONS:
                if self._clear_of_block(x, SUBSTATION_WIDTH):
                    self.add("substation", x, y + MACHINE_CENTRE_OFFSET)
            for x in SOLAR_PANEL_X_POSITIONS:
                if self._clear_of_block(x, SOLAR_PANEL_WIDTH):
                    self.add("solar-panel", x, y + MACHINE_CENTRE_OFFSET)

    def _clear_of_block(self, x: float, width: int) -> bool:
        """Return True when a structure of given width at x does not overlap a block."""
        first = int(round(x - width / 2.0))
        return first + width - 1 < self.block_x or first > self.block_east


def build(
    data: solve.Data,
    nodes: Sequence[Node],
    lane_index: dict[str, int],
    demand: dict[str, float],
    belt: str,
    inserter: str,
) -> tuple[list[dict[str, object]], int, int, dict[str, int]]:
    """Build every entity of the base and the rig that feeds and drains it."""
    builder = _BaseBuilder(nodes, lane_index, demand, belt)
    builder.place_lanes()
    builder.place_blocks()
    builder.place_clocks()
    builder.place_rig()
    builder.place_power()
    return builder.entities, builder.block_x, builder.height, {"count": builder.router.vias}


def report(
    data: solve.Data,
    nodes: Sequence[Node],
    entities: Sequence[dict[str, object]],
    footprints: dict[str, tuple[int, int]],
    vias: dict[str, int],
) -> list[str]:
    """Return summary lines for the planned multi-block base."""
    lines: list[str] = []
    for node in nodes:
        in_rate = sum(
            node.crafts_per_min / SECONDS_PER_MINUTE * amount for _, amount in node.ingredients
        )
        out_rate = sum(
            node.crafts_per_min / SECONDS_PER_MINUTE * amount for _, amount in node.results
        )
        lines.append(
            f"  {node.recipe:28} {node.machines:3} machine(s) "
            f"in {in_rate:8.3f}/s out {out_rate:8.3f}/s row {node.row}"
        )
    _, clashes = pnr.tiles_of(entities, footprints)
    lines.append(f"blocks      {len(nodes)}")
    lines.append(f"entities    {len(entities)}")
    lines.append(f"vias        {vias['count']} underground pairs")
    lines.append(f"overlap     {len(clashes)} tiles claimed twice")
    for name, tile in clashes[:MAX_REPORTED_CLASHES]:
        lines.append(f"clash       {name} at {tile}")
    lines.append(f"power       {pnr.check_power(data, entities)} entities outside a supply area")
    return lines


def demand_of(args: argparse.Namespace) -> dict[str, float]:
    """Parse CLI demand specifications into an item-to-rate dictionary."""
    demand: dict[str, float] = {}
    for text in args.demand:
        item, _, rate = text.partition(":")
        demand[item] = float(rate or 1.0)
    return demand


MEASURE_TEMPLATE = """-- Generated by base.py. Builds the base and reads the demanded items back.
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
"""


def write_mod(
    entities: Sequence[dict[str, object]],
    demand: dict[str, float],
    mod_dir: str | os.PathLike[str],
    ticks: int,
) -> None:
    """Write the probe mod that builds the base and measures demanded output."""
    mod = pathlib.Path(mod_dir)
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "info.json").write_text(
        '{"name": "fmgen", "version": "0.1.0", "title": "Factorio generator",\n'
        ' "author": "omp", "factorio_version": "2.1",\n'
        ' "dependencies": ["base >= 2.1", "space-age"]}\n'
    )
    text = (
        MEASURE_TEMPLATE.replace("WARMUPTICKS", str(ticks // WARMUP_DIVISOR))
        .replace("TICKS", str(ticks))
        .replace("SOLAR", str(pnr.RIG_SOLAR_MULTIPLIER))
        .replace("EXPECTED_TABLE", pnr.lua_value(demand))
        .replace("OUTPUT_TABLE", pnr.lua_value(sorted(demand)))
        .replace("BPSTRING", pnr.blueprint_string(entities))
    )
    (mod / "control.lua").write_text(text)


def main() -> None:
    """Parse CLI arguments and run base planning, building, or measurement."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["plan", "build", "measure"])
    parser.add_argument("demand", nargs="+")
    parser.add_argument("--dump", default=solve.DEFAULT_DUMP)
    parser.add_argument("--belt", default="turbo-transport-belt")
    parser.add_argument("--inserter", default="fast-inserter")
    parser.add_argument("--out", default=None)
    parser.add_argument("--mod", default=None)
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    args = parser.parse_args()

    data = solve.Data(args.dump)
    demand = demand_of(args)
    nodes, _rates, _raw, _surplus = plan(data, demand, args.belt, args.inserter)
    lane_index, lanes = assign_lanes(nodes)
    entities, block_x, height, vias = build(
        data, nodes, lane_index, demand, args.belt, args.inserter
    )

    footprints = pnr.footprints_of(data, {str(e["name"]) for e in entities})
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
