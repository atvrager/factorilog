"""Render Factorio blueprint strings to PNG images.

This module decodes Factorio blueprint exchange strings and composites
entity, tile, wire, and icon sprites from local game data into a PNG image.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

from PIL import Image, ImageDraw

from foundry.blueprint import decode
from foundry.bp2save import find_factorio_binary
from foundry.entity_helpers import (
    DIR_STEPS,
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    NORTH_SOUTH_DIRS,
    OPPOSITE_DIR,
    entity_size,
    register_prototype_boxes,
)
from foundry.native_engine.verifier import load_game_prototypes

JsonMap: TypeAlias = Mapping[str, object]
CoordPair: TypeAlias = tuple[int, int]
FloatPair: TypeAlias = tuple[float, float]
RgbaColor: TypeAlias = tuple[int, int, int, int]

BASE_TILE_PX = 32
MIN_TILE_PX = 8
MAX_CANVAS_DIM = 8192
MARGIN_TILES = 3.0
CHUNK_TILES = 32
PAIR_LEN = 2
TRIPLE_LEN = 3
QUAD_LEN = 4
HALF_RATIO = 0.5
FULL_ROTATION = 16
DIR_STEP_DIVISOR = 4
PNG_COMPRESS_LEVEL = 1
WIRE_CURVE_STEPS = 12
WIRE_SAG_SCALE = 0.22
WIRE_MAX_SAG_TILES = 0.9
WIRE_PARABOLA_FACTOR = 4.0
WIRE_WIDTH_PX = 2
WIRE_TILE_PX_DIVISOR = 12
ALT_ICON_BG_SCALE = 0.55
ALT_ICON_SCALE = 0.45
ALT_BG_RADIUS_RATIO = 0.35
ALT_BG_CORNER_RADIUS = 3
DISPLAY_PANEL_ICON_OFFSET_Y = -0.25
COMBINATOR_ICON_OFFSET_Y = 0.25
POLE_COPPER_OFFSET_Y = -0.85
POLE_RED_OFFSET_X = 0.25
POLE_RED_OFFSET_Y = -0.65
POLE_GREEN_OFFSET_X = -0.25
POLE_GREEN_OFFSET_Y = -0.65
COMBINATOR_CONN_OFFSET = 0.5
SWITCH_COPPER_OFFSET_X = 0.65
ICON_RAW_SIZE = 64
TILE_SHEET_GRID = 8
TILE_SPRITE_RAW_SIZE = 64
TILE_SPRITE_SCALE = 0.5
EVEN_MODULO = 2
ELAPSED_ROUND_DIGITS = 3
MAX_BYTE = 255
MAX_BYTE_FLOAT = 255.0
ALPHA_CHANNEL_IDX = 3
WIRE_DST_ID_IDX = 3
ARROW_TIP_RATIO = 0.3
ARROW_WIDTH_DIVISOR = 12
INSERTER_ARM_RATIO = 0.38
INSERTER_ARM_WIDTH_DIVISOR = 10
INSERTER_HUB_DIVISOR = 9

RANK_BELT = 0
RANK_TRANSPORT_STRUCT = 1
RANK_DEFAULT_ENTITY = 2
RANK_INSERTER = 3
RANK_POLE = 4

BELT_STRAIGHT_NORTH = 3
BELT_STRAIGHT_EAST = 1
BELT_STRAIGHT_SOUTH = 4
BELT_STRAIGHT_WEST = 2
BELT_RIGHT_NORTH = 5
BELT_RIGHT_EAST = 9
BELT_RIGHT_SOUTH = 12
BELT_RIGHT_WEST = 8
BELT_LEFT_NORTH = 7
BELT_LEFT_EAST = 6
BELT_LEFT_SOUTH = 10
BELT_LEFT_WEST = 11

WIRE_ID_CIRCUIT_RED = 1
WIRE_ID_CIRCUIT_GREEN = 2
WIRE_ID_COMBINATOR_IN_RED = 1
WIRE_ID_COMBINATOR_IN_GREEN = 2
WIRE_ID_COMBINATOR_OUT_RED = 3
WIRE_ID_COMBINATOR_OUT_GREEN = 4
WIRE_ID_POLE_COPPER = 5
WIRE_ID_SWITCH_LEFT = 5
WIRE_ID_SWITCH_RIGHT = 6

BG_COLOR: RgbaColor = (28, 30, 34, 255)
GRID_LINE_COLOR: RgbaColor = (42, 45, 52, 255)
CHUNK_LINE_COLOR: RgbaColor = (64, 70, 82, 255)
ORIGIN_LINE_COLOR: RgbaColor = (88, 96, 112, 255)
TILE_FALLBACK_COLOR: RgbaColor = (58, 60, 64, 255)
HAZARD_FALLBACK_COLOR: RgbaColor = (118, 98, 38, 255)
ENTITY_OUTLINE_COLOR: RgbaColor = (32, 36, 42, 255)
ICON_BG_FALLBACK_COLOR: RgbaColor = (16, 18, 22, 210)
BELT_ARROW_COLOR: RgbaColor = (245, 210, 75, 230)

FALLBACK_FILL_COLORS: Mapping[str, RgbaColor] = {
    "transport-belt": (210, 165, 45, 255),
    "underground-belt": (195, 145, 40, 255),
    "splitter": (205, 150, 45, 255),
    "inserter": (185, 145, 50, 255),
    "pipe": (110, 130, 150, 255),
    "pipe-to-ground": (100, 120, 140, 255),
    "pump": (120, 145, 165, 255),
    "electric-pole": (135, 105, 75, 255),
    "assembling-machine": (75, 120, 165, 255),
    "furnace": (155, 105, 75, 255),
    "chemical-plant": (95, 150, 135, 255),
    "oil-refinery": (145, 115, 85, 255),
    "centrifuge": (90, 155, 95, 255),
    "lab": (115, 155, 185, 255),
    "radar": (95, 125, 145, 255),
    "boiler": (145, 95, 75, 255),
    "generator": (125, 135, 150, 255),
    "rocket-silo": (135, 110, 95, 255),
    "cargo-landing-pad": (125, 125, 135, 255),
    "combinator": (85, 115, 95, 255),
    "display-panel": (95, 105, 120, 255),
    "power-switch": (125, 105, 95, 255),
    "lamp": (215, 215, 155, 255),
}
DEFAULT_ENTITY_FILL: RgbaColor = (105, 115, 128, 255)

ENTITY_LAYER_RANKS: Mapping[str, int] = {
    "transport-belt": RANK_BELT,
    "underground-belt": RANK_TRANSPORT_STRUCT,
    "splitter": RANK_TRANSPORT_STRUCT,
    "pipe": RANK_TRANSPORT_STRUCT,
    "pipe-to-ground": RANK_TRANSPORT_STRUCT,
    "inserter": RANK_INSERTER,
    "electric-pole": RANK_POLE,
}

WIRE_COLORS: Mapping[str, RgbaColor] = {
    "copper": (207, 124, 0, 235),
    "red": (200, 55, 24, 235),
    "green": (88, 140, 56, 235),
}

POLE_DEFAULT_WIRE_OFFSETS: Mapping[str, FloatPair] = {
    "copper": (0.0, POLE_COPPER_OFFSET_Y),
    "red": (POLE_RED_OFFSET_X, POLE_RED_OFFSET_Y),
    "green": (POLE_GREEN_OFFSET_X, POLE_GREEN_OFFSET_Y),
}

DIR_NAMES: Mapping[int, str] = {
    DIRECTION_NORTH: "north",
    DIRECTION_EAST: "east",
    DIRECTION_SOUTH: "south",
    DIRECTION_WEST: "west",
}

CARDINAL_DIRS: tuple[int, int, int, int] = (
    DIRECTION_NORTH,
    DIRECTION_EAST,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
)

BELT_STRAIGHT_INDICES: Mapping[int, tuple[str, int]] = {
    DIRECTION_NORTH: ("north_index", BELT_STRAIGHT_NORTH),
    DIRECTION_EAST: ("east_index", BELT_STRAIGHT_EAST),
    DIRECTION_SOUTH: ("south_index", BELT_STRAIGHT_SOUTH),
    DIRECTION_WEST: ("west_index", BELT_STRAIGHT_WEST),
}

BELT_RIGHT_INDICES: Mapping[int, tuple[str, int]] = {
    DIRECTION_NORTH: ("east_to_north_index", BELT_RIGHT_NORTH),
    DIRECTION_EAST: ("south_to_east_index", BELT_RIGHT_EAST),
    DIRECTION_SOUTH: ("west_to_south_index", BELT_RIGHT_SOUTH),
    DIRECTION_WEST: ("north_to_west_index", BELT_RIGHT_WEST),
}

BELT_LEFT_INDICES: Mapping[int, tuple[str, int]] = {
    DIRECTION_NORTH: ("west_to_north_index", BELT_LEFT_NORTH),
    DIRECTION_EAST: ("north_to_east_index", BELT_LEFT_EAST),
    DIRECTION_SOUTH: ("east_to_south_index", BELT_LEFT_SOUTH),
    DIRECTION_WEST: ("south_to_west_index", BELT_LEFT_WEST),
}

PIPE_SPRITE_KEYS: Mapping[tuple[bool, ...], str] = {
    (True, True, True, True): "cross",
    (True, True, False, True): "t_up",
    (False, True, True, True): "t_down",
    (True, True, True, False): "t_right",
    (True, False, True, True): "t_left",
    (True, False, True, False): "straight_vertical",
    (False, True, False, True): "straight_horizontal",
    (True, True, False, False): "corner_up_right",
    (True, False, False, True): "corner_up_left",
    (False, True, True, False): "corner_down_right",
    (False, False, True, True): "corner_down_left",
    (True, False, False, False): "ending_up",
    (False, False, True, False): "ending_down",
    (False, True, False, False): "ending_right",
    (False, False, False, True): "ending_left",
    (False, False, False, False): "straight_vertical_single",
}

ARITHMETIC_OP_KEYS: Mapping[str, str] = {
    "+": "plus_symbol_sprites",
    "-": "minus_symbol_sprites",
    "*": "multiply_symbol_sprites",
    "/": "divide_symbol_sprites",
    "%": "modulo_symbol_sprites",
    "^": "power_symbol_sprites",
    "<<": "left_shift_symbol_sprites",
    ">>": "right_shift_symbol_sprites",
    "AND": "and_symbol_sprites",
    "OR": "or_symbol_sprites",
    "XOR": "xor_symbol_sprites",
}

DECIDER_OP_KEYS: Mapping[str, str] = {
    "<": "less_symbol_sprites",
    ">": "greater_symbol_sprites",
    "≤": "less_or_equal_symbol_sprites",
    "<=": "less_or_equal_symbol_sprites",
    "≥": "greater_or_equal_symbol_sprites",
    ">=": "greater_or_equal_symbol_sprites",
    "=": "equal_symbol_sprites",
    "≠": "not_equal_symbol_sprites",
    "!=": "not_equal_symbol_sprites",
}

ROCKET_SILO_LAYER_KEYS: tuple[str, ...] = (
    "door_back_sprite",
    "door_front_sprite",
    "base_day_sprite",
    "arm_01_back_animation",
    "arm_02_right_animation",
    "arm_03_front_animation",
)

LANDING_PAD_WALL_KEYS: tuple[str, ...] = (
    "top_wall",
    "bottom_wall",
    "left_wall",
    "right_wall",
)

PROTOTYPE_SEARCH_CATEGORIES: tuple[str, ...] = (
    "transport-belt",
    "underground-belt",
    "splitter",
    "inserter",
    "pipe",
    "pipe-to-ground",
    "pump",
    "electric-pole",
    "assembling-machine",
    "furnace",
    "rocket-silo",
    "cargo-landing-pad",
    "lab",
    "radar",
    "boiler",
    "generator",
    "arithmetic-combinator",
    "decider-combinator",
    "constant-combinator",
    "display-panel",
    "power-switch",
    "lamp",
    "container",
    "logistic-container",
    "infinity-container",
    "infinity-pipe",
    "electric-energy-interface",
    "loader",
    "mining-drill",
    "solar-panel",
    "accumulator",
    "storage-tank",
    "beacon",
    "roboport",
    "reactor",
    "heat-pipe",
    "wall",
    "gate",
)

ICON_SEARCH_CATEGORIES: tuple[str, ...] = (
    "recipe",
    "item",
    "fluid",
    "tool",
    "ammo",
    "capsule",
    "gun",
    "armor",
    "module",
    "rail-planner",
    "item-with-entity-data",
    "space-platform-starter-pack",
    "virtual-signal",
    *PROTOTYPE_SEARCH_CATEGORIES,
)


@dataclass(frozen=True, slots=True)
class SpriteSpec:
    """Resolved sprite crop and placement parameters in tile coordinates."""

    filename: str
    crop_x: int
    crop_y: int
    width: int
    height: int
    scale: float = TILE_SPRITE_SCALE
    shift_x: float = 0.0
    shift_y: float = 0.0
    anchor_x: float = HALF_RATIO
    anchor_y: float = HALF_RATIO
    tint: RgbaColor | None = None


@dataclass(frozen=True)
class Viewport:
    """Canvas coordinate transform between blueprint tiles and image pixels."""

    min_x: float
    min_y: float
    width_px: int
    height_px: int
    tile_px: int

    def to_px(self, tile_x: float, tile_y: float) -> FloatPair:
        """Convert blueprint tile coordinates to floating-point pixel coordinates."""
        return (
            (tile_x - self.min_x) * self.tile_px,
            (tile_y - self.min_y) * self.tile_px,
        )


def _index_entity_prototypes(
    prototypes: Mapping[str, object],
) -> dict[str, tuple[str, Mapping[str, object]]]:
    """Build a map from entity name to `(category, prototype_dict)`."""
    lookup: dict[str, tuple[str, Mapping[str, object]]] = {}
    for cat in PROTOTYPE_SEARCH_CATEGORIES:
        for name, proto in _as_map(prototypes.get(cat)).items():
            proto_map = _as_map(proto)
            if proto_map and name not in lookup:
                lookup[name] = (cat, proto_map)
    return lookup


def _index_icon_prototypes(prototypes: Mapping[str, object]) -> dict[str, tuple[str, int]]:
    """Build a map from item, fluid, recipe, or signal name to `(icon_path, icon_size)`."""
    lookup: dict[str, tuple[str, int]] = {}
    for cat in ICON_SEARCH_CATEGORIES:
        for name, proto in _as_map(prototypes.get(cat)).items():
            if name in lookup:
                continue
            icon_entry = _extract_icon_entry(_as_map(proto))
            if icon_entry is not None:
                lookup[name] = icon_entry
    return lookup


@dataclass
class SpriteAtlas:
    """Loads and caches Factorio sprite sheets and scaled stamps."""

    data_dir: Path | None
    prototypes: Mapping[str, object]
    entity_lookup: dict[str, tuple[str, Mapping[str, object]]] = field(default_factory=dict)
    icon_lookup: dict[str, tuple[str, int]] = field(default_factory=dict)
    sheet_cache: dict[str, Image.Image | None] = field(default_factory=dict)
    stamp_cache: dict[
        tuple[str, int, int, int, int, int, int, RgbaColor | None], Image.Image | None
    ] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Index entity prototypes and icon paths for fast lookup."""
        self.entity_lookup = _index_entity_prototypes(self.prototypes)
        self.icon_lookup = _index_icon_prototypes(self.prototypes)

    def resolve_path(self, factorio_path: str) -> Path | None:
        """Resolve a `__base__` or `__core__` asset string to a local file path."""
        if self.data_dir is None or not factorio_path:
            return None
        if factorio_path.startswith("__base__/"):
            candidate = self.data_dir / "base" / factorio_path.removeprefix("__base__/")
            return candidate if candidate.is_file() else None
        if factorio_path.startswith("__core__/"):
            candidate = self.data_dir / "core" / factorio_path.removeprefix("__core__/")
            return candidate if candidate.is_file() else None
        return None

    def load_sheet(self, factorio_path: str) -> Image.Image | None:
        """Load and cache a full PNG sprite sheet in RGBA format."""
        if factorio_path in self.sheet_cache:
            return self.sheet_cache[factorio_path]
        resolved = self.resolve_path(factorio_path)
        if resolved is None:
            self.sheet_cache[factorio_path] = None
            return None
        try:
            with Image.open(resolved) as img:
                rgba = img.convert("RGBA")
        except OSError:
            rgba = None
        self.sheet_cache[factorio_path] = rgba
        return rgba

    def get_stamp(self, spec: SpriteSpec, tile_px: int) -> Image.Image | None:
        """Return a cropped, tinted, and resized RGBA stamp for the given sprite spec."""
        scale_ratio = tile_px / BASE_TILE_PX
        target_w = max(1, round(spec.width * spec.scale * scale_ratio))
        target_h = max(1, round(spec.height * spec.scale * scale_ratio))
        cache_key = (
            spec.filename,
            spec.crop_x,
            spec.crop_y,
            spec.width,
            spec.height,
            target_w,
            target_h,
            spec.tint,
        )
        if cache_key in self.stamp_cache:
            return self.stamp_cache[cache_key]

        sheet = self.load_sheet(spec.filename)
        if sheet is None:
            self.stamp_cache[cache_key] = None
            return None

        right = min(sheet.width, spec.crop_x + spec.width)
        bottom = min(sheet.height, spec.crop_y + spec.height)
        if right <= spec.crop_x or bottom <= spec.crop_y:
            self.stamp_cache[cache_key] = None
            return None

        cropped = sheet.crop((spec.crop_x, spec.crop_y, right, bottom))
        if spec.tint is not None:
            cropped = _apply_tint(cropped, spec.tint)
        if cropped.size != (target_w, target_h):
            cropped = cropped.resize((target_w, target_h), Image.Resampling.BILINEAR)
        self.stamp_cache[cache_key] = cropped
        return cropped


