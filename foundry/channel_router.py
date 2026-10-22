"""Channel routing primitives and router intersection architecture.

Implements lane allocation for 6-tile street corridors and non-blocking
4-way router intersections using vanilla Factorio entities.
"""

from __future__ import annotations

from typing import Any, TypeAlias

from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    OPPOSITE_DIR,
    get_compatible_underground_belt,
)

EntityDict: TypeAlias = dict[str, Any]

LANE_COUNT = 6
MAX_LANE_INDEX = 5
FORWARD_LANE_LIMIT = 3
TILE_CENTER_OFFSET = 0.5
CROSSING_HALF_SPAN = 3.0


class ChannelLane:
    """A single transport lane within a 6-tile street corridor."""

    def __init__(
        self,
        lane_index: int,
        orientation: str,
        direction: int,
        net_name: str | None = None,
        item_type: str | None = None,
        tier: str = "fast-transport-belt",
    ) -> None:
        if lane_index < 0 or lane_index > MAX_LANE_INDEX:
            raise ValueError(f"lane_index must be between 0 and 5, got {lane_index}")
        if orientation not in ("H", "V"):
            raise ValueError(f"orientation must be 'H' or 'V', got {orientation}")
        self.lane_index = lane_index
        self.orientation = orientation
        self.direction = direction
        self.net_name = net_name
        self.item_type = item_type
        self.tier = tier

    @property
    def is_allocated(self) -> bool:
        """Check whether this lane carries an assigned net."""
        return self.net_name is not None

    @property
    def is_fluid(self) -> bool:
        """Check whether this lane carries fluid pipe."""
        return "pipe" in self.tier

    def assign(
        self,
        net_name: str,
        direction: int,
        item_type: str | None,
        tier: str,
    ) -> None:
        """Assign net metadata and direction to this lane."""
        self.net_name = net_name
        self.direction = direction
        self.item_type = item_type
        self.tier = tier


