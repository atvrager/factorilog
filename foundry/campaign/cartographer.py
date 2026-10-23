"""Map cartographer and technology-dependent obstacle classifier.

Maintains chunk visibility, water boundaries, resource patch tile coordinates,
and terrain obstacles (trees, rocks, cliffs) from live map telemetry, scenario
exports, or procedural map dumps. Partitions terrain obstacles into clearable
and hard keepout sets based on researched technologies and prototype properties.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias

from foundry import solve
from foundry.entity_helpers import (
    DIRECTION_NORTH,
    entity_size,
    register_prototype_boxes,
)
from foundry.patch_router import MapSurfaceSpec, TileCoord

EntityDict: TypeAlias = dict[str, Any]
ChunkCoord: TypeAlias = tuple[int, int]
BoundingBox: TypeAlias = tuple[tuple[float, float], tuple[float, float]]

CHUNK_SIZE: int = 32
DEFAULT_MARGIN_TILES: int = 4
BOX_EPSILON: float = 1e-6
HALF_DIVISOR: float = 2.0
DEFAULT_CLIFF_HALF_EXTENT: float = 1.9
DEFAULT_TREE_HALF_EXTENT: float = 0.4
DEFAULT_ROCK_HALF_EXTENT: float = 0.9

DEFAULT_WATER_TILE_NAMES: frozenset[str] = frozenset(
    {
        "water",
        "deepwater",
        "water-green",
        "deepwater-green",
        "water-shallow",
        "water-mud",
    }
)


@dataclass(frozen=True, slots=True)
class TerrainObstacle:
    """One terrain obstacle entity on the map surface with its tile footprint."""

    name: str
    entity_type: str
    x: float
    y: float
    occupied_tiles: frozenset[TileCoord]
    unit_number: int | None = None
    cliff_orientation: str | None = None


@dataclass(slots=True)
class ObstacleClassification:
    """Technology-dependent partition of terrain obstacles into hard and clearable sets."""

    hard_obstacles: list[TerrainObstacle]
    clearable_obstacles: list[TerrainObstacle]
    hard_tiles: set[TileCoord]
    clearable_tiles: set[TileCoord]
    cliff_demolition_unlocked: bool
    landfill_unlocked: bool


def _tiles_for_box(
    cx: float,
    cy: float,
    box: BoundingBox,
) -> frozenset[TileCoord]:
    """Return the set of integer (x, y) tiles overlapped by box centered at (cx, cy)."""
    (dx0, dy0), (dx1, dy1) = box
    x0 = math.floor(cx + dx0 + BOX_EPSILON)
    x1 = math.ceil(cx + dx1 - BOX_EPSILON)
    y0 = math.floor(cy + dy0 + BOX_EPSILON)
    y1 = math.ceil(cy + dy1 - BOX_EPSILON)
    if x1 <= x0:
        x1 = x0 + 1
    if y1 <= y0:
        y1 = y0 + 1
    return frozenset((tx, ty) for tx in range(x0, x1) for ty in range(y0, y1))


def _default_box_for_type(entity_type: str) -> BoundingBox:
    """Return a default relative collision box for an obstacle entity type."""
    if entity_type == "cliff":
        ext = DEFAULT_CLIFF_HALF_EXTENT
    elif entity_type == "tree":
        ext = DEFAULT_TREE_HALF_EXTENT
    else:
        ext = DEFAULT_ROCK_HALF_EXTENT
    return ((-ext, -ext), (ext, ext))


def _lookup_prototype_collision_box(
    name: str,
    entity_type: str,
    data: solve.Data | None,
) -> BoundingBox:
    """Look up the collision_box for (name, entity_type) from prototype data."""
    if data is not None:
        raw = data.prototypes
        for category in (entity_type, "cliff", "tree", "simple-entity"):
            cat_map = raw.get(category)
            if isinstance(cat_map, Mapping) and name in cat_map:
                proto = cat_map[name]
                if isinstance(proto, Mapping):
                    cbox = proto.get("collision_box")
                    if isinstance(cbox, Sequence) and len(cbox) >= 2:
                        p0, p1 = cbox[0], cbox[1]
                        if (
                            isinstance(p0, Sequence)
                            and isinstance(p1, Sequence)
                            and len(p0) >= 2
                            and len(p1) >= 2
                        ):
                            return (
                                (float(p0[0]), float(p0[1])),
                                (float(p1[0]), float(p1[1])),
                            )
    return _default_box_for_type(entity_type)


def discover_water_tile_names(data: solve.Data | None) -> set[str]:
    """Discover water tile prototype names dynamically from prototype data."""
    names = set(DEFAULT_WATER_TILE_NAMES)
    if data is None:
        return names
    tile_map = data.prototypes.get("tile", {})
    if not isinstance(tile_map, Mapping):
        return names
    for t_name, t_proto in tile_map.items():
        if not isinstance(t_proto, Mapping):
            continue
        mask = t_proto.get("collision_mask")
        layers = mask.get("layers", {}) if isinstance(mask, Mapping) else {}
        if (
            isinstance(layers, Mapping) and "water_tile" in layers and "ground_tile" not in layers
        ) or "water" in str(t_name):
            names.add(str(t_name))
    return names


def _discover_cliff_explosive_capsules(data: solve.Data) -> set[str]:
    """Discover capsule item names that destroy cliffs from prototype data."""
    capsules: set[str] = set()
    raw = data.prototypes
    for cat_name in ("capsule", "item"):
        cat_map = raw.get(cat_name, {})
        if not isinstance(cat_map, Mapping):
            continue
        for item_name, proto in cat_map.items():
            if not isinstance(proto, Mapping):
                continue
            cap_act = proto.get("capsule_action")
            if isinstance(cap_act, Mapping) and cap_act.get("type") == "destroy-cliffs":
                capsules.add(str(item_name))
    for cliff_proto in raw.get("cliff", {}).values():
        if isinstance(cliff_proto, Mapping):
            explosive_item = cliff_proto.get("cliff_explosive")
            if isinstance(explosive_item, str) and explosive_item:
                capsules.add(explosive_item)
    if not capsules:
        for r_name in data.recipes:
            if "cliff" in r_name:
                capsules.add(r_name)
    return capsules


def _discover_landfill_items(data: solve.Data) -> set[str]:
    """Discover item names that place landfill tiles over water from prototype data."""
    items: set[str] = set()
    item_map = data.prototypes.get("item", {})
    if isinstance(item_map, Mapping):
        for item_name, proto in item_map.items():
            if not isinstance(proto, Mapping):
                continue
            place_tile = proto.get("place_as_tile")
            if isinstance(place_tile, Mapping):
                cond = place_tile.get("condition")
                layers = cond.get("layers", {}) if isinstance(cond, Mapping) else {}
                if isinstance(layers, Mapping) and "ground_tile" in layers:
                    items.add(str(item_name))
    if not items and "landfill" in data.recipes:
        items.add("landfill")
    return items


def _unlocked_recipes_for_tech_set(
    data: solve.Data,
    researched_techs: set[str],
) -> set[str]:
    """Return all recipes unlocked at game start or by any technology in researched_techs."""
    expanded_techs: set[str] = set()
    for t_name in researched_techs:
        if t_name in data.techs:
            expanded_techs.update(data.tech_order(t_name))
        else:
            expanded_techs.add(t_name)

    unlocked: set[str] = {
        r_name
        for r_name, r_proto in data.recipes.items()
        if r_proto.get("enabled", True) is not False and r_name not in data.unlocked_by
    }
    for t_name in expanded_techs:
        t_proto = data.techs.get(t_name, {})
        for eff in t_proto.get("effects", []):
            if eff.get("type") == "unlock-recipe" and "recipe" in eff:
                unlocked.add(str(eff["recipe"]))
    return unlocked


def is_cliff_demolition_unlocked(
    data: solve.Data,
    researched_techs: set[str],
) -> bool:
    """Return True if researched_techs unlock a cliff-destroying capsule recipe."""
    capsules = _discover_cliff_explosive_capsules(data)
    if not capsules:
        return False
    unlocked = _unlocked_recipes_for_tech_set(data, researched_techs)
    for cap_item in capsules:
        if cap_item in unlocked:
            return True
        for prod_recipe in data.producers.get(cap_item, []):
            if prod_recipe in unlocked:
                return True
    return False


def is_landfill_unlocked(
    data: solve.Data,
    researched_techs: set[str],
) -> bool:
    """Return True if researched_techs unlock a water-filling tile recipe."""
    landfill_items = _discover_landfill_items(data)
    if not landfill_items:
        return False
    unlocked = _unlocked_recipes_for_tech_set(data, researched_techs)
    for lf_item in landfill_items:
        if lf_item in unlocked:
            return True
        for prod_recipe in data.producers.get(lf_item, []):
            if prod_recipe in unlocked:
                return True
    return False


def _footprint_for_entity_dict(
    ent: Mapping[str, Any],
    data: solve.Data | None = None,
) -> set[TileCoord]:
    """Compute the integer tile footprint for a placed or planned blueprint entity."""
    if data is not None:
        register_prototype_boxes(data.prototypes)
    name = str(ent.get("name", ""))
    direction = int(ent.get("direction", DIRECTION_NORTH))
    pos = ent.get("position", {})
    if isinstance(pos, Mapping):
        cx = float(pos.get("x", 0.0))
        cy = float(pos.get("y", 0.0))
    elif isinstance(pos, Sequence) and len(pos) >= 2:
        cx = float(pos[0])
        cy = float(pos[1])
    else:
        cx, cy = 0.0, 0.0
    w_f, h_f = entity_size(name, direction)
    w, h = int(w_f), int(h_f)
    x0 = math.floor(cx - w / HALF_DIVISOR + BOX_EPSILON)
    y0 = math.floor(cy - h / HALF_DIVISOR + BOX_EPSILON)
    return {(x0 + dx, y0 + dy) for dx in range(w) for dy in range(h)}


@dataclass(slots=True)
class MapCartographer:
    """Tracks charted chunks, water boundaries, resource patches, and terrain obstacles."""

    data: solve.Data | None = None
    charted_chunks: set[ChunkCoord] = field(default_factory=set)
    water_tiles: set[TileCoord] = field(default_factory=set)
    resource_patches: dict[str, set[TileCoord]] = field(default_factory=dict)
    resource_amounts: dict[TileCoord, tuple[str, int]] = field(default_factory=dict)
    obstacles: list[TerrainObstacle] = field(default_factory=list)
    _obstacle_keys: set[tuple[str, float, float]] = field(default_factory=set)

    def add_charted_chunk(self, cx: int, cy: int) -> None:
        """Record chunk (cx, cy) as charted."""
        self.charted_chunks.add((int(cx), int(cy)))

    def add_water_tile(self, tx: int, ty: int) -> None:
        """Record tile (tx, ty) as water and mark its containing chunk as charted."""
        coord = (int(tx), int(ty))
        self.water_tiles.add(coord)
        self.charted_chunks.add((coord[0] // CHUNK_SIZE, coord[1] // CHUNK_SIZE))

    def add_resource_tile(
        self,
        commodity: str,
        x: float,
        y: float,
        amount: int = 1000,
    ) -> None:
        """Record a resource entity at (x, y) into its integer tile coordinate."""
        tx, ty = math.floor(float(x)), math.floor(float(y))
        coord = (tx, ty)
        self.resource_patches.setdefault(str(commodity), set()).add(coord)
        self.resource_amounts[coord] = (str(commodity), int(amount))
        self.charted_chunks.add((tx // CHUNK_SIZE, ty // CHUNK_SIZE))

    def add_obstacle(
        self,
        name: str,
        entity_type: str,
        x: float,
        y: float,
        collision_box: BoundingBox | None = None,
        unit_number: int | None = None,
        cliff_orientation: str | None = None,
    ) -> TerrainObstacle:
        """Register a tree, rock, or cliff obstacle and compute its tile footprint."""
        fx, fy = round(float(x), 3), round(float(y), 3)
        box = (
            collision_box
            if collision_box is not None
            else _lookup_prototype_collision_box(name, entity_type, self.data)
        )
        tiles = _tiles_for_box(fx, fy, box)
        obs = TerrainObstacle(
            name=str(name),
            entity_type=str(entity_type),
            x=fx,
            y=fy,
            occupied_tiles=tiles,
            unit_number=unit_number,
            cliff_orientation=cliff_orientation,
        )
        key = (obs.name, fx, fy)
        if key not in self._obstacle_keys:
            self._obstacle_keys.add(key)
            self.obstacles.append(obs)
        self.charted_chunks.add((math.floor(fx) // CHUNK_SIZE, math.floor(fy) // CHUNK_SIZE))
        return obs

    def remove_obstacle_at(self, x: float, y: float, tolerance: float = 0.75) -> int:
        """Remove any registered obstacle within tolerance of (x, y); return count removed."""
        kept: list[TerrainObstacle] = []
        removed = 0
        for obs in self.obstacles:
            if math.hypot(obs.x - x, obs.y - y) <= tolerance:
                self._obstacle_keys.discard((obs.name, obs.x, obs.y))
                removed += 1
            else:
                kept.append(obs)
        self.obstacles = kept
        return removed

    def ingest_telemetry_dict(self, payload: Mapping[str, Any]) -> None:
        """Update map state from a parsed telemetry or cartography JSON dictionary."""
        surface_block = payload.get("cartography") or payload.get("surface") or payload
        if not isinstance(surface_block, Mapping):
            return

        chunks_raw = surface_block.get("charted_chunks") or surface_block.get("chunks") or []
        if isinstance(chunks_raw, Sequence):
            for entry in chunks_raw:
                if isinstance(entry, Sequence) and len(entry) >= 2:
                    self.add_charted_chunk(int(entry[0]), int(entry[1]))
                elif isinstance(entry, Mapping) and "x" in entry and "y" in entry:
                    self.add_charted_chunk(int(entry["x"]), int(entry["y"]))

        water_raw = surface_block.get("water_tiles") or []
        if isinstance(water_raw, Sequence):
            for pt in water_raw:
                if isinstance(pt, Sequence) and len(pt) >= 2:
                    self.add_water_tile(math.floor(float(pt[0])), math.floor(float(pt[1])))
                elif isinstance(pt, Mapping) and "x" in pt and "y" in pt:
                    self.add_water_tile(
                        math.floor(float(pt["x"])),
                        math.floor(float(pt["y"])),
                    )

        tiles_raw = surface_block.get("tiles") or []
        if isinstance(tiles_raw, Sequence):
            water_names = discover_water_tile_names(self.data)
            for t_entry in tiles_raw:
                if not isinstance(t_entry, Mapping):
                    continue
                t_name = str(t_entry.get("name", ""))
                pos = t_entry.get("position", t_entry)
                if isinstance(pos, Mapping) and "x" in pos and "y" in pos:
                    tx = math.floor(float(pos["x"]))
                    ty = math.floor(float(pos["y"]))
                    if t_name in water_names or bool(t_entry.get("is_water", False)):
                        self.add_water_tile(tx, ty)

        patches_raw = surface_block.get("resource_patches") or {}
        if isinstance(patches_raw, Mapping):
            for comm, pts in patches_raw.items():
                if not isinstance(pts, Sequence):
                    continue
                for pt in pts:
                    if isinstance(pt, Sequence) and len(pt) >= 2:
                        amount = int(pt[2]) if len(pt) >= 3 else 1000
                        self.add_resource_tile(str(comm), float(pt[0]), float(pt[1]), amount)
                    elif isinstance(pt, Mapping) and "x" in pt and "y" in pt:
                        self.add_resource_tile(
                            str(comm),
                            float(pt["x"]),
                            float(pt["y"]),
                            int(pt.get("amount", 1000)),
                        )

        resources_raw = surface_block.get("resources") or []
        if isinstance(resources_raw, Sequence):
            for r_ent in resources_raw:
                if not isinstance(r_ent, Mapping):
                    continue
                r_name = str(r_ent.get("name", ""))
                pos = r_ent.get("position", r_ent)
                if r_name and isinstance(pos, Mapping) and "x" in pos and "y" in pos:
                    self.add_resource_tile(
                        r_name,
                        float(pos["x"]),
                        float(pos["y"]),
                        int(r_ent.get("amount", 1000)),
                    )

        obstacles_raw = surface_block.get("obstacles") or []
        if isinstance(obstacles_raw, Sequence):
            for o_ent in obstacles_raw:
                if not isinstance(o_ent, Mapping):
                    continue
                o_name = str(o_ent.get("name", ""))
                o_type = str(o_ent.get("type", o_ent.get("entity_type", "tree")))
                pos = o_ent.get("position", o_ent)
                if not (o_name and isinstance(pos, Mapping) and "x" in pos and "y" in pos):
                    continue
                cbox: BoundingBox | None = None
                raw_bb = o_ent.get("bounding_box") or o_ent.get("collision_box")
                if isinstance(raw_bb, Sequence) and len(raw_bb) >= 2:
                    p0, p1 = raw_bb[0], raw_bb[1]
                    if (
                        isinstance(p0, Sequence)
                        and isinstance(p1, Sequence)
                        and len(p0) >= 2
                        and len(p1) >= 2
                    ):
                        ox, oy = float(pos["x"]), float(pos["y"])
                        # If bounding_box is in world coordinates, convert to relative offsets
                        b_x0, b_y0 = float(p0[0]), float(p0[1])
                        b_x1, b_y1 = float(p1[0]), float(p1[1])
                        if "bounding_box" in o_ent:
                            cbox = ((b_x0 - ox, b_y0 - oy), (b_x1 - ox, b_y1 - oy))
                        else:
                            cbox = ((b_x0, b_y0), (b_x1, b_y1))
                unit_num = o_ent.get("unit_number")
                self.add_obstacle(
                    name=o_name,
                    entity_type=o_type,
                    x=float(pos["x"]),
                    y=float(pos["y"]),
                    collision_box=cbox,
                    unit_number=int(unit_num) if isinstance(unit_num, int) else None,
                    cliff_orientation=(
                        str(o_ent["cliff_orientation"])
                        if "cliff_orientation" in o_ent and o_ent["cliff_orientation"] is not None
                        else None
                    ),
                )

    def ingest_telemetry_file(self, path: str | Path) -> bool:
        """Read a JSON telemetry file and ingest its cartography data; return True on success."""
        file_path = Path(path)
        if not file_path.is_file():
            return False
        try:
            text = file_path.read_text(encoding="utf-8").strip()
            if not text:
                return False
            payload = json.loads(text)
        except (OSError, ValueError):
            return False
        if not isinstance(payload, Mapping):
            return False
        self.ingest_telemetry_dict(payload)
        return True

    def classify_obstacles(
        self,
        researched_techs: set[str] | Sequence[str] = (),
    ) -> ObstacleClassification:
        """Partition obstacles into hard keepouts and clearable obstacles for researched_techs."""
        tech_set = set(researched_techs)
        cliff_unlocked = (
            is_cliff_demolition_unlocked(self.data, tech_set)
            if self.data is not None
            else ("cliff-explosives" in tech_set)
        )
        lf_unlocked = (
            is_landfill_unlocked(self.data, tech_set)
            if self.data is not None
            else ("landfill" in tech_set)
        )

        hard_obs: list[TerrainObstacle] = []
        clearable_obs: list[TerrainObstacle] = []
        hard_tiles: set[TileCoord] = set()
        clearable_tiles: set[TileCoord] = set()

        for obs in self.obstacles:
            if obs.entity_type == "cliff" and not cliff_unlocked:
                hard_obs.append(obs)
                hard_tiles.update(obs.occupied_tiles)
            else:
                clearable_obs.append(obs)
                clearable_tiles.update(obs.occupied_tiles)

        return ObstacleClassification(
            hard_obstacles=hard_obs,
            clearable_obstacles=clearable_obs,
            hard_tiles=hard_tiles,
            clearable_tiles=clearable_tiles,
            cliff_demolition_unlocked=cliff_unlocked,
            landfill_unlocked=lf_unlocked,
        )

    def surface_bounds(self, margin: int = DEFAULT_MARGIN_TILES) -> tuple[int, int, int, int]:
        """Compute (min_x, max_x, min_y, max_y) across all charted tiles, patches, and obstacles."""
        xs: list[int] = []
        ys: list[int] = []
        for wx, wy in self.water_tiles:
            xs.append(wx)
            ys.append(wy)
        for pts in self.resource_patches.values():
            for px, py in pts:
                xs.append(px)
                ys.append(py)
        for obs in self.obstacles:
            for ox, oy in obs.occupied_tiles:
                xs.append(ox)
                ys.append(oy)
        if not xs:
            if self.charted_chunks:
                cxs = [cx * CHUNK_SIZE for cx, _cy in self.charted_chunks]
                cys = [cy * CHUNK_SIZE for _cx, cy in self.charted_chunks]
                return (
                    min(cxs),
                    max(cxs) + CHUNK_SIZE - 1,
                    min(cys),
                    max(cys) + CHUNK_SIZE - 1,
                )
            return (0, CHUNK_SIZE - 1, 0, CHUNK_SIZE - 1)
        return (
            min(xs) - margin,
            max(xs) + margin,
            min(ys) - margin,
            max(ys) + margin,
        )

    def to_map_surface_spec(
        self,
        researched_techs: set[str] | Sequence[str] = (),
        bounds: tuple[int, int, int, int] | None = None,
    ) -> MapSurfaceSpec:
        """Build a MapSurfaceSpec where only non-clearable obstacles act as hard routing keepouts."""
        classification = self.classify_obstacles(researched_techs)
        min_x, max_x, min_y, max_y = bounds if bounds is not None else self.surface_bounds()
        patches_copy = {comm: set(pts) for comm, pts in self.resource_patches.items() if pts}
        return MapSurfaceSpec(
            min_x=min_x,
            max_x=max_x,
            min_y=min_y,
            max_y=max_y,
            water_tiles=set(self.water_tiles),
            resource_patches=patches_copy,
            obstacles=set(classification.hard_tiles),
        )

    def obstacles_to_clear_for_entities(
        self,
        entities: Sequence[Mapping[str, Any]],
        researched_techs: set[str] | Sequence[str] = (),
        reference_pos: tuple[float, float] = (0.0, 0.0),
    ) -> list[TerrainObstacle]:
        """Return clearable obstacles overlapping any planned entity footprint, sorted by distance."""
        classification = self.classify_obstacles(researched_techs)
        planned_tiles: set[TileCoord] = set()
        for ent in entities:
            planned_tiles.update(_footprint_for_entity_dict(ent, self.data))

        rx, ry = reference_pos
        overlapping = [
            obs for obs in classification.clearable_obstacles if obs.occupied_tiles & planned_tiles
        ]
        overlapping.sort(key=lambda o: (math.hypot(o.x - rx, o.y - ry), o.x, o.y))
        return overlapping


def build_lua_cartography_scanner_snippet(radius: int = 96) -> str:
    """Return a Lua snippet that scans water tiles, resource patches, trees, rocks, and cliffs."""
    r_val = int(radius)
    return (
        "      if not storage.cartography then\n"
        f"        local r_scan = {r_val}\n"
        "        local px0 = math.floor(p.position.x)\n"
        "        local py0 = math.floor(p.position.y)\n"
        "        local area = {{px0 - r_scan, py0 - r_scan}, {px0 + r_scan, py0 + r_scan}}\n"
        "        local water_pts = {}\n"
        "        for _, t in ipairs(p.surface.find_tiles_filtered({area = area, collision_mask = 'water_tile'})) do\n"
        "          if not t.collides_with('ground_tile') then\n"
        "            water_pts[#water_pts + 1] = {t.position.x, t.position.y}\n"
        "          end\n"
        "        end\n"
        "        local patches = {}\n"
        "        for _, res in ipairs(p.surface.find_entities_filtered({area = area, type = 'resource'})) do\n"
        "          local mp = res.prototype.mineable_properties\n"
        "          if mp and mp.minable and not mp.required_fluid then\n"
        "            local list = patches[res.name]\n"
        "            if not list then list = {} patches[res.name] = list end\n"
        "            list[#list + 1] = {math.floor(res.position.x), math.floor(res.position.y), res.amount}\n"
        "          end\n"
        "        end\n"
        "        local obs_list = {}\n"
        "        for _, o in ipairs(p.surface.find_entities_filtered({area = area, type = {'tree', 'simple-entity', 'cliff'}})) do\n"
        "          local bb = o.bounding_box\n"
        "          obs_list[#obs_list + 1] = {\n"
        "            name = o.name,\n"
        "            type = o.type,\n"
        "            position = {x = o.position.x, y = o.position.y},\n"
        "            bounding_box = {{bb.left_top.x, bb.left_top.y}, {bb.right_bottom.x, bb.right_bottom.y}},\n"
        "            unit_number = o.unit_number,\n"
        "            cliff_orientation = o.type == 'cliff' and o.cliff_orientation or nil\n"
        "          }\n"
        "        end\n"
        "        storage.cartography = {\n"
        "          water_tiles = water_pts,\n"
        "          resource_patches = patches,\n"
        "          obstacles = obs_list\n"
        "        }\n"
        "      end\n"
    )