def _as_map(val: object) -> Mapping[str, object]:
    """Return val as a string-keyed mapping, or an empty dict if not a mapping."""
    if isinstance(val, Mapping):
        return val
    return {}


def _as_seq(val: object) -> Sequence[object]:
    """Return val as a sequence, or an empty tuple if not a sequence."""
    if isinstance(val, Sequence) and not isinstance(val, (str, bytes)):
        return val
    return ()


def _as_float(val: object, default: float = 0.0) -> float:
    """Convert a JSON value to float with a default fallback."""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return default


def _as_int(val: object, default: int = 0) -> int:
    """Convert a JSON value to int with a default fallback."""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return int(val)
    return default


def _as_str(val: object, default: str = "") -> str:
    """Convert a JSON value to str with a default fallback."""
    if isinstance(val, str):
        return val
    return default


def _extract_icon_entry(proto: Mapping[str, object]) -> tuple[str, int] | None:
    """Extract the primary icon filename and size from a prototype mapping."""
    icon = _as_str(proto.get("icon"))
    if icon:
        return icon, _as_int(proto.get("icon_size"), ICON_RAW_SIZE)
    icons = _as_seq(proto.get("icons"))
    if icons:
        first = _as_map(icons[0])
        first_icon = _as_str(first.get("icon"))
        if first_icon:
            return first_icon, _as_int(first.get("icon_size"), ICON_RAW_SIZE)
    return None