class StreetChannel:
    """A 6-tile wide transit corridor along a city block boundary."""

    def __init__(
        self,
        channel_id: str,
        orientation: str,
        base_coord: float,
        span_min: float,
        span_max: float,
        default_tier: str = "fast-transport-belt",
    ) -> None:
        self.channel_id = channel_id
        self.orientation = orientation
        self.base_coord = base_coord
        self.span_min = span_min
        self.span_max = span_max
        self.default_tier = default_tier

        # Default directions: lanes 0-2 forward, lanes 3-5 reverse
        if orientation == "H":
            fwd_dir, rev_dir = DIRECTION_EAST, DIRECTION_WEST
        else:
            fwd_dir, rev_dir = DIRECTION_SOUTH, DIRECTION_NORTH

        self.lanes: list[ChannelLane] = [
            ChannelLane(
                i,
                orientation,
                fwd_dir if i < FORWARD_LANE_LIMIT else rev_dir,
                tier=default_tier,
            )
            for i in range(LANE_COUNT)
        ]

    def get_lane_coord(self, lane_index: int) -> float:
        """Return the physical coordinate across the 6-tile corridor for a lane."""
        return self.base_coord + lane_index + TILE_CENTER_OFFSET

    def get_lane(self, lane_index: int) -> ChannelLane:
        """Return lane by index."""
        if lane_index < 0 or lane_index >= len(self.lanes):
            raise IndexError(f"Lane index {lane_index} out of range [0..5]")
        return self.lanes[lane_index]

    @property
    def allocated_lanes(self) -> list[ChannelLane]:
        """Return list of all allocated lanes."""
        return [lane for lane in self.lanes if lane.is_allocated]

    @property
    def unallocated_lanes(self) -> list[ChannelLane]:
        """Return list of all unallocated lanes."""
        return [lane for lane in self.lanes if not lane.is_allocated]

    def lanes_for_direction(self, direction: int) -> list[ChannelLane]:
        """Return all lanes matching specified direction."""
        return [lane for lane in self.lanes if lane.direction == direction]

    def _find_candidate_lane(
        self,
        direction: int,
        preferred_lane: int | None,
    ) -> ChannelLane | None:
        """Locate the first free lane matching preference or direction."""
        if preferred_lane is not None:
            if not (0 <= preferred_lane <= MAX_LANE_INDEX):
                raise ValueError(f"preferred_lane must be 0..5, got {preferred_lane}")
            if not self.lanes[preferred_lane].is_allocated:
                return self.lanes[preferred_lane]

        free_lanes = self.unallocated_lanes
        matching = [lane for lane in free_lanes if lane.direction == direction]
        if matching:
            return matching[0]
        return free_lanes[0] if free_lanes else None

    def allocate_lane(
        self,
        net_name: str,
        direction: int,
        item_type: str | None = None,
        preferred_lane: int | None = None,
        tier: str | None = None,
    ) -> ChannelLane:
        """Allocate an available lane for a net."""
        assigned_tier = tier or self.default_tier

        # Reuse existing lane if net is already routed in this channel in the same direction
        for lane in self.lanes:
            if lane.is_allocated and lane.net_name == net_name and lane.direction == direction:
                return lane

        candidate = self._find_candidate_lane(direction, preferred_lane)
        if candidate is not None:
            candidate.assign(net_name, direction, item_type, assigned_tier)
            return candidate

        raise RuntimeError(f"Street channel {self.channel_id} has no free lanes for net {net_name}")

    def free_lane(self, lane_index: int) -> None:
        """Deallocate a lane."""
        lane = self.get_lane(lane_index)
        lane.net_name = None
        lane.item_type = None
        lane.tier = self.default_tier

    def clear_allocations(self) -> None:
        """Deallocate all lanes in this street channel."""
        for i in range(len(self.lanes)):
            self.free_lane(i)

    def _make_lane_tile_entity(self, lane: ChannelLane, c: int) -> EntityDict:
        """Build one belt or pipe entity at tile index c along lane."""
        fixed_coord = self.get_lane_coord(lane.lane_index)
        tile_center = c + TILE_CENTER_OFFSET
        pos = (
            {"x": tile_center, "y": fixed_coord}
            if self.orientation == "H"
            else {"x": fixed_coord, "y": tile_center}
        )
        if lane.is_fluid:
            return {"name": "pipe", "position": pos}
        return {
            "name": lane.tier,
            "position": pos,
            "direction": lane.direction,
        }

    def emit_straight_belts(
        self,
        start_coord: float | None = None,
        end_coord: float | None = None,
    ) -> list[EntityDict]:
        """Emit continuous surface transport belts along allocated lanes."""
        entities: list[EntityDict] = []
        c_start = int(self.span_min if start_coord is None else start_coord)
        c_end = int(self.span_max if end_coord is None else end_coord)

        for lane in self.lanes:
            if not lane.is_allocated:
                continue
            entities.extend(self._make_lane_tile_entity(lane, c) for c in range(c_start, c_end))

        return entities


def _emit_vertical_lane_span(
    lane: ChannelLane,
    vx: float,
    y_min: float,
    y_max: float,
) -> list[EntityDict]:
    """Emit straight vertical crossing entities for one allocated lane."""
    if lane.is_fluid:
        in_y = (
            y_min - TILE_CENTER_OFFSET
            if lane.direction == DIRECTION_SOUTH
            else y_max + TILE_CENTER_OFFSET
        )
        out_y = (
            y_max + TILE_CENTER_OFFSET
            if lane.direction == DIRECTION_SOUTH
            else y_min - TILE_CENTER_OFFSET
        )
        return [
            {
                "name": "pipe-to-ground",
                "position": {"x": vx, "y": in_y},
                "direction": OPPOSITE_DIR[lane.direction],
            },
            {
                "name": "pipe-to-ground",
                "position": {"x": vx, "y": out_y},
                "direction": lane.direction,
            },
        ]

    return [
        {
            "name": lane.tier,
            "position": {"x": vx, "y": y_min + step + TILE_CENTER_OFFSET},
            "direction": lane.direction,
        }
        for step in range(LANE_COUNT)
    ]


