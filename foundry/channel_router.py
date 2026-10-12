"""Channel routing primitives and router intersection architecture.

Implements lane allocation for 6-tile street corridors and non-blocking
4-way router intersections using vanilla Factorio entities.
"""

from __future__ import annotations

from typing import Any

from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    OPPOSITE_DIR,
    get_compatible_underground_belt,
)


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
        if lane_index < 0 or lane_index > 5:
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
            ChannelLane(i, orientation, fwd_dir if i < 3 else rev_dir, tier=default_tier)
            for i in range(6)
        ]

    def get_lane_coord(self, lane_index: int) -> float:
        """Return the physical coordinate across the 6-tile corridor for a lane."""
        return self.base_coord + lane_index + 0.5

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
        if preferred_lane is not None:
            if preferred_lane < 0 or preferred_lane > 5:
                raise ValueError(f"preferred_lane must be 0..5, got {preferred_lane}")
            lane = self.lanes[preferred_lane]
            if not lane.is_allocated:
                lane.net_name = net_name
                lane.direction = direction
                lane.item_type = item_type
                lane.tier = assigned_tier
                return lane

        # Find first matching unallocated lane with compatible direction
        for lane in self.lanes:
            if not lane.is_allocated and lane.direction == direction:
                lane.net_name = net_name
                lane.item_type = item_type
                lane.tier = assigned_tier
                return lane

        # Otherwise find any unallocated lane
        for lane in self.lanes:
            if not lane.is_allocated:
                lane.net_name = net_name
                lane.direction = direction
                lane.item_type = item_type
                lane.tier = assigned_tier
                return lane

        raise RuntimeError(f"Street channel {self.channel_id} has no free lanes for net {net_name}")

    def free_lane(self, lane_index: int) -> None:
        """Deallocate a lane."""
        lane = self.get_lane(lane_index)
        lane.net_name = None
        lane.item_type = None
        lane.tier = self.default_tier

    def emit_straight_belts(
        self,
        start_coord: float | None = None,
        end_coord: float | None = None,
    ) -> list[dict[str, Any]]:
        """Emit continuous surface transport belts along allocated lanes."""
        entities: list[dict[str, Any]] = []
        c_start = int(self.span_min if start_coord is None else start_coord)
        c_end = int(self.span_max if end_coord is None else end_coord)

        for lane in self.lanes:
            if not lane.is_allocated:
                continue

            fixed_coord = self.get_lane_coord(lane.lane_index)
            is_pipe = lane.is_fluid

            for c in range(c_start, c_end):
                tile_center = c + 0.5
                if self.orientation == "H":
                    pos = {"x": tile_center, "y": fixed_coord}
                else:
                    pos = {"x": fixed_coord, "y": tile_center}

                if is_pipe:
                    entities.append({"name": "pipe", "position": pos})
                else:
                    entities.append(
                        {
                            "name": lane.tier,
                            "position": pos,
                            "direction": lane.direction,
                        }
                    )

        return entities


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

    def emit_intersection_entities(self) -> list[dict[str, Any]]:
        """Emit all physical entities for the 4-way crossroad."""
        entities: list[dict[str, Any]] = []

        # Crossing bounds: [center_x - 3, center_x + 3] x [center_y - 3, center_y + 3]
        x_min = self.center_x - 3.0
        x_max = self.center_x + 3.0
        y_min = self.center_y - 3.0
        y_max = self.center_y + 3.0

        # Set of lanes participating in 90-degree turns
        h_turning_lanes = {from_l for (o, from_l, _, _) in self.turns if o == "H"} | {
            to_l for (_, _, o, to_l) in self.turns if o == "H"
        }
        v_turning_lanes = {from_l for (o, from_l, _, _) in self.turns if o == "V"} | {
            to_l for (_, _, o, to_l) in self.turns if o == "V"
        }

        # 1. Straight Vertical (M2) traffic across the 6x6 crossing zone
        for lane in self.v_channel.lanes:
            if not lane.is_allocated or lane.lane_index in v_turning_lanes:
                continue

            vx = self.v_channel.get_lane_coord(lane.lane_index)
            is_pipe = lane.is_fluid

            if is_pipe:
                # Vertical pipe uses pipe-to-ground across crossing
                in_y = y_min - 0.5 if lane.direction == DIRECTION_SOUTH else y_max + 0.5
                out_y = y_max + 0.5 if lane.direction == DIRECTION_SOUTH else y_min - 0.5
                entities.append(
                    {
                        "name": "pipe-to-ground",
                        "position": {"x": vx, "y": in_y},
                        "direction": OPPOSITE_DIR[lane.direction],
                    }
                )
                entities.append(
                    {
                        "name": "pipe-to-ground",
                        "position": {"x": vx, "y": out_y},
                        "direction": lane.direction,
                    }
                )
            else:
                for step in range(6):
                    cur_y = y_min + step + 0.5
                    entities.append(
                        {
                            "name": lane.tier,
                            "position": {"x": vx, "y": cur_y},
                            "direction": lane.direction,
                        }
                    )

        # 2. Straight Horizontal (M1) traffic dives underground across vertical traffic
        for lane in self.h_channel.lanes:
            if not lane.is_allocated or lane.lane_index in h_turning_lanes:
                continue

            hy = self.h_channel.get_lane_coord(lane.lane_index)
            is_pipe = lane.is_fluid

            if is_pipe:
                # Pipe-to-ground spanning the 6-tile street crossing
                in_x = x_min - 0.5 if lane.direction == DIRECTION_EAST else x_max + 0.5
                out_x = x_max + 0.5 if lane.direction == DIRECTION_EAST else x_min - 0.5
                entities.append(
                    {
                        "name": "pipe-to-ground",
                        "position": {"x": in_x, "y": hy},
                        "direction": OPPOSITE_DIR[lane.direction],
                    }
                )
                entities.append(
                    {
                        "name": "pipe-to-ground",
                        "position": {"x": out_x, "y": hy},
                        "direction": lane.direction,
                    }
                )
            else:
                # Underground belt pair spanning the street crossing
                crossing_gap = round(x_max - x_min)
                candidate_ug = lane.tier.replace("transport-belt", "underground-belt")
                ug_tier = get_compatible_underground_belt(
                    min_span=crossing_gap, preferred_name=candidate_ug
                )

                in_x = x_min - 0.5 if lane.direction == DIRECTION_EAST else x_max + 0.5
                out_x = x_max + 0.5 if lane.direction == DIRECTION_EAST else x_min - 0.5
                entities.append(
                    {
                        "name": ug_tier,
                        "type": "input",
                        "position": {"x": in_x, "y": hy},
                        "direction": lane.direction,
                    }
                )
                entities.append(
                    {
                        "name": ug_tier,
                        "type": "output",
                        "position": {"x": out_x, "y": hy},
                        "direction": lane.direction,
                    }
                )

        # 3. Turning connections
        for from_o, from_l, to_o, to_l in self.turns:
            if from_o == "H" and to_o == "V":
                h_lane = self.h_channel.lanes[from_l]
                v_lane = self.v_channel.lanes[to_l]
                tx = self.v_channel.get_lane_coord(to_l)
                ty = self.h_channel.get_lane_coord(from_l)

                # Horizontal approach segment inside crossing zone
                if h_lane.direction == DIRECTION_EAST:
                    x_cur = x_min + 0.5
                    while x_cur < tx:
                        entities.append(
                            {
                                "name": h_lane.tier,
                                "position": {"x": x_cur, "y": ty},
                                "direction": DIRECTION_EAST,
                            }
                        )
                        x_cur += 1.0
                elif h_lane.direction == DIRECTION_WEST:
                    x_cur = x_max - 0.5
                    while x_cur > tx:
                        entities.append(
                            {
                                "name": h_lane.tier,
                                "position": {"x": x_cur, "y": ty},
                                "direction": DIRECTION_WEST,
                            }
                        )
                        x_cur -= 1.0

                # Corner tile at (tx, ty)
                entities.append(
                    {
                        "name": v_lane.tier,
                        "position": {"x": tx, "y": ty},
                        "direction": v_lane.direction,
                    }
                )

                # Vertical exit segment inside crossing zone
                if v_lane.direction == DIRECTION_SOUTH:
                    y_cur = ty + 1.0
                    while y_cur < y_max:
                        entities.append(
                            {
                                "name": v_lane.tier,
                                "position": {"x": tx, "y": y_cur},
                                "direction": DIRECTION_SOUTH,
                            }
                        )
                        y_cur += 1.0
                elif v_lane.direction == DIRECTION_NORTH:
                    y_cur = ty - 1.0
                    while y_cur > y_min:
                        entities.append(
                            {
                                "name": v_lane.tier,
                                "position": {"x": tx, "y": y_cur},
                                "direction": DIRECTION_NORTH,
                            }
                        )
                        y_cur -= 1.0

            elif from_o == "V" and to_o == "H":
                v_lane = self.v_channel.lanes[from_l]
                h_lane = self.h_channel.lanes[to_l]
                tx = self.v_channel.get_lane_coord(from_l)
                ty = self.h_channel.get_lane_coord(to_l)

                # Vertical approach segment inside crossing zone
                if v_lane.direction == DIRECTION_SOUTH:
                    y_cur = y_min + 0.5
                    while y_cur < ty:
                        entities.append(
                            {
                                "name": v_lane.tier,
                                "position": {"x": tx, "y": y_cur},
                                "direction": DIRECTION_SOUTH,
                            }
                        )
                        y_cur += 1.0
                elif v_lane.direction == DIRECTION_NORTH:
                    y_cur = y_max - 0.5
                    while y_cur > ty:
                        entities.append(
                            {
                                "name": v_lane.tier,
                                "position": {"x": tx, "y": y_cur},
                                "direction": DIRECTION_NORTH,
                            }
                        )
                        y_cur -= 1.0

                # Corner tile at (tx, ty)
                entities.append(
                    {
                        "name": h_lane.tier,
                        "position": {"x": tx, "y": ty},
                        "direction": h_lane.direction,
                    }
                )

                # Horizontal exit segment inside crossing zone
                if h_lane.direction == DIRECTION_EAST:
                    x_cur = tx + 1.0
                    while x_cur < x_max:
                        entities.append(
                            {
                                "name": h_lane.tier,
                                "position": {"x": x_cur, "y": ty},
                                "direction": DIRECTION_EAST,
                            }
                        )
                        x_cur += 1.0
                elif h_lane.direction == DIRECTION_WEST:
                    x_cur = tx - 1.0
                    while x_cur > x_min:
                        entities.append(
                            {
                                "name": h_lane.tier,
                                "position": {"x": x_cur, "y": ty},
                                "direction": DIRECTION_WEST,
                            }
                        )
                        x_cur -= 1.0

        # 4. Lane switches (2x2 splitters on approaches)
        for orient, la, lb, filter_item, approach_offset in self.switches:
            if orient == "H":
                min_lane = min(la, lb)
                y_center = self.h_channel.base_coord + min_lane + 1.0
                lane = self.h_channel.lanes[la]
                # Splitter placed on approach before the underground entrance
                x_pos = (
                    (x_min - 0.5 - approach_offset)
                    if lane.direction == DIRECTION_EAST
                    else (x_max + 0.5 + approach_offset)
                )
                splitter_name = lane.tier.replace("transport-belt", "splitter")
                splitter_ent: dict[str, Any] = {
                    "name": splitter_name,
                    "position": {"x": x_pos, "y": y_center},
                    "direction": lane.direction,
                }
                if filter_item:
                    splitter_ent["filter"] = filter_item
                entities.append(splitter_ent)
            else:
                min_lane = min(la, lb)
                x_center = self.v_channel.base_coord + min_lane + 1.0
                lane = self.v_channel.lanes[la]
                # Splitter placed on approach before the surface entrance
                y_pos = (
                    (y_min - 0.5 - approach_offset)
                    if lane.direction == DIRECTION_SOUTH
                    else (y_max + 0.5 + approach_offset)
                )
                splitter_name = lane.tier.replace("transport-belt", "splitter")
                splitter_ent = {
                    "name": splitter_name,
                    "position": {"x": x_center, "y": y_pos},
                    "direction": lane.direction,
                }
                if filter_item:
                    splitter_ent["filter"] = filter_item
                entities.append(splitter_ent)

        return entities