def _apply_tint(image: Image.Image, tint: RgbaColor) -> Image.Image:
    """Multiply RGBA channels of an image by a tint color."""
    channels = image.split()
    if len(channels) != QUAD_LEN:
        return image
    scaled = tuple(
        ch.point(lambda p, mult=mult: (p * mult) // MAX_BYTE)
        for ch, mult in zip(channels, tint, strict=True)
    )
    return Image.merge("RGBA", scaled)


def _clamp_byte(val: float) -> int:
    """Clamp a floating-point channel value to an 8-bit integer in `[0, 255]`."""
    return max(0, min(MAX_BYTE, round(val)))


def _parse_tint(raw_tint: object) -> RgbaColor | None:
    """Convert a Factorio color table or sequence into an 8-bit RGBA tuple."""
    tint_seq = _as_seq(raw_tint)
    if isinstance(raw_tint, Mapping):
        rgba = (
            _as_float(raw_tint.get("r"), 1.0),
            _as_float(raw_tint.get("g"), 1.0),
            _as_float(raw_tint.get("b"), 1.0),
            _as_float(raw_tint.get("a"), 1.0),
        )
    elif len(tint_seq) >= TRIPLE_LEN:
        alpha = _as_float(tint_seq[ALPHA_CHANNEL_IDX], 1.0) if len(tint_seq) >= QUAD_LEN else 1.0
        rgba = (
            _as_float(tint_seq[0], 1.0),
            _as_float(tint_seq[1], 1.0),
            _as_float(tint_seq[2], 1.0),
            alpha,
        )
    else:
        return None
    scale = MAX_BYTE_FLOAT if max(rgba) <= 1.0 else 1.0
    return (
        _clamp_byte(rgba[0] * scale),
        _clamp_byte(rgba[1] * scale),
        _clamp_byte(rgba[2] * scale),
        _clamp_byte(rgba[ALPHA_CHANNEL_IDX] * scale),
    )


def _find_factorio_data_dir() -> Path | None:
    """Locate the Factorio `data/` directory containing `base` and `core` assets."""
    try:
        game_bin = find_factorio_binary()
    except FileNotFoundError:
        return None
    data_dir = game_bin.parent.parent.parent / "data"
    return data_dir if data_dir.is_dir() else None


def _rotate_point(pt: FloatPair, direction: int) -> FloatPair:
    """Rotate a 2D point around the origin by a cardinal Factorio direction."""
    x, y = pt
    if direction == DIRECTION_EAST:
        return (-y, x)
    if direction == DIRECTION_SOUTH:
        return (-x, -y)
    if direction == DIRECTION_WEST:
        return (y, -x)
    return (x, y)


def _parse_shift(raw_shift: object) -> FloatPair:
    """Extract an `(x, y)` shift pair from a sequence or mapping."""
    if isinstance(raw_shift, Sequence) and len(raw_shift) >= PAIR_LEN:
        return (_as_float(raw_shift[0]), _as_float(raw_shift[1]))
    if isinstance(raw_shift, Mapping):
        return (_as_float(raw_shift.get("x")), _as_float(raw_shift.get("y")))
    return (0.0, 0.0)


def _sprite_spec_from_dict(
    data: Mapping[str, object],
    *,
    frame_x: int = 0,
    frame_y: int = 0,
    extra_shift: FloatPair = (0.0, 0.0),
) -> SpriteSpec | None:
    """Convert a prototype sprite dictionary into a resolved `SpriteSpec`."""
    if data.get("draw_as_shadow"):
        return None
    filename = _as_str(data.get("filename"))
    if not filename:
        filenames = _as_seq(data.get("filenames"))
        if filenames:
            filename = _as_str(filenames[0])
    if not filename:
        return None

    size = data.get("size")
    default_w = _as_int(size) if isinstance(size, int) else 0
    default_h = default_w
    if isinstance(size, Sequence) and len(size) >= PAIR_LEN:
        default_w = _as_int(size[0])
        default_h = _as_int(size[1])

    width = _as_int(data.get("width"), default_w)
    height = _as_int(data.get("height"), default_h)
    if width <= 0 or height <= 0:
        return None

    base_x = _as_int(data.get("x"), 0)
    base_y = _as_int(data.get("y"), 0)
    scale = _as_float(data.get("scale"), TILE_SPRITE_SCALE)
    sx, sy = _parse_shift(data.get("shift"))
    return SpriteSpec(
        filename=filename,
        crop_x=base_x + frame_x * width,
        crop_y=base_y + frame_y * height,
        width=width,
        height=height,
        scale=scale,
        shift_x=sx + extra_shift[0],
        shift_y=sy + extra_shift[1],
        tint=_parse_tint(data.get("tint")),
    )


def _extract_layers(
    node: object,
    *,
    frame_x: int = 0,
    frame_y: int = 0,
    extra_shift: FloatPair = (0.0, 0.0),
) -> list[SpriteSpec]:
    """Extract non-shadow `SpriteSpec` items from a sprite, `layers`, `sheets`, or sequence node."""
    node_seq = _as_seq(node)
    if node_seq:
        return [
            spec
            for item in node_seq
            for spec in _extract_layers(
                item, frame_x=frame_x, frame_y=frame_y, extra_shift=extra_shift
            )
        ]
    node_map = _as_map(node)
    if not node_map:
        return []
    for list_key in ("layers", "sheets", "animation"):
        if list_key in node_map:
            return _extract_layers(
                node_map[list_key], frame_x=frame_x, frame_y=frame_y, extra_shift=extra_shift
            )
    if "sheet" in node_map:
        return _extract_layers(
            node_map["sheet"], frame_x=frame_x, frame_y=frame_y, extra_shift=extra_shift
        )
    spec = _sprite_spec_from_dict(
        node_map,
        frame_x=frame_x,
        frame_y=frame_y,
        extra_shift=extra_shift,
    )
    return [spec] if spec is not None else []


def _extract_directional_layers(
    node: object,
    direction: int,
    *,
    extra_shift: FloatPair = (0.0, 0.0),
) -> list[SpriteSpec]:
    """Extract layers for a 4-way directional animation or sprite set."""
    node_map = _as_map(node)
    if not node_map:
        return []
    dir_name = DIR_NAMES.get(direction, "north")
    if dir_name in node_map:
        return _extract_layers(node_map[dir_name], extra_shift=extra_shift)
    if "north" in node_map:
        return _extract_layers(node_map["north"], extra_shift=extra_shift)
    return _extract_layers(node_map, extra_shift=extra_shift)


def _paste_stamp(
    canvas: Image.Image,
    stamp: Image.Image,
    center_px: FloatPair,
    anchor: FloatPair = (HALF_RATIO, HALF_RATIO),
) -> None:
    """Alpha-composite a sprite stamp onto the canvas at the given pixel anchor."""
    dx = round(center_px[0] - stamp.width * anchor[0])
    dy = round(center_px[1] - stamp.height * anchor[1])
    if dx >= canvas.width or dy >= canvas.height or dx + stamp.width <= 0 or dy + stamp.height <= 0:
        return
    canvas.alpha_composite(stamp, (dx, dy))


def _paste_spec(
    canvas: Image.Image,
    atlas: SpriteAtlas,
    viewport: Viewport,
    entity_pos: FloatPair,
    spec: SpriteSpec,
) -> bool:
    """Render a single `SpriteSpec` at `entity_pos` on the canvas."""
    stamp = atlas.get_stamp(spec, viewport.tile_px)
    if stamp is None:
        return False
    px, py = viewport.to_px(entity_pos[0] + spec.shift_x, entity_pos[1] + spec.shift_y)
    _paste_stamp(canvas, stamp, (px, py), (spec.anchor_x, spec.anchor_y))
    return True


def _load_blueprint_payload(bp_input: str | Path) -> Mapping[str, object]:
    """Decode a blueprint file or exchange string into its inner `blueprint` dictionary."""
    raw_text = str(bp_input).strip()
    candidate = Path(raw_text)
    try:
        if candidate.is_file():
            raw_text = candidate.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    doc = decode(raw_text)
    bp = _as_map(doc.get("blueprint"))
    return bp if bp else doc


def _compute_viewport(
    entities: Sequence[Mapping[str, object]],
    tiles: Sequence[Mapping[str, object]],
) -> Viewport:
    """Calculate the bounding box and pixel scale for all blueprint entities and tiles."""
    min_x = math.inf
    min_y = math.inf
    max_x = -math.inf
    max_y = -math.inf

    for ent in entities:
        pos = _as_map(ent.get("position"))
        ex = _as_float(pos.get("x"))
        ey = _as_float(pos.get("y"))
        name = _as_str(ent.get("name"))
        direction = _as_int(ent.get("direction"), DIRECTION_NORTH)
        w, h = entity_size(name, direction)
        min_x = min(min_x, ex - w * HALF_RATIO)
        min_y = min(min_y, ey - h * HALF_RATIO)
        max_x = max(max_x, ex + w * HALF_RATIO)
        max_y = max(max_y, ey + h * HALF_RATIO)

    for tile in tiles:
        pos = _as_map(tile.get("position"))
        tx = _as_float(pos.get("x"))
        ty = _as_float(pos.get("y"))
        min_x = min(min_x, tx)
        min_y = min(min_y, ty)
        max_x = max(max_x, tx + 1.0)
        max_y = max(max_y, ty + 1.0)

    if not math.isfinite(min_x):
        min_x, min_y, max_x, max_y = -MARGIN_TILES, -MARGIN_TILES, MARGIN_TILES, MARGIN_TILES
    else:
        min_x = math.floor(min_x - MARGIN_TILES)
        min_y = math.floor(min_y - MARGIN_TILES)
        max_x = math.ceil(max_x + MARGIN_TILES)
        max_y = math.ceil(max_y + MARGIN_TILES)

    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)
    max_span = max(span_x, span_y)
    tile_px = BASE_TILE_PX
    if max_span * tile_px > MAX_CANVAS_DIM:
        tile_px = max(MIN_TILE_PX, int(MAX_CANVAS_DIM // max_span))

    return Viewport(
        min_x=min_x,
        min_y=min_y,
        width_px=max(1, round(span_x * tile_px)),
        height_px=max(1, round(span_y * tile_px)),
        tile_px=tile_px,
    )


def _grid_line_color(coord: int) -> RgbaColor:
    """Return the grid line color for a given integer tile coordinate."""
    if coord == 0:
        return ORIGIN_LINE_COLOR
    if coord % CHUNK_TILES == 0:
        return CHUNK_LINE_COLOR
    return GRID_LINE_COLOR


def _draw_background_grid(canvas: Image.Image, viewport: Viewport) -> None:
    """Draw tile grid lines and 32x32 chunk boundary lines onto the background."""
    draw = ImageDraw.Draw(canvas)
    start_tx = math.floor(viewport.min_x)
    end_tx = math.ceil(viewport.min_x + viewport.width_px / viewport.tile_px)
    start_ty = math.floor(viewport.min_y)
    end_ty = math.ceil(viewport.min_y + viewport.height_px / viewport.tile_px)

    for tx in range(start_tx, end_tx + 1):
        ix = round((tx - viewport.min_x) * viewport.tile_px)
        if 0 <= ix < viewport.width_px:
            draw.line([(ix, 0), (ix, viewport.height_px)], fill=_grid_line_color(tx), width=1)

    for ty in range(start_ty, end_ty + 1):
        iy = round((ty - viewport.min_y) * viewport.tile_px)
        if 0 <= iy < viewport.height_px:
            draw.line([(0, iy), (viewport.width_px, iy)], fill=_grid_line_color(ty), width=1)


def _resolve_tile_picture(tile_proto: Mapping[str, object]) -> str:
    """Extract the tile texture sheet path from a tile prototype."""
    variants = _as_map(tile_proto.get("variants"))
    picture = _as_str(_as_map(variants.get("material_background")).get("picture"))
    if picture:
        return picture
    main_vars = _as_seq(variants.get("main"))
    return _as_str(_as_map(main_vars[0]).get("picture")) if main_vars else ""


def _render_tiles(
    canvas: Image.Image,
    atlas: SpriteAtlas,
    viewport: Viewport,
    tiles: Sequence[Mapping[str, object]],
) -> None:
    """Render pavement tiles onto the canvas before entity layers."""
    if not tiles:
        return
    tile_protos = _as_map(atlas.prototypes.get("tile"))
    draw = ImageDraw.Draw(canvas)

    for tile in tiles:
        name = _as_str(tile.get("name"))
        pos = _as_map(tile.get("position"))
        tx = math.floor(_as_float(pos.get("x")))
        ty = math.floor(_as_float(pos.get("y")))
        picture = _resolve_tile_picture(_as_map(tile_protos.get(name)))

        pasted = False
        if picture:
            spec = SpriteSpec(
                filename=picture,
                crop_x=(tx % TILE_SHEET_GRID) * TILE_SPRITE_RAW_SIZE,
                crop_y=(ty % TILE_SHEET_GRID) * TILE_SPRITE_RAW_SIZE,
                width=TILE_SPRITE_RAW_SIZE,
                height=TILE_SPRITE_RAW_SIZE,
                scale=TILE_SPRITE_SCALE,
            )
            pasted = _paste_spec(canvas, atlas, viewport, (tx + HALF_RATIO, ty + HALF_RATIO), spec)

        if not pasted:
            x0, y0 = viewport.to_px(float(tx), float(ty))
            x1, y1 = viewport.to_px(float(tx + 1), float(ty + 1))
            fill = HAZARD_FALLBACK_COLOR if "hazard" in name else TILE_FALLBACK_COLOR
            draw.rectangle([round(x0), round(y0), round(x1), round(y1)], fill=fill)


def _belt_grid_cells(
    ent: Mapping[str, object],
) -> list[tuple[CoordPair, tuple[int, str, str]]]:
    """Return grid cell entries for a belt, splitter, or underground belt entity."""
    name = _as_str(ent.get("name"))
    pos = _as_map(ent.get("position"))
    ex, ey = _as_float(pos.get("x")), _as_float(pos.get("y"))
    direction = _as_int(ent.get("direction"), DIRECTION_NORTH)
    if "splitter" in name:
        return [
            (
                (
                    math.floor(ex + _rotate_point((side, 0.0), direction)[0]),
                    math.floor(ey + _rotate_point((side, 0.0), direction)[1]),
                ),
                (direction, "splitter", "output"),
            )
            for side in (-HALF_RATIO, HALF_RATIO)
        ]
    if "underground-belt" in name:
        return [
            (
                (math.floor(ex), math.floor(ey)),
                (direction, "underground-belt", _as_str(ent.get("type"), "input")),
            )
        ]
    if "transport-belt" in name:
        return [((math.floor(ex), math.floor(ey)), (direction, "transport-belt", "output"))]
    return []


def _neighbor_feeds_belt(
    coord: CoordPair,
    side_dir: int,
    belt_grid: Mapping[CoordPair, tuple[int, str, str]],
) -> bool:
    """Return True if the belt entity in `side_dir` from `coord` outputs into `coord`."""
    dx, dy = DIR_STEPS.get(side_dir, (0, -1))
    nb = belt_grid.get((coord[0] + dx, coord[1] + dy))
    if nb is None or nb[0] != OPPOSITE_DIR.get(side_dir, DIRECTION_SOUTH):
        return False
    return not (nb[1] == "underground-belt" and nb[2] == "input")


def _belt_curve_shape(
    coord: CoordPair,
    direction: int,
    belt_grid: Mapping[CoordPair, tuple[int, str, str]],
) -> str:
    """Determine whether a transport belt at `coord` is straight, curving right, or curving left."""
    back_dir = OPPOSITE_DIR.get(direction, DIRECTION_SOUTH)
    if _neighbor_feeds_belt(coord, back_dir, belt_grid):
        return "straight"

    right_feeds = _neighbor_feeds_belt(
        coord, (direction + DIR_STEP_DIVISOR) % FULL_ROTATION, belt_grid
    )
    left_feeds = _neighbor_feeds_belt(
        coord, (direction - DIR_STEP_DIVISOR) % FULL_ROTATION, belt_grid
    )
    if right_feeds and not left_feeds:
        return "right"
    if left_feeds and not right_feeds:
        return "left"
    return "straight"


def _belt_animation_spec(
    bas: Mapping[str, object],
    direction: int,
    shape: str,
    *,
    extra_shift: FloatPair = (0.0, 0.0),
) -> SpriteSpec | None:
    """Build the `SpriteSpec` for a belt tile from its `belt_animation_set` definition."""
    anim_set = _as_map(bas.get("animation_set"))
    if not anim_set:
        return None
    table = (
        BELT_RIGHT_INDICES
        if shape == "right"
        else BELT_LEFT_INDICES
        if shape == "left"
        else BELT_STRAIGHT_INDICES
    )
    idx_key, default_idx = table.get(direction, ("north_index", BELT_STRAIGHT_NORTH))
    row_index = max(1, _as_int(bas.get(idx_key), default_idx)) - 1
    return _sprite_spec_from_dict(anim_set, frame_y=row_index, extra_shift=extra_shift)


def _collect_proto_fluid_boxes(proto: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Collect all fluid box mappings defined on an entity prototype."""
    boxes = [_as_map(fb) for fb in _as_seq(proto.get("fluid_boxes"))]
    for key in ("fluid_box", "output_fluid_box"):
        fb = _as_map(proto.get(key))
        if fb:
            boxes.append(fb)
    return boxes


def _register_fluid_box_connections(
    conn_map: dict[CoordPair, set[int]],
    ex: float,
    ey: float,
    direction: int,
    fb: Mapping[str, object],
) -> None:
    """Add all normal pipe connection directions from `fb` into `conn_map`."""
    for conn in _as_seq(fb.get("pipe_connections")):
        c_map = _as_map(conn)
        if _as_str(c_map.get("connection_type"), "normal") != "normal":
            continue
        c_dir = (direction + _as_int(c_map.get("direction"), DIRECTION_NORTH)) % FULL_ROTATION
        raw_pos = c_map.get("position")
        if raw_pos is None:
            positions = _as_seq(c_map.get("positions"))
            idx = (direction // DIR_STEP_DIVISOR) % max(1, len(positions))
            raw_pos = positions[idx] if positions else (0.0, 0.0)
        ox, oy = _rotate_point(_parse_shift(raw_pos), direction)
        conn_map.setdefault((math.floor(ex + ox), math.floor(ey + oy)), set()).add(c_dir)


def _build_fluid_connection_points(
    entities: Sequence[Mapping[str, object]],
    atlas: SpriteAtlas,
) -> dict[CoordPair, set[int]]:
    """Map grid coordinates to cardinal directions where fluid entities expose connections."""
    conn_map: dict[CoordPair, set[int]] = {}
    for ent in entities:
        name = _as_str(ent.get("name"))
        pos = _as_map(ent.get("position"))
        ex, ey = _as_float(pos.get("x")), _as_float(pos.get("y"))
        direction = _as_int(ent.get("direction"), DIRECTION_NORTH)
        coord = (math.floor(ex), math.floor(ey))

        if name in ("pipe", "infinity-pipe"):
            conn_map.setdefault(coord, set()).update(DIR_STEPS.keys())
        elif "pipe-to-ground" in name:
            conn_map.setdefault(coord, set()).add(direction)
        else:
            _, proto = atlas.entity_lookup.get(name, ("", {}))
            for fb in _collect_proto_fluid_boxes(proto):
                _register_fluid_box_connections(conn_map, ex, ey, direction, fb)
    return conn_map


def _pipe_specs_for_entity(
    proto: Mapping[str, object],
    coord: CoordPair,
    fluid_conns: Mapping[CoordPair, set[int]],
) -> list[SpriteSpec]:
    """Select the connected pipe sprite from `pictures` based on adjacent fluid connections."""
    pictures = _as_map(proto.get("pictures"))
    if not pictures:
        return []
    key_tuple = tuple(
        OPPOSITE_DIR[d]
        in fluid_conns.get((coord[0] + DIR_STEPS[d][0], coord[1] + DIR_STEPS[d][1]), set())
        for d in CARDINAL_DIRS
    )
    sprite_key = PIPE_SPRITE_KEYS.get(key_tuple, "straight_vertical_single")
    if sprite_key == "straight_vertical" and coord[1] % EVEN_MODULO != 0:
        win = [
            *_extract_layers(pictures.get("vertical_window_background")),
            *_extract_layers(pictures.get("straight_vertical_window")),
        ]
        if win:
            return win
    if sprite_key == "straight_horizontal" and coord[0] % EVEN_MODULO != 0:
        win = [
            *_extract_layers(pictures.get("horizontal_window_background")),
            *_extract_layers(pictures.get("straight_horizontal_window")),
        ]
        if win:
            return win
    return _extract_layers(pictures.get(sprite_key))


def _combinator_symbol_specs(
    proto: Mapping[str, object],
    ent: Mapping[str, object],
    direction: int,
    op_map: Mapping[str, str],
    cond_key: str,
    default_op: str,
) -> list[SpriteSpec]:
    """Extract the directional operator symbol sprite for an arithmetic or decider combinator."""
    cb = _as_map(ent.get("control_behavior"))
    cond = _as_map(cb.get(cond_key))
    op = _as_str(cond.get("operation") or cond.get("comparator"), default_op)
    sprite_key = op_map.get(op)
    if not sprite_key:
        return []
    dir_name = DIR_NAMES.get(direction, "north")
    sym_map = _as_map(proto.get(sprite_key))
    return _extract_layers(sym_map.get(dir_name))


def _draw_underground_belt_specs(
    ent: Mapping[str, object],
    proto: Mapping[str, object],
    direction: int,
) -> list[SpriteSpec]:
    """Extract back patch, belt strip, hood structure, and front patch for an underground belt."""
    is_input = _as_str(ent.get("type"), "input") == "input"
    eff_dir = direction if is_input else OPPOSITE_DIR.get(direction, direction)
    eff_idx = (eff_dir // DIR_STEP_DIVISOR) % QUAD_LEN
    bas = _as_map(proto.get("belt_animation_set"))
    belt_spec = _belt_animation_spec(bas, direction, "straight")
    structure = _as_map(proto.get("structure"))
    back = _extract_layers(structure.get("back_patch"), frame_x=eff_idx)
    hood_node = structure.get("direction_in" if is_input else "direction_out")
    hood = _extract_layers(hood_node, frame_x=eff_idx)
    front = _extract_layers(structure.get("front_patch"), frame_x=eff_idx)
    return [*back, *([belt_spec] if belt_spec else []), *hood, *front]


def _draw_splitter_specs(proto: Mapping[str, object], direction: int) -> list[SpriteSpec]:
    """Extract the two parallel belt strips and directional housing for a splitter."""
    bas = _as_map(proto.get("belt_animation_set"))
    off0 = _rotate_point((-HALF_RATIO, 0.0), direction)
    off1 = _rotate_point((HALF_RATIO, 0.0), direction)
    b0 = _belt_animation_spec(bas, direction, "straight", extra_shift=off0)
    b1 = _belt_animation_spec(bas, direction, "straight", extra_shift=off1)
    patch = _extract_directional_layers(proto.get("structure_patch"), direction)
    struct = _extract_directional_layers(proto.get("structure"), direction)
    return [*([b0] if b0 else []), *([b1] if b1 else []), *patch, *struct]


def _draw_crafting_machine_specs(proto: Mapping[str, object], direction: int) -> list[SpriteSpec]:
    """Extract idle or directional animation layers for an assembling machine or furnace."""
    gset = _as_map(proto.get("graphics_set"))
    if gset.get("always_draw_idle_animation") and "idle_animation" in gset:
        return _extract_directional_layers(gset.get("idle_animation"), direction)
    return _extract_directional_layers(gset.get("animation"), direction)


def _draw_cargo_landing_pad_specs(proto: Mapping[str, object]) -> list[SpriteSpec]:
    """Extract base or wall connection layers for a cargo landing pad."""
    gset = _as_map(proto.get("graphics_set"))
    pic_layers = _extract_layers(gset.get("picture"))
    if pic_layers:
        return pic_layers
    conns = _as_map(gset.get("connections"))
    return [
        spec
        for key in LANDING_PAD_WALL_KEYS
        for spec in _extract_layers(_as_map(conns.get(key)).get("layers"))
    ]


@dataclass(frozen=True)
class EntityDrawContext:
    """Contextual state passed to per-category entity sprite extractors."""

    ent: Mapping[str, object]
    proto: Mapping[str, object]
    direction: int
    coord: CoordPair
    belt_grid: Mapping[CoordPair, tuple[int, str, str]]
    fluid_conns: Mapping[CoordPair, set[int]]


def _specs_transport_belt(ctx: EntityDrawContext) -> list[SpriteSpec]:
    bas = _as_map(ctx.proto.get("belt_animation_set"))
    shape = _belt_curve_shape(ctx.coord, ctx.direction, ctx.belt_grid)
    spec = _belt_animation_spec(bas, ctx.direction, shape)
    return [spec] if spec is not None else []


def _specs_inserter(ctx: EntityDrawContext) -> list[SpriteSpec]:
    opp_idx = (OPPOSITE_DIR.get(ctx.direction, DIRECTION_SOUTH) // DIR_STEP_DIVISOR) % QUAD_LEN
    return _extract_layers(ctx.proto.get("platform_picture"), frame_x=opp_idx)


def _specs_electric_pole(ctx: EntityDrawContext) -> list[SpriteSpec]:
    dir_idx = (ctx.direction // DIR_STEP_DIVISOR) % QUAD_LEN
    return _extract_layers(ctx.proto.get("pictures"), frame_x=dir_idx)


def _specs_boiler(ctx: EntityDrawContext) -> list[SpriteSpec]:
    dir_name = DIR_NAMES.get(ctx.direction, "north")
    pics = _as_map(_as_map(ctx.proto.get("pictures")).get(dir_name))
    return _extract_layers(pics.get("structure"))


def _specs_generator(ctx: EntityDrawContext) -> list[SpriteSpec]:
    is_vert = ctx.direction in NORTH_SOUTH_DIRS
    anim_key = "vertical_animation" if is_vert else "horizontal_animation"
    direct = _extract_layers(ctx.proto.get(anim_key))
    if direct:
        return direct
    pics = _as_map(ctx.proto.get("pictures"))
    orient_key = "north" if is_vert else "east"
    return _extract_layers(pics.get(orient_key))


def _specs_arithmetic_combinator(ctx: EntityDrawContext) -> list[SpriteSpec]:
    base = _extract_directional_layers(ctx.proto.get("sprites"), ctx.direction)
    syms = _combinator_symbol_specs(
        ctx.proto, ctx.ent, ctx.direction, ARITHMETIC_OP_KEYS, "arithmetic_conditions", "*"
    )
    return [*base, *syms]


def _specs_decider_combinator(ctx: EntityDrawContext) -> list[SpriteSpec]:
    base = _extract_directional_layers(ctx.proto.get("sprites"), ctx.direction)
    syms = _combinator_symbol_specs(
        ctx.proto, ctx.ent, ctx.direction, DECIDER_OP_KEYS, "decider_conditions", "<"
    )
    return [*base, *syms]


CATEGORY_SPRITE_BUILDERS: Mapping[str, Callable[[EntityDrawContext], list[SpriteSpec]]] = {
    "transport-belt": _specs_transport_belt,
    "underground-belt": lambda ctx: _draw_underground_belt_specs(ctx.ent, ctx.proto, ctx.direction),
    "splitter": lambda ctx: _draw_splitter_specs(ctx.proto, ctx.direction),
    "pipe": lambda ctx: _pipe_specs_for_entity(ctx.proto, ctx.coord, ctx.fluid_conns),
    "pipe-to-ground": lambda ctx: _extract_directional_layers(
        ctx.proto.get("pictures"), ctx.direction
    ),
    "pump": lambda ctx: _extract_directional_layers(ctx.proto.get("animations"), ctx.direction),
    "inserter": _specs_inserter,
    "electric-pole": _specs_electric_pole,
    "assembling-machine": lambda ctx: _draw_crafting_machine_specs(ctx.proto, ctx.direction),
    "furnace": lambda ctx: _draw_crafting_machine_specs(ctx.proto, ctx.direction),
    "boiler": _specs_boiler,
    "generator": _specs_generator,
    "lab": lambda ctx: _extract_layers(ctx.proto.get("off_animation")),
    "radar": lambda ctx: _extract_layers(ctx.proto.get("pictures")),
    "rocket-silo": lambda ctx: [
        spec for key in ROCKET_SILO_LAYER_KEYS for spec in _extract_layers(ctx.proto.get(key))
    ],
    "cargo-landing-pad": lambda ctx: _draw_cargo_landing_pad_specs(ctx.proto),
    "arithmetic-combinator": _specs_arithmetic_combinator,
    "decider-combinator": _specs_decider_combinator,
    "constant-combinator": lambda ctx: _extract_directional_layers(
        ctx.proto.get("sprites"), ctx.direction
    ),
    "display-panel": lambda ctx: _extract_directional_layers(
        ctx.proto.get("sprites"), ctx.direction
    ),
    "power-switch": lambda ctx: [
        *_extract_layers(ctx.proto.get("power_on_animation")),
        *_extract_layers(ctx.proto.get("led_off")),
    ],
    "lamp": lambda ctx: _extract_layers(ctx.proto.get("picture_off")),
}


def _entity_sprite_specs(
    ent: Mapping[str, object],
    cat: str,
    proto: Mapping[str, object],
    belt_grid: Mapping[CoordPair, tuple[int, str, str]],
    fluid_conns: Mapping[CoordPair, set[int]],
) -> list[SpriteSpec]:
    """Resolve the ordered list of `SpriteSpec` layers for a blueprint entity."""
    direction = _as_int(ent.get("direction"), DIRECTION_NORTH)
    pos = _as_map(ent.get("position"))
    coord = (math.floor(_as_float(pos.get("x"))), math.floor(_as_float(pos.get("y"))))
    ctx = EntityDrawContext(
        ent=ent,
        proto=proto,
        direction=direction,
        coord=coord,
        belt_grid=belt_grid,
        fluid_conns=fluid_conns,
    )
    builder = CATEGORY_SPRITE_BUILDERS.get(cat)
    if builder is not None:
        return builder(ctx)

    for fallback_key in ("picture", "pictures", "animation", "animations", "sprites"):
        if fallback_key in proto:
            return _extract_directional_layers(proto[fallback_key], direction)
    return []


def _fallback_color_for(name: str, cat: str) -> RgbaColor:
    """Return the geometric fallback fill color for an entity category or name."""
    if cat in FALLBACK_FILL_COLORS:
        return FALLBACK_FILL_COLORS[cat]
    for key, color in FALLBACK_FILL_COLORS.items():
        if key in name:
            return color
    return DEFAULT_ENTITY_FILL


def _draw_fallback_entity(
    draw: ImageDraw.ImageDraw,
    viewport: Viewport,
    ent: Mapping[str, object],
    cat: str,
) -> None:
    """Draw a clean geometric box and orientation marker when game sprites are unavailable."""
    name = _as_str(ent.get("name"))
    pos = _as_map(ent.get("position"))
    ex, ey = _as_float(pos.get("x")), _as_float(pos.get("y"))
    direction = _as_int(ent.get("direction"), DIRECTION_NORTH)
    w, h = entity_size(name, direction)
    x0, y0 = viewport.to_px(ex - w * HALF_RATIO, ey - h * HALF_RATIO)
    x1, y1 = viewport.to_px(ex + w * HALF_RATIO, ey + h * HALF_RATIO)
    fill = _fallback_color_for(name, cat)
    draw.rectangle(
        [round(x0) + 1, round(y0) + 1, round(x1) - 1, round(y1) - 1],
        fill=fill,
        outline=ENTITY_OUTLINE_COLOR,
        width=1,
    )
    if cat in ("transport-belt", "underground-belt", "splitter", "inserter"):
        cx, cy = viewport.to_px(ex, ey)
        dx, dy = DIR_STEPS.get(direction, (0, -1))
        tip_x = cx + dx * viewport.tile_px * ARROW_TIP_RATIO
        tip_y = cy + dy * viewport.tile_px * ARROW_TIP_RATIO
        draw.line(
            [(round(cx), round(cy)), (round(tip_x), round(tip_y))],
            fill=BELT_ARROW_COLOR,
            width=max(1, viewport.tile_px // ARROW_WIDTH_DIVISOR),
        )


def _draw_inserter_arm(
    draw: ImageDraw.ImageDraw,
    viewport: Viewport,
    ent: Mapping[str, object],
) -> None:
    """Draw a directional arm and hand indicator for an inserter entity."""
    pos = _as_map(ent.get("position"))
    ex, ey = _as_float(pos.get("x")), _as_float(pos.get("y"))
    direction = _as_int(ent.get("direction"), DIRECTION_NORTH)
    dx, dy = DIR_STEPS.get(direction, (0, -1))
    cx, cy = viewport.to_px(ex, ey)
    src_x, src_y = viewport.to_px(ex + dx * INSERTER_ARM_RATIO, ey + dy * INSERTER_ARM_RATIO)
    dst_x, dst_y = viewport.to_px(ex - dx * INSERTER_ARM_RATIO, ey - dy * INSERTER_ARM_RATIO)
    arm_w = max(1, viewport.tile_px // INSERTER_ARM_WIDTH_DIVISOR)
    draw.line(
        [(round(src_x), round(src_y)), (round(dst_x), round(dst_y))],
        fill=BELT_ARROW_COLOR,
        width=arm_w,
    )
    r = max(1, viewport.tile_px // INSERTER_HUB_DIVISOR)
    draw.ellipse(
        [round(cx) - r, round(cy) - r, round(cx) + r, round(cy) + r], fill=ENTITY_OUTLINE_COLOR
    )


COMBINATOR_COND_KEYS: Mapping[str, str] = {
    "arithmetic-combinator": "arithmetic_conditions",
    "decider-combinator": "decider_conditions",
}


def _display_panel_icon(ent: Mapping[str, object]) -> str:
    """Extract the signal icon name configured on a display panel entity."""
    icon_name = _as_str(_as_map(ent.get("icon")).get("name"))
    if icon_name:
        return icon_name
    params = _as_seq(_as_map(ent.get("control_behavior")).get("parameters"))
    return _as_str(_as_map(_as_map(params[0]).get("icon")).get("name")) if params else ""


def _combinator_output_icon(ent: Mapping[str, object], cond_key: str) -> str:
    """Extract the output signal icon name from a combinator entity."""
    cond = _as_map(_as_map(ent.get("control_behavior")).get(cond_key))
    return _as_str(_as_map(cond.get("output_signal")).get("name"))


def _extract_entity_icons(ent: Mapping[str, object], cat: str) -> list[tuple[str, FloatPair]]:
    """Determine the Alt-mode icon names and tile offsets for a blueprint entity."""
    recipe = _as_str(ent.get("recipe"))
    if recipe:
        return [(recipe, (0.0, 0.0))]

    if cat == "display-panel":
        panel_icon = _display_panel_icon(ent)
        return [(panel_icon, (0.0, DISPLAY_PANEL_ICON_OFFSET_Y))] if panel_icon else []

    if cat == "inserter":
        filters = _as_seq(ent.get("filters"))
        first_name = _as_str(_as_map(filters[0]).get("name")) if filters else ""
        return [(first_name, (0.0, 0.0))] if first_name else []

    cond_key = COMBINATOR_COND_KEYS.get(cat)
    if cond_key is not None:
        out_sig = _combinator_output_icon(ent, cond_key)
        return [(out_sig, (0.0, COMBINATOR_ICON_OFFSET_Y))] if out_sig else []

    return []


def _draw_alt_icon_badge(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    atlas: SpriteAtlas,
    viewport: Viewport,
    target_pos: FloatPair,
    icon_info: tuple[str, int],
    scaled_bg: SpriteSpec | None,
) -> None:
    """Composite a single Alt-mode dark background badge and icon at `target_pos`."""
    if scaled_bg is None or not _paste_spec(canvas, atlas, viewport, target_pos, scaled_bg):
        px, py = viewport.to_px(target_pos[0], target_pos[1])
        r = max(PAIR_LEN, round(viewport.tile_px * ALT_BG_RADIUS_RATIO))
        draw.rounded_rectangle(
            [round(px) - r, round(py) - r, round(px) + r, round(py) + r],
            radius=ALT_BG_CORNER_RADIUS,
            fill=ICON_BG_FALLBACK_COLOR,
        )

    icon_file, icon_size = icon_info
    icon_spec = SpriteSpec(
        filename=icon_file,
        crop_x=0,
        crop_y=0,
        width=icon_size,
        height=icon_size,
        scale=ALT_ICON_SCALE * (ICON_RAW_SIZE / max(1, icon_size)),
    )
    _paste_spec(canvas, atlas, viewport, target_pos, icon_spec)


def _render_alt_overlays(
    canvas: Image.Image,
    atlas: SpriteAtlas,
    viewport: Viewport,
    entities: Sequence[Mapping[str, object]],
) -> None:
    """Render Alt-mode recipe and signal icons on top of placed entities."""
    util_sprites = _as_map(_as_map(atlas.prototypes.get("utility-sprites")).get("default"))
    bg_raw = _sprite_spec_from_dict(_as_map(util_sprites.get("entity_info_dark_background")))
    scaled_bg = (
        SpriteSpec(
            filename=bg_raw.filename,
            crop_x=bg_raw.crop_x,
            crop_y=bg_raw.crop_y,
            width=bg_raw.width,
            height=bg_raw.height,
            scale=ALT_ICON_BG_SCALE,
        )
        if bg_raw is not None
        else None
    )
    draw = ImageDraw.Draw(canvas)

    for ent in entities:
        name = _as_str(ent.get("name"))
        cat, proto = atlas.entity_lookup.get(name, ("", {}))
        pos = _as_map(ent.get("position"))
        ex, ey = _as_float(pos.get("x")), _as_float(pos.get("y"))
        spec_shift = _parse_shift(_as_map(proto.get("icon_draw_specification")).get("shift"))

        for icon_name, rel_offset in _extract_entity_icons(ent, cat):
            icon_info = atlas.icon_lookup.get(icon_name)
            if icon_info is not None:
                target_pos = (
                    ex + spec_shift[0] + rel_offset[0],
                    ey + spec_shift[1] + rel_offset[1],
                )
                _draw_alt_icon_badge(
                    canvas, draw, atlas, viewport, target_pos, icon_info, scaled_bg
                )


def _wire_color_and_side(connector_id: int) -> tuple[str, int]:
    """Map a Factorio 2.0 `wire_connector_id` integer to `(color, side_index)`."""
    if connector_id in (WIRE_ID_CIRCUIT_RED, WIRE_ID_COMBINATOR_IN_RED):
        return ("red", 1)
    if connector_id in (WIRE_ID_CIRCUIT_GREEN, WIRE_ID_COMBINATOR_IN_GREEN):
        return ("green", 1)
    if connector_id == WIRE_ID_COMBINATOR_OUT_RED:
        return ("red", PAIR_LEN)
    if connector_id == WIRE_ID_COMBINATOR_OUT_GREEN:
        return ("green", PAIR_LEN)
    if connector_id == WIRE_ID_SWITCH_RIGHT:
        return ("copper", PAIR_LEN)
    return ("copper", 1)


def _lookup_wire_point(
    conn_pts: Sequence[object],
    dir_idx: int,
    color: str,
) -> FloatPair | None:
    """Look up a directional `(x, y)` wire offset from a `connection_points` sequence."""
    if not conn_pts:
        return None
    pt = _as_map(conn_pts[dir_idx % len(conn_pts)])
    wire = _as_map(pt.get("wire"))
    return _parse_shift(wire[color]) if color in wire else None


def _power_switch_wire_offset(proto: Mapping[str, object], side: int) -> FloatPair:
    """Resolve the copper wire connection point offset for a power switch."""
    key = "left_wire_connection_point" if side == 1 else "right_wire_connection_point"
    wire = _as_map(_as_map(proto.get(key)).get("wire"))
    if "copper" in wire:
        return _parse_shift(wire["copper"])
    return (-SWITCH_COPPER_OFFSET_X if side == 1 else SWITCH_COPPER_OFFSET_X, 0.0)


def _wire_attachment_offset(
    cat: str,
    proto: Mapping[str, object],
    direction: int,
    color: str,
    side: int,
) -> FloatPair:
    """Resolve the wire connection point offset in tiles for an entity."""
    dir_idx = (direction // DIR_STEP_DIVISOR) % QUAD_LEN
    if cat == "electric-pole":
        found = _lookup_wire_point(_as_seq(proto.get("connection_points")), dir_idx, color)
        return (
            found
            if found is not None
            else POLE_DEFAULT_WIRE_OFFSETS.get(color, (0.0, POLE_COPPER_OFFSET_Y))
        )

    if cat in COMBINATOR_COND_KEYS:
        key = "input_connection_points" if side == 1 else "output_connection_points"
        found = _lookup_wire_point(_as_seq(proto.get(key)), dir_idx, color)
        if found is not None:
            return found
        dy = COMBINATOR_CONN_OFFSET if side == 1 else -COMBINATOR_CONN_OFFSET
        return _rotate_point((0.0, dy), direction)

    if cat == "power-switch" and color == "copper":
        return _power_switch_wire_offset(proto, side)

    return (0.0, 0.0)


def _render_wires(
    canvas: Image.Image,
    atlas: SpriteAtlas,
    viewport: Viewport,
    entities: Sequence[Mapping[str, object]],
    wires: Sequence[object],
) -> None:
    """Render copper, red, and green wires with parabolic catenary sag."""
    if not wires:
        return
    ent_by_num = {
        _as_int(ent.get("entity_number"), idx): ent for idx, ent in enumerate(entities, start=1)
    }

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    line_w = max(1, min(WIRE_WIDTH_PX, viewport.tile_px // WIRE_TILE_PX_DIVISOR))

    for raw_wire in wires:
        w_seq = _as_seq(raw_wire)
        if len(w_seq) < QUAD_LEN:
            continue
        ent1 = ent_by_num.get(_as_int(w_seq[0]))
        ent2 = ent_by_num.get(_as_int(w_seq[PAIR_LEN]))
        if ent1 is None or ent2 is None:
            continue

        color, s1 = _wire_color_and_side(_as_int(w_seq[1]))
        _, s2 = _wire_color_and_side(_as_int(w_seq[WIRE_DST_ID_IDX]))
        cat1, proto1 = atlas.entity_lookup.get(_as_str(ent1.get("name")), ("", {}))
        cat2, proto2 = atlas.entity_lookup.get(_as_str(ent2.get("name")), ("", {}))
        off1 = _wire_attachment_offset(
            cat1, proto1, _as_int(ent1.get("direction"), DIRECTION_NORTH), color, s1
        )
        off2 = _wire_attachment_offset(
            cat2, proto2, _as_int(ent2.get("direction"), DIRECTION_NORTH), color, s2
        )

        p1, p2 = _as_map(ent1.get("position")), _as_map(ent2.get("position"))
        x1, y1 = _as_float(p1.get("x")) + off1[0], _as_float(p1.get("y")) + off1[1]
        x2, y2 = _as_float(p2.get("x")) + off2[0], _as_float(p2.get("y")) + off2[1]

        sag = min(WIRE_MAX_SAG_TILES, math.hypot(x2 - x1, y2 - y1) * WIRE_SAG_SCALE)
        pts = [
            (
                round(viewport.to_px(x1 + (x2 - x1) * (s / WIRE_CURVE_STEPS), 0.0)[0]),
                round(
                    viewport.to_px(
                        0.0,
                        y1
                        + (y2 - y1) * (s / WIRE_CURVE_STEPS)
                        + WIRE_PARABOLA_FACTOR
                        * sag
                        * (s / WIRE_CURVE_STEPS)
                        * (1.0 - s / WIRE_CURVE_STEPS),
                    )[1]
                ),
            )
            for s in range(WIRE_CURVE_STEPS + 1)
        ]
        draw.line(pts, fill=WIRE_COLORS.get(color, WIRE_COLORS["copper"]), width=line_w)

    canvas.alpha_composite(overlay)


def render_blueprint(bp_input: str | Path, out_path: str | Path) -> Path:
    """Render a blueprint exchange string or file to a PNG image.

    Args:
        bp_input: The Factorio blueprint exchange string or file path.
        out_path: The target file path for the output PNG image.

    Returns:
        The resolved path to the generated PNG image.
    """
    bp_data = _load_blueprint_payload(bp_input)
    prototypes = load_game_prototypes()
    if prototypes:
        register_prototype_boxes(prototypes)

    atlas = SpriteAtlas(data_dir=_find_factorio_data_dir(), prototypes=prototypes)
    entities = [_as_map(e) for e in _as_seq(bp_data.get("entities"))]
    tiles = [_as_map(t) for t in _as_seq(bp_data.get("tiles"))]
    wires = _as_seq(bp_data.get("wires"))

    viewport = _compute_viewport(entities, tiles)
    canvas = Image.new("RGBA", (viewport.width_px, viewport.height_px), BG_COLOR)
    _draw_background_grid(canvas, viewport)
    _render_tiles(canvas, atlas, viewport, tiles)

    belt_grid = {coord: cell for ent in entities for coord, cell in _belt_grid_cells(ent)}
    fluid_conns = _build_fluid_connection_points(entities, atlas)

    sorted_entities = sorted(
        entities,
        key=lambda e: (
            ENTITY_LAYER_RANKS.get(
                atlas.entity_lookup.get(_as_str(e.get("name")), ("", {}))[0],
                RANK_DEFAULT_ENTITY,
            ),
            _as_float(_as_map(e.get("position")).get("y")),
            _as_float(_as_map(e.get("position")).get("x")),
        ),
    )

    draw = ImageDraw.Draw(canvas)
    for ent in sorted_entities:
        name = _as_str(ent.get("name"))
        cat, proto = atlas.entity_lookup.get(name, ("", {}))
        pos = _as_map(ent.get("position"))
        ent_pos = (_as_float(pos.get("x")), _as_float(pos.get("y")))
        specs = _entity_sprite_specs(ent, cat, proto, belt_grid, fluid_conns)
        pasted_count = sum(
            1 for spec in specs if _paste_spec(canvas, atlas, viewport, ent_pos, spec)
        )
        if pasted_count == 0:
            _draw_fallback_entity(draw, viewport, ent, cat)
        elif cat == "inserter":
            _draw_inserter_arm(draw, viewport, ent)

    _render_alt_overlays(canvas, atlas, viewport, sorted_entities)
    _render_wires(canvas, atlas, viewport, entities, wires)

    out_file = Path(out_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_file, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
    return out_file


def main() -> int:
    """Command-line entry point to render blueprint files to PNG images."""
    parser = argparse.ArgumentParser(description="Render Factorio blueprint to PNG.")
    parser.add_argument("blueprint", help="Path to blueprint file or raw blueprint string")
    parser.add_argument("-o", "--output", required=True, help="Output PNG file path")
    parser.add_argument("--time-out", help="Optional output JSON file path for render duration")
    args = parser.parse_args()

    t0 = time.perf_counter()
    out = render_blueprint(args.blueprint, args.output)
    t1 = time.perf_counter()
    elapsed = round(t1 - t0, ELAPSED_ROUND_DIGITS)

    if args.time_out:
        out_time = Path(args.time_out)
        out_time.parent.mkdir(parents=True, exist_ok=True)
        time_data = {
            "stage": "render",
            "elapsed_seconds": elapsed,
        }
        out_time.write_text(json.dumps(time_data, indent=2) + "\n", encoding="utf-8")

    print(f"factorilog: rendered blueprint to {out} in {elapsed}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