def _emit_horizontal_lane_span(
    lane: ChannelLane,
    hy: float,
    x_min: float,
    x_max: float,
) -> list[EntityDict]:
    """Emit straight horizontal underground crossing entities for one allocated lane."""
    in_x = (
        x_min - TILE_CENTER_OFFSET
        if lane.direction == DIRECTION_EAST
        else x_max + TILE_CENTER_OFFSET
    )
    out_x = (
        x_max + TILE_CENTER_OFFSET
        if lane.direction == DIRECTION_EAST
        else x_min - TILE_CENTER_OFFSET
    )
    if lane.is_fluid:
        return [
            {
                "name": "pipe-to-ground",
                "position": {"x": in_x, "y": hy},
                "direction": OPPOSITE_DIR[lane.direction],
            },
            {
                "name": "pipe-to-ground",
                "position": {"x": out_x, "y": hy},
                "direction": lane.direction,
            },
        ]

    crossing_gap = round(x_max - x_min)
    candidate_ug = lane.tier.replace("transport-belt", "underground-belt")
    ug_tier = get_compatible_underground_belt(min_span=crossing_gap, preferred_name=candidate_ug)
    return [
        {
            "name": ug_tier,
            "type": "input",
            "position": {"x": in_x, "y": hy},
            "direction": lane.direction,
        },
        {
            "name": ug_tier,
            "type": "output",
            "position": {"x": out_x, "y": hy},
            "direction": lane.direction,
        },
    ]


def _emit_linear_belt_run(
    tier: str,
    direction: int,
    fixed_coord: float,
    start_val: float,
    limit_val: float,
    step_val: float,
    horizontal: bool,
) -> list[EntityDict]:
    """Emit a 1D run of belt tiles along X or Y until reaching limit_val."""
    result: list[EntityDict] = []
    cur = start_val
    while (cur < limit_val) if step_val > 0 else (cur > limit_val):
        pos = {"x": cur, "y": fixed_coord} if horizontal else {"x": fixed_coord, "y": cur}
        result.append({"name": tier, "position": pos, "direction": direction})
        cur += step_val
    return result


class RouterIntersection:
    """A 4-way crossroad junction between horizontal and vertical street channels.

    Operates as a non-blocking switchbox:
    - Vertical (M2) belts traverse the 6x6 crossing zone on the surface.
    - Horizontal (M1) belts dive underground across the 6-tile gap.
    - Turning connections peel off into adjacent channels cleanly.
    - Lane switches insert 2x2 splitters on channel approaches.
    """

    def __init__(
        self,
        center_x: float,
        center_y: float,
        h_channel: StreetChannel,
        v_channel: StreetChannel,
    ) -> None:
        self.center_x = center_x
        self.center_y = center_y
        self.h_channel = h_channel
        self.v_channel = v_channel

        # Turn configurations: (from_orientation, from_lane, to_orientation, to_lane)
        self.turns: list[tuple[str, int, str, int]] = []
        # Lane switches: (orientation, lane_a, lane_b, filter_item, approach_offset)
        self.switches: list[tuple[str, int, int, str | None, int]] = []

    def clear(self) -> None:
        """Clear all turn configurations and lane switches."""
        self.turns.clear()
        self.switches.clear()

    def add_turn(
        self,
        from_orientation: str,
        from_lane: int,
        to_orientation: str,
        to_lane: int,
    ) -> None:
        """Add a 90-degree turning connection between intersecting channels."""
        if from_orientation not in ("H", "V") or to_orientation not in ("H", "V"):
            raise ValueError("Orientations must be 'H' or 'V'")
        if from_orientation == to_orientation:
            raise ValueError("Turns must connect different orientations (H to V or V to H)")
        self.turns.append((from_orientation, from_lane, to_orientation, to_lane))

    def add_switch(
        self,
        orientation: str,
        lane_a: int,
        lane_b: int,
        filter_item: str | None = None,
        approach_offset: int = 1,
    ) -> None:
        """Add a 2x2 splitter lane switch on an intersection approach."""
        if abs(lane_a - lane_b) != 1:
            raise ValueError(f"Switches must bridge adjacent lanes, got {lane_a} and {lane_b}")
        self.switches.append(
            (
                orientation,
                lane_a,
                lane_b,
                filter_item,
                approach_offset,
            )
        )

    def _emit_h_to_v_turn(
        self,
        from_l: int,
        to_l: int,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> list[EntityDict]:
        """Emit belt tiles for a horizontal-to-vertical 90-degree turn."""
        h_lane = self.h_channel.lanes[from_l]
        v_lane = self.v_channel.lanes[to_l]
        tx = self.v_channel.get_lane_coord(to_l)
        ty = self.h_channel.get_lane_coord(from_l)
        entities: list[EntityDict] = []

        if h_lane.direction == DIRECTION_EAST:
            entities.extend(
                _emit_linear_belt_run(
                    h_lane.tier, DIRECTION_EAST, ty, x_min + TILE_CENTER_OFFSET, tx, 1.0, True
                )
            )
        elif h_lane.direction == DIRECTION_WEST:
            entities.extend(
                _emit_linear_belt_run(
                    h_lane.tier, DIRECTION_WEST, ty, x_max - TILE_CENTER_OFFSET, tx, -1.0, True
                )
            )

        entities.append(
            {"name": v_lane.tier, "position": {"x": tx, "y": ty}, "direction": v_lane.direction}
        )

        if v_lane.direction == DIRECTION_SOUTH:
            entities.extend(
                _emit_linear_belt_run(v_lane.tier, DIRECTION_SOUTH, tx, ty + 1.0, y_max, 1.0, False)
            )
        elif v_lane.direction == DIRECTION_NORTH:
            entities.extend(
                _emit_linear_belt_run(
                    v_lane.tier, DIRECTION_NORTH, tx, ty - 1.0, y_min, -1.0, False
                )
            )
        return entities

    def _emit_v_to_h_turn(
        self,
        from_l: int,
        to_l: int,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> list[EntityDict]:
        """Emit belt tiles for a vertical-to-horizontal 90-degree turn."""
        v_lane = self.v_channel.lanes[from_l]
        h_lane = self.h_channel.lanes[to_l]
        tx = self.v_channel.get_lane_coord(from_l)
        ty = self.h_channel.get_lane_coord(to_l)
        entities: list[EntityDict] = []

        if v_lane.direction == DIRECTION_SOUTH:
            entities.extend(
                _emit_linear_belt_run(
                    v_lane.tier, DIRECTION_SOUTH, tx, y_min + TILE_CENTER_OFFSET, ty, 1.0, False
                )
            )
        elif v_lane.direction == DIRECTION_NORTH:
            entities.extend(
                _emit_linear_belt_run(
                    v_lane.tier, DIRECTION_NORTH, tx, y_max - TILE_CENTER_OFFSET, ty, -1.0, False
                )
            )

        entities.append(
            {"name": h_lane.tier, "position": {"x": tx, "y": ty}, "direction": h_lane.direction}
        )

        if h_lane.direction == DIRECTION_EAST:
            entities.extend(
                _emit_linear_belt_run(h_lane.tier, DIRECTION_EAST, ty, tx + 1.0, x_max, 1.0, True)
            )
        elif h_lane.direction == DIRECTION_WEST:
            entities.extend(
                _emit_linear_belt_run(h_lane.tier, DIRECTION_WEST, ty, tx - 1.0, x_min, -1.0, True)
            )
        return entities

    def _emit_switch_entity(
        self,
        orient: str,
        la: int,
        lb: int,
        filter_item: str | None,
        approach_offset: int,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> EntityDict:
        """Emit one 2x2 splitter switch entity on a channel approach."""
        min_lane = min(la, lb)
        if orient == "H":
            y_center = self.h_channel.base_coord + min_lane + 1.0
            lane = self.h_channel.lanes[la]
            x_pos = (
                (x_min - TILE_CENTER_OFFSET - approach_offset)
                if lane.direction == DIRECTION_EAST
                else (x_max + TILE_CENTER_OFFSET + approach_offset)
            )
            pos = {"x": x_pos, "y": y_center}
        else:
            x_center = self.v_channel.base_coord + min_lane + 1.0
            lane = self.v_channel.lanes[la]
            y_pos = (
                (y_min - TILE_CENTER_OFFSET - approach_offset)
                if lane.direction == DIRECTION_SOUTH
                else (y_max + TILE_CENTER_OFFSET + approach_offset)
            )
            pos = {"x": x_center, "y": y_pos}

        splitter_ent: EntityDict = {
            "name": lane.tier.replace("transport-belt", "splitter"),
            "position": pos,
            "direction": lane.direction,
        }
        if filter_item:
            splitter_ent["filter"] = filter_item
        return splitter_ent

    def _turning_lane_sets(self) -> tuple[set[int], set[int]]:
        """Collect horizontal and vertical lane indices involved in turns."""
        h_turning = {from_l for (o, from_l, _, _) in self.turns if o == "H"} | {
            to_l for (_, _, o, to_l) in self.turns if o == "H"
        }
        v_turning = {from_l for (o, from_l, _, _) in self.turns if o == "V"} | {
            to_l for (_, _, o, to_l) in self.turns if o == "V"
        }
        return h_turning, v_turning

    def _emit_turn_entities(
        self,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> list[EntityDict]:
        """Emit all turning belt segments inside the crossing zone."""
        result: list[EntityDict] = []
        for from_o, from_l, to_o, to_l in self.turns:
            if from_o == "H" and to_o == "V":
                result.extend(self._emit_h_to_v_turn(from_l, to_l, x_min, x_max, y_min, y_max))
            elif from_o == "V" and to_o == "H":
                result.extend(self._emit_v_to_h_turn(from_l, to_l, x_min, x_max, y_min, y_max))
        return result

    def emit_intersection_entities(self) -> list[EntityDict]:
        """Emit all physical entities for the 4-way crossroad."""
        entities: list[EntityDict] = []

        x_min = self.center_x - CROSSING_HALF_SPAN
        x_max = self.center_x + CROSSING_HALF_SPAN
        y_min = self.center_y - CROSSING_HALF_SPAN
        y_max = self.center_y + CROSSING_HALF_SPAN

        h_turning_lanes, v_turning_lanes = self._turning_lane_sets()

        for lane in self.v_channel.allocated_lanes:
            if lane.lane_index not in v_turning_lanes:
                vx = self.v_channel.get_lane_coord(lane.lane_index)
                entities.extend(_emit_vertical_lane_span(lane, vx, y_min, y_max))

        for lane in self.h_channel.allocated_lanes:
            if lane.lane_index not in h_turning_lanes:
                hy = self.h_channel.get_lane_coord(lane.lane_index)
                entities.extend(_emit_horizontal_lane_span(lane, hy, x_min, x_max))

        entities.extend(self._emit_turn_entities(x_min, x_max, y_min, y_max))

        for orient, la, lb, filter_item, approach_offset in self.switches:
            entities.append(
                self._emit_switch_entity(
                    orient, la, lb, filter_item, approach_offset, x_min, x_max, y_min, y_max
                )
            )

        return entities
