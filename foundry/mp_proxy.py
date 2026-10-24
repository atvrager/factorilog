"""Factorio UDP multiplayer protocol dissector and lockstep proxy.

Parses NetworkMessageHeader, ClientToServerHeartbeat, ServerToClientHeartbeat,
TickClosure, and InputAction records on the UDP wire.
All docstrings and comments follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import json
import math
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

MSG_PING = 0
MSG_PING_REPLY = 1
MSG_CONNECTION_REQUEST = 2
MSG_CONNECTION_REQUEST_REPLY = 3
MSG_CONNECTION_REQUEST_REPLY_CONFIRM = 4
MSG_CONNECTION_ACCEPT_OR_DENY = 5
MSG_CLIENT_TO_SERVER_HEARTBEAT = 6
MSG_SERVER_TO_CLIENT_HEARTBEAT = 7
MSG_TRANSFER_BLOCK_REQUEST = 12
MSG_TRANSFER_BLOCK = 13

ALWAYS_HAS_MSG_ID = frozenset({MSG_CONNECTION_REQUEST, MSG_CONNECTION_REQUEST_REPLY_CONFIRM})

HEADER_TYPE_MASK = 0x1F
HEADER_FRAGMENTED_MASK = 0x40
HEADER_LAST_FRAGMENT_MASK = 0x80
CONFIRM_BIT_MASK = 1 << 15
MSG_ID_MASK = (1 << 15) - 1

HB_HAS_REQUESTS = 0x01
HB_HAS_TICK_CLOSURES = 0x02
HB_HAS_SINGLE_TICK_CLOSURE = 0x04
HB_ALL_TICK_CLOSURES_EMPTY = 0x08
HB_HAS_SYNCHRONIZER_ACTION = 0x10

VARINT_EXTENDED = 0xFF
UINT16_MASK = (1 << 16) - 1
UINT32_MASK = (1 << 32) - 1
PLAYER_INDEX_INIT = (1 << 16) - 1
PLAYER_INDEX_MASK = (1 << 16) - 1
MAX_UDP_PAYLOAD = (1 << 16) - 1
FIXED_POINT_SCALE = 256

ACTION_NOTHING = 0x0000
ACTION_STOP_WALKING = 0x0001
ACTION_BEGIN_MINING = 0x0002
ACTION_STOP_MINING = 0x0003
ACTION_OPEN_GUI = 0x0005
ACTION_SELECTED_ENTITY_CLEARED = 0x0009
ACTION_CLEAR_CURSOR = 0x000A
ACTION_RESET_ASSEMBLING_MACHINE = 0x000B
ACTION_CLOSE_GUI = 0x003D
ACTION_BUILD = 0x0045
ACTION_START_WALKING = 0x0046
ACTION_BEGIN_MINING_TERRAIN = 0x0047
ACTION_CRAFT = 0x0056
ACTION_SETUP_ASSEMBLING_MACHINE = 0x0059
ACTION_SELECTED_ENTITY_CHANGED = 0x005A
ACTION_PIPETTE = 0x005B
ACTION_START_RESEARCH = 0x0064
ACTION_SELECT_ENTITY_VERY_CLOSE = 0x010A
ACTION_SELECT_ENTITY_VERY_CLOSE_PRECISE = 0x010B
ACTION_SELECT_ENTITY_RELATIVE = 0x010C
ACTION_SELECT_ENTITY_UNIT_NUMBER = 0x010D
ACTION_FAST_ENTITY_TRANSFER = 0x0117
ACTION_ROTATE_ENTITY = 0x0118
ACTION_FLIP_ENTITY = 0x0119
ACTION_FAST_ENTITY_SPLIT = 0x011A
ACTION_ACHIEVEMENT_GAINED = 0x0159
ACTION_CHECK_CRC_HEURISTIC = 0x015D

KNOWN_ACTION_PAYLOAD_SIZES: dict[int, int] = {
    **dict.fromkeys(range(0x0000, 0x003D), 0),
    ACTION_CLOSE_GUI: 2,
    0x003E: 1,
    0x003F: 1,
    0x0041: 3,
    0x0042: 1,
    0x0043: 2,
    0x0044: 8,
    ACTION_BUILD: 12,
    ACTION_START_WALKING: 16,
    ACTION_BEGIN_MINING_TERRAIN: 8,
    0x0048: 2,
    0x0049: 16,
    0x004B: 2,
    0x004E: 8,
    0x0055: 12,
    ACTION_CRAFT: 6,
    0x0057: 8,
    0x0058: 9,
    ACTION_SETUP_ASSEMBLING_MACHINE: 3,
    ACTION_SELECTED_ENTITY_CHANGED: 8,
    ACTION_PIPETTE: 10,
    0x005E: 6,
    0x0060: 1,
    0x0061: 12,
    ACTION_START_RESEARCH: 2,
    0x0065: 1,
    0x0067: 5,
    0x0068: 2,
    0x0069: 17,
    0x006A: 17,
    0x006C: 8,
    0x006E: 1,
    0x006F: 1,
    0x0070: 1,
    0x0072: 17,
    0x0073: 21,
    0x0074: 21,
    0x0075: 25,
    0x0076: 18,
    0x0077: 25,
    0x0078: 19,
    0x007B: 9,
    0x007C: 9,
    0x007D: 8,
    0x007E: 9,
    0x0080: 8,
    0x0081: 24,
    0x0083: 8,
    0x0084: 23,
    0x0085: 23,
    0x0086: 22,
    0x0087: 22,
    0x0088: 22,
    0x0089: 22,
    0x008F: 6,
    0x0090: 6,
    0x0092: 6,
    0x0095: 2,
    0x009B: 8,
    0x009D: 6,
    0x009E: 3,
    0x00A0: 1,
    0x00A1: 23,
    0x00A2: 22,
    0x00A4: 8,
    0x00A7: 4,
    0x00A8: 8,
    0x00AB: 4,
    0x00AC: 8,
    0x00AF: 4,
    0x00B3: 10,
    0x00B4: 2,
    0x00B5: 13,
    0x00B7: 8,
    0x00B8: 19,
    0x00BA: 13,
    0x00BB: 9,
    0x00BF: 9,
    0x00C2: 13,
    0x00C3: 6,
    0x00C4: 13,
    0x00CE: 6,
    0x00CF: 10,
    0x00DA: 9,
    0x00DB: 9,
    0x00E1: 8,
    0x00E4: 13,
    0x00E5: 13,
    0x00E6: 21,
    0x00E9: 4,
    0x00ED: 4,
    0x00EF: 4,
    0x00F4: 16,
    0x00F5: 5,
    0x00F6: 4,
    0x00F7: 2,
    0x00F8: 1,
    0x00FB: 6,
    0x0101: 4,
    0x0102: 4,
    0x0106: 2,
    0x0107: 4,
    0x0108: 2,
    0x0109: 1,
    ACTION_SELECT_ENTITY_VERY_CLOSE: 1,
    ACTION_SELECT_ENTITY_VERY_CLOSE_PRECISE: 2,
    ACTION_SELECT_ENTITY_RELATIVE: 4,
    ACTION_SELECT_ENTITY_UNIT_NUMBER: 8,
    0x010F: 1,
    0x0110: 1,
    0x0111: 1,
    0x0112: 1,
    0x0113: 1,
    0x0114: 1,
    0x0115: 1,
    0x0116: 1,
    ACTION_FAST_ENTITY_TRANSFER: 1,
    ACTION_ROTATE_ENTITY: 1,
    ACTION_FLIP_ENTITY: 1,
    ACTION_FAST_ENTITY_SPLIT: 1,
    0x011B: 1,
    0x011C: 1,
    0x011D: 1,
    0x011E: 1,
    0x011F: 1,
    0x0120: 1,
    0x0121: 1,
    0x0122: 10,
    0x0124: 4,
    0x0125: 1,
    0x0126: 1,
    0x0127: 1,
    0x0128: 4,
    0x0129: 4,
    0x012A: 4,
    0x012B: 1,
    0x012C: 1,
    0x012D: 2,
    0x012E: 1,
    0x012F: 1,
    0x0130: 8,
    0x0131: 1,
    0x0132: 4,
    0x0133: 8,
    0x0134: 1,
    0x0135: 9,
    0x0136: 1,
    0x0137: 1,
    0x0138: 4,
    0x013A: 4,
    0x013B: 2,
    0x013C: 4,
    0x013D: 1,
    0x013E: 1,
    0x0140: 2,
    0x0141: 4,
    0x0142: 1,
    0x0144: 1,
    0x0145: 1,
    0x0149: 8,
    0x014A: 2,
    0x0150: 4,
    0x0151: 8,
    0x0152: 4,
    0x0153: 4,
    0x0154: 4,
    0x0155: 14,
    0x0157: 2,
    0x0158: 1,
    ACTION_ACHIEVEMENT_GAINED: 2,
    0x015A: 2,
    0x015B: 2,
    ACTION_CHECK_CRC_HEURISTIC: 12,
    0x015F: 1,
    0x0161: 2,
    0x0162: 1,
    0x0163: 2,
    0x0164: 2,
}

DEFAULT_SUPPRESSED_CLIENT_ACTIONS: frozenset[int] = frozenset(
    {
        ACTION_STOP_WALKING,
        ACTION_STOP_MINING,
        ACTION_SELECTED_ENTITY_CLEARED,
        ACTION_SELECTED_ENTITY_CHANGED,
        ACTION_SELECT_ENTITY_VERY_CLOSE,
        ACTION_SELECT_ENTITY_VERY_CLOSE_PRECISE,
        ACTION_SELECT_ENTITY_RELATIVE,
        ACTION_SELECT_ENTITY_UNIT_NUMBER,
    }
)


def decode_uint16v(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a space-optimized uint16 from buf at pos."""
    if pos >= len(buf):
        raise ValueError("Buffer underflow in decode_uint16v")
    val = buf[pos]
    pos += 1
    if val == VARINT_EXTENDED:
        if pos + 2 > len(buf):
            raise ValueError("Buffer underflow in extended decode_uint16v")
        (val,) = struct.unpack_from("<H", buf, pos)
        pos += 2
    return val, pos


def encode_uint16v(val: int) -> bytes:
    """Encode an integer as a space-optimized uint16."""
    if val < VARINT_EXTENDED:
        return struct.pack("<B", val)
    return struct.pack("<BH", VARINT_EXTENDED, val & UINT16_MASK)


def decode_uint32v(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a space-optimized uint32 from buf at pos."""
    if pos >= len(buf):
        raise ValueError("Buffer underflow in decode_uint32v")
    val = buf[pos]
    pos += 1
    if val == VARINT_EXTENDED:
        if pos + 4 > len(buf):
            raise ValueError("Buffer underflow in extended decode_uint32v")
        (val,) = struct.unpack_from("<I", buf, pos)
        pos += 4
    return val, pos


def encode_uint32v(val: int) -> bytes:
    """Encode an integer as a space-optimized uint32."""
    if val < VARINT_EXTENDED:
        return struct.pack("<B", val)
    return struct.pack("<BI", VARINT_EXTENDED, val & UINT32_MASK)


@dataclass(frozen=True)
class NetworkHeader:
    """Parsed Factorio UDP packet header."""

    msg_type: int
    fragmented: bool
    last_fragment: bool
    message_id: int | None
    fragment_number: int | None
    payload_offset: int


def parse_network_header(buf: bytes) -> NetworkHeader:
    """Parse NetworkMessageHeader from the start of a UDP datagram."""
    if not buf:
        raise ValueError("Empty UDP packet")
    flags = buf[0]
    pos = 1
    msg_type = flags & HEADER_TYPE_MASK
    fragmented = bool(flags & HEADER_FRAGMENTED_MASK)
    last_fragment = bool(flags & HEADER_LAST_FRAGMENT_MASK)
    message_id: int | None = None
    fragment_number: int | None = None

    if fragmented or msg_type in ALWAYS_HAS_MSG_ID:
        if pos + 2 > len(buf):
            raise ValueError("Packet too short for message_id")
        (raw_id,) = struct.unpack_from("<H", buf, pos)
        pos += 2
        message_id = raw_id & MSG_ID_MASK
        has_confirm = bool(raw_id & CONFIRM_BIT_MASK)
        if fragmented:
            if pos >= len(buf):
                raise ValueError("Packet too short for fragment_number")
            fragment_number = buf[pos]
            pos += 1
        if has_confirm:
            if pos >= len(buf):
                raise ValueError("Packet too short for confirm_count")
            confirm_count = buf[pos]
            pos += 1 + confirm_count * 4
            if pos > len(buf):
                raise ValueError("Packet too short for confirm items")

    return NetworkHeader(
        msg_type=msg_type,
        fragmented=fragmented,
        last_fragment=last_fragment,
        message_id=message_id,
        fragment_number=fragment_number,
        payload_offset=pos,
    )


@dataclass(frozen=True)
class InputActionRecord:
    """Single InputAction decoded from a TickClosure."""

    action_type: int
    player_index: int
    payload: bytes


@dataclass(frozen=True)
class TickClosureRecord:
    """Single TickClosure decoded from a heartbeat packet."""

    update_tick: int
    actions: tuple[InputActionRecord, ...] = ()


def build_single_action_tick_closure(
    update_tick: int,
    action_type: int,
    player_index: int,
    action_payload: bytes,
) -> bytes:
    """Encode a TickClosure containing one InputAction."""
    header = struct.pack("<Q", update_tick)
    count_flagged = encode_uint32v(1 << 1)
    delta = (player_index - PLAYER_INDEX_INIT) & PLAYER_INDEX_MASK
    act_hdr = encode_uint16v(action_type) + encode_uint16v(delta)
    return header + count_flagged + act_hdr + action_payload


def to_fixed_coord(coord: float) -> int:
    """Convert a floating-point tile coordinate to 1/256 fixed-point integer."""
    return round(coord * FIXED_POINT_SCALE)


def build_stop_walking_action() -> tuple[int, bytes]:
    """Build StopWalking (0x0001) InputAction."""
    return ACTION_STOP_WALKING, b""


def build_begin_mining_action() -> tuple[int, bytes]:
    """Build BeginMining (0x0002) InputAction."""
    return ACTION_BEGIN_MINING, b""


def build_stop_mining_action() -> tuple[int, bytes]:
    """Build StopMining (0x0003) InputAction."""
    return ACTION_STOP_MINING, b""


def build_clear_cursor_action() -> tuple[int, bytes]:
    """Build ClearCursor (0x000a) InputAction."""
    return ACTION_CLEAR_CURSOR, b""


def build_reset_assembling_machine_action() -> tuple[int, bytes]:
    """Build ResetAssemblingMachine (0x000b) InputAction."""
    return ACTION_RESET_ASSEMBLING_MACHINE, b""


def build_open_gui_action() -> tuple[int, bytes]:
    """Build OpenGui (0x0005) InputAction to open the currently selected entity GUI."""
    return ACTION_OPEN_GUI, b""


def build_close_gui_action(flags: int = 0, play_sound: bool = False) -> tuple[int, bytes]:
    """Build CloseGui (0x003d) InputAction to close the currently opened GUI."""
    return ACTION_CLOSE_GUI, struct.pack("<BB", flags & 0xFF, int(play_sound))


def build_start_walking_action(dx: float, dy: float) -> tuple[int, bytes]:
    """Build StartWalking (0x0046) InputAction with 2D movement vector."""
    return ACTION_START_WALKING, struct.pack("<dd", dx, dy)


def build_begin_mining_terrain_action(x: float, y: float) -> tuple[int, bytes]:
    """Build BeginMiningTerrain (0x0047) InputAction at tile coordinates (x, y)."""
    return ACTION_BEGIN_MINING_TERRAIN, struct.pack("<ii", to_fixed_coord(x), to_fixed_coord(y))


def build_selected_entity_action(x: float, y: float) -> tuple[int, bytes]:
    """Build SelectedEntityChanged (0x005a) InputAction at tile coordinates (x, y)."""
    return ACTION_SELECTED_ENTITY_CHANGED, struct.pack("<ii", to_fixed_coord(x), to_fixed_coord(y))


def build_select_entity_unit_action(unit_number: int) -> tuple[int, bytes]:
    """Build SelectedEntityChangedBasedOnUnitNumber (0x010d) InputAction."""
    return ACTION_SELECT_ENTITY_UNIT_NUMBER, struct.pack("<Q", unit_number)


def build_craft_action(recipe_id: int, count: int = 1) -> tuple[int, bytes]:
    """Build Craft (0x0056) InputAction for recipe_id and count."""
    return ACTION_CRAFT, struct.pack("<HI", recipe_id & UINT16_MASK, count & UINT32_MASK)


def build_setup_assembling_machine_action(recipe_id: int, quality_id: int = 1) -> tuple[int, bytes]:
    """Build SetupAssemblingMachine (0x0059) InputAction."""
    return ACTION_SETUP_ASSEMBLING_MACHINE, struct.pack(
        "<HB", recipe_id & UINT16_MASK, quality_id & 0xFF
    )


def build_pipette_item_action(
    item_id: int,
    quality_id: int = 0,
    allow_ghost: bool = False,
    exact_quality: bool = False,
) -> tuple[int, bytes]:
    """Build Pipette (0x005b) InputAction to place an inventory item into the player cursor."""
    return ACTION_PIPETTE, struct.pack(
        "<BHHHBBB",
        0,
        item_id & UINT16_MASK,
        0,
        0,
        quality_id & 0xFF,
        int(allow_ghost),
        int(exact_quality),
    )


def build_place_entity_action(
    x: float,
    y: float,
    direction: int = 0,
    created_by_moving: int = 0,
    build_mode: int = 0,
    flags: int = 0,
) -> tuple[int, bytes]:
    """Build Build (0x0045) InputAction to place the cursor item at tile coordinates (x, y)."""
    return ACTION_BUILD, struct.pack(
        "<iiBBBB",
        to_fixed_coord(x),
        to_fixed_coord(y),
        direction & 0xFF,
        created_by_moving & 0xFF,
        build_mode & 0xFF,
        flags & 0xFF,
    )


def build_fast_entity_transfer_action(into_entity: bool) -> tuple[int, bytes]:
    """Build FastEntityTransfer (0x0117) InputAction to transfer a stack into or out of an entity."""
    return ACTION_FAST_ENTITY_TRANSFER, struct.pack("<B", int(into_entity))


def build_fast_entity_split_action(into_entity: bool) -> tuple[int, bytes]:
    """Build FastEntitySplit (0x011a) InputAction to transfer half a stack into or out of an entity."""
    return ACTION_FAST_ENTITY_SPLIT, struct.pack("<B", int(into_entity))


def build_rotate_entity_action(reverse: bool = False) -> tuple[int, bytes]:
    """Build RotateEntity (0x0118) InputAction to rotate the selected entity."""
    return ACTION_ROTATE_ENTITY, struct.pack("<B", int(reverse))


def build_start_research_action(tech_id: int) -> tuple[int, bytes]:
    """Build StartResearch (0x0064) InputAction for tech_id."""
    return ACTION_START_RESEARCH, struct.pack("<H", tech_id & UINT16_MASK)


def extract_lazy_bastard_limit(raw_data: Mapping[str, Any]) -> int:
    """Extract the maximum hand-crafted item count for lazy-bastard from prototype data."""
    table = raw_data.get("dont-craft-manually-achievement")
    if isinstance(table, Mapping):
        for entry in table.values():
            if isinstance(entry, Mapping) and "amount" in entry:
                return int(entry["amount"])
    raise KeyError("dont-craft-manually-achievement amount not found in prototype data")


def _try_parse_id_mapping_table(
    buf: bytes, start_pos: int
) -> tuple[dict[str, dict[str, int]], int] | None:
    """Attempt to parse one savedIDMapping table from buf at start_pos."""
    if start_pos + 2 > len(buf):
        return None
    (num_types,) = struct.unpack_from("<H", buf, start_pos)
    if num_types == 0 or num_types > 512:
        return None

    uint8_id_types = {"quality", "item-group", "airborne-pollutant"}
    pos = start_pos + 2
    table: dict[str, dict[str, int]] = {}
    total_entries = 0
    for _ in range(num_types):
        try:
            t_len, pos = decode_uint32v(buf, pos)
        except ValueError:
            return None
        if t_len == 0 or t_len > 128 or pos + t_len + 2 > len(buf):
            return None
        t_bytes = buf[pos : pos + t_len]
        if not all(97 <= b <= 122 or 48 <= b <= 57 or b == 45 for b in t_bytes):
            return None
        t_name = t_bytes.decode("ascii")
        pos += t_len

        (num_protos,) = struct.unpack_from("<H", buf, pos)
        pos += 2
        if num_protos > 8192:
            return None

        id_bytes = 1 if t_name in uint8_id_types else 2
        proto_map: dict[str, int] = {}
        for _ in range(num_protos):
            try:
                p_len, pos = decode_uint32v(buf, pos)
            except ValueError:
                return None
            if p_len == 0 or p_len > 256 or pos + p_len + id_bytes > len(buf):
                return None
            p_bytes = buf[pos : pos + p_len]
            if not all(32 <= b <= 126 for b in p_bytes):
                return None
            p_name = p_bytes.decode("utf-8", errors="strict")
            pos += p_len

            if id_bytes == 1:
                proto_id = buf[pos]
                pos += 1
            else:
                (proto_id,) = struct.unpack_from("<H", buf, pos)
                pos += 2
            if proto_id == 0:
                return None
            proto_map[p_name] = proto_id
            total_entries += 1

        table[t_name] = proto_map

    if total_entries == 0:
        return None
    return table, pos


def extract_save_id_mappings(save_zip_path: Path) -> dict[str, dict[str, int]]:
    """Extract runtime prototype name-to-ID mappings from a Factorio save archive."""
    raw_chunks: list[bytes] = []
    with zipfile.ZipFile(save_zip_path, "r") as zf:
        dat_names = sorted(
            name
            for name in zf.namelist()
            if name.endswith(".dat0") or name.endswith(".dat1")
            if "level.dat" in name
        )
        for name in dat_names:
            compressed = zf.read(name)
            try:
                raw_chunks.append(zlib.decompress(compressed))
            except zlib.error:
                continue

    stream = b"".join(raw_chunks)
    merged: dict[str, dict[str, int]] = {}
    pos = 0
    limit = len(stream) - 6
    while pos < limit:
        parsed = _try_parse_id_mapping_table(stream, pos)
        if parsed is not None:
            table, next_pos = parsed
            for t_name, proto_map in table.items():
                if t_name not in merged or len(proto_map) > len(merged[t_name]):
                    merged[t_name] = proto_map
            pos = next_pos
        else:
            pos += 1
    return merged


@dataclass(frozen=True)
class ResourcePatchTile:
    """Nearest hand-mineable resource entity reported by world telemetry."""

    name: str
    x: float
    y: float
    amount: int


@dataclass(frozen=True)
class ShorelineSite:
    """Validated lakeside placement coordinates for an offshore-pump steam factory complex."""

    pump_x: float
    pump_y: float
    pump_dir: int
    boiler_x: float
    boiler_y: float
    boiler_dir: int
    engine_x: float
    engine_y: float
    engine_dir: int
    pole_x: float
    pole_y: float
    asm_x: float
    asm_y: float
    furnace_x: float
    furnace_y: float
    stand_x: float
    stand_y: float
    furnace2_x: float = 0.0
    furnace2_y: float = 0.0
    furnace3_x: float = 0.0
    furnace3_y: float = 0.0
    lab_x: float = 0.0
    lab_y: float = 0.0


@dataclass(frozen=True)
class PlacedSurfaceEntity:
    """Placed player-force entity serialized from the live Factorio surface."""

    name: str
    x: float
    y: float
    direction: int = 0
    entity_type: str | None = None
    recipe: str | None = None


@dataclass(frozen=True)
class PlayerWorldState:
    """Snapshot of player position, activity, selection, inventory, and nearest resource."""

    tick: int
    player_x: float
    player_y: float
    walking: bool
    mining: bool
    selected_name: str | None
    selected_x: float | None
    selected_y: float | None
    inventory: dict[str, int]
    nearest_resource: ResourcePatchTile | None
    resources_by_name: dict[str, ResourcePatchTile] = field(default_factory=dict)
    shoreline_site: ShorelineSite | None = None
    crafting_queue_size: int = 0
    hand_crafted_count: int = 0
    machine_crafted_count: int = 0
    steam_energy_generated: float = 0.0
    steam_power_researched: bool = False
    electronics_researched: bool = False
    science_pack_researched: bool = False
    automation_researched: bool = False
    logistics_researched: bool = False
    electric_mining_drill_researched: bool = False
    drills_active: int = 0
    plates_made: int = 0
    asm_made: dict[str, int] = field(default_factory=dict)
    placed_entities: tuple[PlacedSurfaceEntity, ...] = ()


def read_world_state(state_path: Path) -> PlayerWorldState | None:
    """Read and parse the latest world state JSON written by the scenario telemetry hook."""
    if not state_path.is_file():
        return None
    try:
        raw_text = state_path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw_text:
            return None
        payload = json.loads(raw_text)
    except (OSError, json.JSONDecodeError):
        return None

    player = payload.get("player")
    if not isinstance(player, dict):
        return None

    inv_raw = player.get("inventory")
    inventory: dict[str, int] = {}
    if isinstance(inv_raw, dict):
        for k, v in inv_raw.items():
            if isinstance(k, str) and isinstance(v, (int, float)):
                inventory[k] = int(v)

    sel_raw = player.get("selected")
    sel_name: str | None = None
    sel_x: float | None = None
    sel_y: float | None = None
    if isinstance(sel_raw, dict):
        sel_name = str(sel_raw.get("name", "")) or None
        if "x" in sel_raw and "y" in sel_raw:
            sel_x = float(sel_raw["x"])
            sel_y = float(sel_raw["y"])

    res_raw = payload.get("nearest_resource")
    nearest: ResourcePatchTile | None = None
    if isinstance(res_raw, dict) and "name" in res_raw and "x" in res_raw and "y" in res_raw:
        nearest = ResourcePatchTile(
            name=str(res_raw["name"]),
            x=float(res_raw["x"]),
            y=float(res_raw["y"]),
            amount=int(res_raw.get("amount", 0)),
        )

    res_by_name_raw = payload.get("resources_by_name")
    resources_by_name: dict[str, ResourcePatchTile] = {}
    if isinstance(res_by_name_raw, dict):
        for rname, rval in res_by_name_raw.items():
            if isinstance(rname, str) and isinstance(rval, dict) and "x" in rval and "y" in rval:
                resources_by_name[rname] = ResourcePatchTile(
                    name=str(rval.get("name", rname)),
                    x=float(rval["x"]),
                    y=float(rval["y"]),
                    amount=int(rval.get("amount", 0)),
                )

    shore_raw = payload.get("shoreline_site")
    shoreline_site: ShorelineSite | None = None
    if isinstance(shore_raw, dict) and "pump_x" in shore_raw and "stand_x" in shore_raw:
        shoreline_site = ShorelineSite(
            pump_x=float(shore_raw["pump_x"]),
            pump_y=float(shore_raw["pump_y"]),
            pump_dir=int(shore_raw.get("pump_dir", 0)),
            boiler_x=float(shore_raw["boiler_x"]),
            boiler_y=float(shore_raw["boiler_y"]),
            boiler_dir=int(shore_raw.get("boiler_dir", 0)),
            engine_x=float(shore_raw["engine_x"]),
            engine_y=float(shore_raw["engine_y"]),
            engine_dir=int(shore_raw.get("engine_dir", 0)),
            pole_x=float(shore_raw["pole_x"]),
            pole_y=float(shore_raw["pole_y"]),
            asm_x=float(shore_raw["asm_x"]),
            asm_y=float(shore_raw["asm_y"]),
            furnace_x=float(shore_raw["furnace_x"]),
            furnace_y=float(shore_raw["furnace_y"]),
            stand_x=float(shore_raw["stand_x"]),
            stand_y=float(shore_raw["stand_y"]),
            furnace2_x=float(shore_raw.get("furnace2_x", 0.0)),
            furnace2_y=float(shore_raw.get("furnace2_y", 0.0)),
            furnace3_x=float(shore_raw.get("furnace3_x", 0.0)),
            furnace3_y=float(shore_raw.get("furnace3_y", 0.0)),
            lab_x=float(shore_raw.get("lab_x", 0.0)),
            lab_y=float(shore_raw.get("lab_y", 0.0)),
        )

    asm_raw = payload.get("asm_made")
    asm_made: dict[str, int] = {}
    if isinstance(asm_raw, dict):
        for rname, rcount in asm_raw.items():
            if isinstance(rname, str) and isinstance(rcount, (int, float)):
                asm_made[rname] = int(rcount)

    placed_raw = payload.get("placed_entities")
    placed_entities: list[PlacedSurfaceEntity] = []
    if isinstance(placed_raw, list):
        for ent in placed_raw:
            if isinstance(ent, dict) and "name" in ent and "x" in ent and "y" in ent:
                ug_val = ent.get("type")
                rec_val = ent.get("recipe")
                placed_entities.append(
                    PlacedSurfaceEntity(
                        name=str(ent["name"]),
                        x=float(ent["x"]),
                        y=float(ent["y"]),
                        direction=int(ent.get("direction", 0)),
                        entity_type=str(ug_val) if isinstance(ug_val, str) and ug_val else None,
                        recipe=str(rec_val) if isinstance(rec_val, str) and rec_val else None,
                    )
                )

    return PlayerWorldState(
        tick=int(payload.get("tick", 0)),
        player_x=float(player.get("x", 0.0)),
        player_y=float(player.get("y", 0.0)),
        walking=bool(player.get("walking", False)),
        mining=bool(player.get("mining", False)),
        selected_name=sel_name,
        selected_x=sel_x,
        selected_y=sel_y,
        inventory=inventory,
        nearest_resource=nearest,
        resources_by_name=resources_by_name,
        shoreline_site=shoreline_site,
        crafting_queue_size=int(player.get("crafting_queue_size", 0)),
        hand_crafted_count=int(player.get("hand_crafted_count", 0)),
        machine_crafted_count=int(player.get("machine_crafted_count", 0)),
        steam_energy_generated=float(player.get("steam_energy_generated", 0.0)),
        steam_power_researched=bool(player.get("steam_power_researched", False)),
        electronics_researched=bool(player.get("electronics_researched", False)),
        science_pack_researched=bool(player.get("science_pack_researched", False)),
        automation_researched=bool(player.get("automation_researched", False)),
        logistics_researched=bool(player.get("logistics_researched", False)),
        electric_mining_drill_researched=bool(
            player.get("electric_mining_drill_researched", False)
        ),
        drills_active=int(payload.get("drills_active", 0)),
        plates_made=int(payload.get("plates_made", 0)),
        asm_made=asm_made,
        placed_entities=tuple(placed_entities),
    )


def compute_walking_vector(
    cur_x: float,
    cur_y: float,
    target_x: float,
    target_y: float,
) -> tuple[float, float]:
    """Compute a normalized 2D movement vector from (cur_x, cur_y) toward (target_x, target_y)."""
    vx = target_x - cur_x
    vy = target_y - cur_y
    dist = math.hypot(vx, vy)
    if dist <= 1e-6:
        return 0.0, 0.0
    dx = vx / dist
    dy = vy / dist
    while dx * dx + dy * dy > 1.0:
        dx = math.nextafter(dx, 0.0)
        dy = math.nextafter(dy, 0.0)
    return dx, dy


def augment_save_freeplay_created_items(
    save_zip_path: Path,
    enable_telemetry: bool = True,
    timelapse_interval_ticks: int | None = None,
    timelapse_resolution: tuple[int, int] = (1280, 720),
    timelapse_zoom: float = 0.85,
    stationary_game_speed: float = 1.0,
    walking_game_speed: float = 1.0,
) -> None:
    """Augment control.lua inside save_zip_path to skip cutscene and emit telemetry."""
    pre_hook = (
        "local handler = require('event_handler')\n"
        "handler.add_lib({\n"
        "  events = {\n"
        "    [defines.events.on_player_created] = function(e)\n"
        "      storage.disable_crashsite = true\n"
        "      storage.skip_intro = true\n"
        "      storage.chart_distance = 128\n"
        "    end\n"
        "  }\n"
        "})\n"
    )
    speed_hook = ""
    if stationary_game_speed != 1.0 or walking_game_speed != 1.0:
        speed_hook = (
            "      if e.tick >= 1220 then\n"
            "        local moved = (storage.last_px ~= nil and (p.position.x ~= storage.last_px or p.position.y ~= storage.last_py))\n"
            "        storage.last_px = p.position.x\n"
            "        storage.last_py = p.position.y\n"
            "        local reset_fast = false\n"
            "        if p.walking_state.walking or moved then\n"
            "          reset_fast = true\n"
            "        elseif #placed_entities ~= (storage.last_placed_count or 0) then\n"
            "          reset_fast = true\n"
            "        elseif not p.mining_state.mining and storage.was_mining then\n"
            "          reset_fast = true\n"
            "        elseif p.crafting_queue_size == 0 and (storage.last_craft_q or 0) > 0 then\n"
            "          reset_fast = true\n"
            "        elseif active_asms < (storage.last_active_asms or 0) then\n"
            "          reset_fast = true\n"
            "        elseif active_labs < (storage.last_active_labs or 0) then\n"
            "          reset_fast = true\n"
            "        elseif active_furnaces == 0 and (storage.last_active_furnaces or 0) > 0 then\n"
            "          reset_fast = true\n"
            "        end\n"
            "        storage.last_placed_count = #placed_entities\n"
            "        storage.was_mining = p.mining_state.mining\n"
            "        storage.last_craft_q = p.crafting_queue_size\n"
            "        storage.last_active_asms = active_asms\n"
            "        storage.last_active_labs = active_labs\n"
            "        storage.last_active_furnaces = active_furnaces\n"
            "        local fast_want = false\n"
            "        if not reset_fast and not p.walking_state.walking then\n"
            "          local in_bootstrap = (#placed_entities <= 75)\n"
            "          local base_built = (#placed_entities >= 400)\n"
            "          local boot_smelt = ((not auto_res or logi_res or active_asms > 0 or active_labs > 0) and active_furnaces > 0)\n"
            "          local mining_want = false\n"
            "          if p.mining_state.mining then\n"
            "            local m_goals = {['iron-ore'] = 320, ['stone'] = 40, ['coal'] = 350, ['copper-ore'] = 220}\n"
            "            local g = (p.selected and p.selected.valid) and m_goals[p.selected.name] or nil\n"
            "            if not g or (contents[p.selected.name] or 0) < g then\n"
            "              mining_want = true\n"
            "            end\n"
            "          end\n"
            "          if mining_want or p.crafting_queue_size > 0 or (in_bootstrap and (active_asms > 0 or active_labs > 0 or boot_smelt or (drills_active >= 6 and active_furnaces > 0))) or (base_built and (drills_active > 0 or active_asms > 0 or active_labs > 0 or active_furnaces > 0)) then\n"
            "            fast_want = true\n"
            "          end\n"
            "        end\n"
            "        if fast_want then\n"
            "          storage.fast_ready_ticks = (storage.fast_ready_ticks or 0) + 2\n"
            "        else\n"
            "          storage.fast_ready_ticks = 0\n"
            "        end\n"
            f"        local target_spd = (storage.fast_ready_ticks >= 72) and {float(stationary_game_speed)} or {float(walking_game_speed)}\n"
            "        if game.speed ~= target_spd then game.speed = target_spd end\n"
            "      end\n"
        )
    telemetry_events = ""
    nth_tick_entries: list[str] = []
    if enable_telemetry:
        telemetry_events = (
            ",\n"
            "    [defines.events.on_player_crafted_item] = function(e)\n"
            "      storage.hand_crafted_count = (storage.hand_crafted_count or 0) + 1\n"
            "    end\n"
        )
        nth_tick_entries.append(
            "    [2] = function(e)\n"
            "      local p = game.get_player(1)\n"
            "      local surf = (p and p.surface) or game.surfaces[1]\n"
            "      if surf and (not storage.cartography_written or (e.tick - (storage.cartography_tick or 0)) >= 600) then\n"
            "        local r_scan = 120\n"
            "        local area = {{-r_scan, -r_scan}, {r_scan, r_scan}}\n"
            "        local obs_list = {}\n"
            "        for _, o in ipairs(surf.find_entities_filtered({area = area, type = {'tree', 'simple-entity', 'cliff'}})) do\n"
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
            "        local water_pts = {}\n"
            "        for _, t in ipairs(surf.find_tiles_filtered({area = area, collision_mask = 'water_tile'})) do\n"
            "          if not t.collides_with('ground_tile') then\n"
            "            water_pts[#water_pts + 1] = {t.position.x, t.position.y}\n"
            "          end\n"
            "        end\n"
            "        local patches = {}\n"
            "        for _, res in ipairs(surf.find_entities_filtered({area = area, type = 'resource'})) do\n"
            "          local mp = res.prototype.mineable_properties\n"
            "          if mp and mp.minable and not mp.required_fluid then\n"
            "            local list = patches[res.name]\n"
            "            if not list then list = {} patches[res.name] = list end\n"
            "            list[#list + 1] = {math.floor(res.position.x), math.floor(res.position.y), res.amount}\n"
            "          end\n"
            "        end\n"
            "        helpers.write_file('factorilog_cartography.json', helpers.table_to_json({\n"
            "          water_tiles = water_pts,\n"
            "          resource_patches = patches,\n"
            "          obstacles = obs_list\n"
            "        }) .. '\\n', false)\n"
            "        storage.cartography_written = true\n"
            "        storage.cartography_tick = e.tick\n"
            "      end\n"
            "      if not p then return end\n"
            "      local inv = p.get_main_inventory()\n"
            "      local contents = {}\n"
            "      if inv then\n"
            "        for _, item in ipairs(inv.get_contents()) do\n"
            "          contents[item.name] = (contents[item.name] or 0) + item.count\n"
            "        end\n"
            "      end\n"
            "      local rescan_res = not storage.nearest_resource or not storage.nearest_resource_entity or not storage.nearest_resource_entity.valid\n"
            "      if not rescan_res and storage.resources_by_name_ents then\n"
            "        for _, rent in pairs(storage.resources_by_name_ents) do\n"
            "          if not rent or not rent.valid then\n"
            "            rescan_res = true\n"
            "            break\n"
            "          end\n"
            "        end\n"
            "      end\n"
            "      if rescan_res then\n"
            "        local resources = p.surface.find_entities_filtered({\n"
            "          type = 'resource',\n"
            "          position = p.position,\n"
            "          radius = 96\n"
            "        })\n"
            "        local best = nil\n"
            "        local best_d2 = 1e18\n"
            "        local by_name = {}\n"
            "        local by_name_d2 = {}\n"
            "        local by_name_ents = {}\n"
            "        for _, r in ipairs(resources) do\n"
            "          local mp = r.prototype.mineable_properties\n"
            "          if mp and mp.minable and not mp.required_fluid and r.amount >= 50 then\n"
            "            local near_obs = p.surface.find_entities_filtered({position = r.position, radius = 1.6, type = {'tree', 'simple-entity', 'cliff'}})\n"
            "            local pen = (#near_obs > 0) and 400.0 or 0.0\n"
            "            local dx = r.position.x - p.position.x\n"
            "            local dy = r.position.y - p.position.y\n"
            "            local d2 = dx * dx + dy * dy + pen\n"
            "            if d2 < best_d2 then\n"
            "              best_d2 = d2\n"
            "              best = r\n"
            "            end\n"
            "            if not by_name_d2[r.name] or d2 < by_name_d2[r.name] then\n"
            "              by_name_d2[r.name] = d2\n"
            "              by_name_ents[r.name] = r\n"
            "              by_name[r.name] = {\n"
            "                name = r.name,\n"
            "                x = r.position.x,\n"
            "                y = r.position.y,\n"
            "                amount = r.amount\n"
            "              }\n"
            "            end\n"
            "          end\n"
            "        end\n"
            "        storage.resources_by_name = by_name\n"
            "        storage.resources_by_name_ents = by_name_ents\n"
            "        if best then\n"
            "          storage.nearest_resource_entity = best\n"
            "          storage.nearest_resource = {\n"
            "            name = best.name,\n"
            "            x = best.position.x,\n"
            "            y = best.position.y,\n"
            "            amount = best.amount\n"
            "          }\n"
            "        else\n"
            "          storage.nearest_resource = nil\n"
            "        end\n"
            "      else\n"
            "        storage.nearest_resource.amount = storage.nearest_resource_entity.amount\n"
            "        if storage.resources_by_name_ents then\n"
            "          for rname, rent in pairs(storage.resources_by_name_ents) do\n"
            "            if rent and rent.valid and storage.resources_by_name[rname] then\n"
            "              storage.resources_by_name[rname].amount = rent.amount\n"
            "            end\n"
            "          end\n"
            "        end\n"
            "      end\n"
            "      if not storage.nearest_tree_entity or not storage.nearest_tree_entity.valid or (e.tick - (storage.nearest_tree_tick or 0)) >= 900 then\n"
            "        local trees = p.surface.find_entities_filtered({type = 'tree', position = p.position, radius = 96})\n"
            "        local best_t = nil\n"
            "        local best_td2 = 1e18\n"
            "        for _, tr in ipairs(trees) do\n"
            "          if tr.valid and tr.prototype.mineable_properties and tr.prototype.mineable_properties.minable and tr ~= storage.nearest_tree_entity then\n"
            "            local near_tr = p.surface.find_entities_filtered({position = tr.position, radius = 2.0, type = {'tree', 'simple-entity', 'cliff'}})\n"
            "            local t_pen = (#near_tr - 1) * 400.0\n"
            "            local dx = tr.position.x - p.position.x\n"
            "            local dy = tr.position.y - p.position.y\n"
            "            local d2 = dx * dx + dy * dy + t_pen\n"
            "            if d2 < best_td2 then\n"
            "              best_td2 = d2\n"
            "              best_t = tr\n"
            "            end\n"
            "          end\n"
            "        end\n"
            "        storage.nearest_tree_entity = best_t\n"
            "        storage.nearest_tree_tick = e.tick\n"
            "      end\n"
            "      if storage.resources_by_name then\n"
            "        if storage.nearest_tree_entity and storage.nearest_tree_entity.valid then\n"
            "          storage.resources_by_name['wood'] = {\n"
            "            name = storage.nearest_tree_entity.name,\n"
            "            x = storage.nearest_tree_entity.position.x,\n"
            "            y = storage.nearest_tree_entity.position.y,\n"
            "            amount = 4\n"
            "          }\n"
            "        else\n"
            "          storage.resources_by_name['wood'] = nil\n"
            "        end\n"
            "      end\n"
            "      if storage.nearest_resource and not storage.shoreline_site then\n"
            "        local ref_x = storage.nearest_resource.x\n"
            "        local ref_y = storage.nearest_resource.y\n"
            "        local ix0 = math.floor(ref_x)\n"
            "        local iy0 = math.floor(ref_y)\n"
            "        local best_site = nil\n"
            "        local best_d2 = 1e18\n"
            "        local rot = function(dx, dy, d)\n"
            "          if d == 0 then return dx, dy\n"
            "          elseif d == 1 then return -dy, dx\n"
            "          elseif d == 2 then return -dx, -dy\n"
            "          else return dy, -dx end\n"
            "        end\n"
            "        for dy_i = -96, 96 do\n"
            "          for dx_i = -96, 96 do\n"
            "            local d2 = dx_i * dx_i + dy_i * dy_i\n"
            "            if d2 < best_d2 then\n"
            "              local px = ix0 + dx_i + 0.5\n"
            "              local py = iy0 + dy_i + 0.5\n"
            "              for d = 0, 3 do\n"
            "                local p_dir = (d * 4) % 16\n"
            "                local w_dx, w_dy = rot(0.0, -1.5, d)\n"
            "                local g_dx, g_dy = rot(0.0, 0.0, d)\n"
            "                local wt = p.surface.get_tile(px + w_dx, py + w_dy)\n"
            "                local gt = p.surface.get_tile(px + g_dx, py + g_dy)\n"
            "                if wt and wt.valid and wt.collides_with('water_tile')\n"
            "                  and gt and gt.valid and gt.collides_with('ground_tile')\n"
            "                  and not gt.collides_with('water_tile')\n"
            "                  and p.surface.can_place_entity({name = 'offshore-pump', position = {px, py}, direction = p_dir, force = p.force, build_check_type = defines.build_check_type.manual}) then\n"
            "                  local b_dir = ((d + 1) * 4) % 16\n"
            "                  local b_dx, b_dy = rot(0.5, 2.0, d)\n"
            "                  local e_dx, e_dy = rot(4.0, 2.0, d)\n"
            "                  local po_dx, po_dy = rot(2.0, 4.0, d)\n"
            "                  local a_dx, a_dy = rot(2.0, 6.0, d)\n"
            "                  local l_dx, l_dy = rot(5.0, 5.5, d)\n"
            "                  local f_dx, f_dy = rot(-4.5, 6.0, d)\n"
            "                  local f2_dx, f2_dy = rot(-4.5, 4.0, d)\n"
            "                  local f3_dx, f3_dy = rot(-4.5, 2.0, d)\n"
            "                  local f4_dx, f4_dy = rot(-4.5, 8.0, d)\n"
            "                  local a2_dx, a2_dy = rot(2.0, 9.0, d)\n"
            "                  local f5_dx, f5_dy = rot(-2.5, 2.0, d)\n"
            "                  local b4_dx, b4_dy = rot(-1.0, 9.0, d)\n"
            "                  local s_dx, s_dy = rot(-0.5, 4.5, d)\n"
            "                  local bx, by = px + b_dx, py + b_dy\n"
            "                  local ex, ey = px + e_dx, py + e_dy\n"
            "                  local pox, poy = px + po_dx, py + po_dy\n"
            "                  local ax, ay = px + a_dx, py + a_dy\n"
            "                  local lx, ly = px + l_dx, py + l_dy\n"
            "                  local fx, fy = px + f_dx, py + f_dy\n"
            "                  local f2x, f2y = px + f2_dx, py + f2_dy\n"
            "                  local f3x, f3y = px + f3_dx, py + f3_dy\n"
            "                  local f4x, f4y = px + f4_dx, py + f4_dy\n"
            "                  local a2x, a2y = px + a2_dx, py + a2_dy\n"
            "                  local f5x, f5y = px + f5_dx, py + f5_dy\n"
            "                  local b4x, b4y = px + b4_dx, py + b4_dy\n"
            "                  local sx, sy = px + s_dx, py + s_dy\n"
            "                  if p.surface.can_place_entity({name = 'boiler', position = {bx, by}, direction = b_dir, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'steam-engine', position = {ex, ey}, direction = b_dir, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'small-electric-pole', position = {pox, poy}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'assembling-machine-1', position = {ax, ay}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'assembling-machine-1', position = {a2x, a2y}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'lab', position = {lx, ly}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'stone-furnace', position = {fx, fy}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'stone-furnace', position = {f2x, f2y}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'stone-furnace', position = {f3x, f3y}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'stone-furnace', position = {f4x, f4y}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'stone-furnace', position = {f5x, f5y}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'transport-belt', position = {b4x, b4y}, direction = 0, force = p.force, build_check_type = defines.build_check_type.manual})\n"
            "                    and p.surface.can_place_entity({name = 'character', position = {sx, sy}, force = p.force}) then\n"
            "                    local clear_path = true\n"
            "                    local steps_x = math.max(1, math.ceil(math.abs(sx - ref_x)))\n"
            "                    for i = 0, steps_x do\n"
            "                      local tx = ref_x + (sx - ref_x) * (i / steps_x)\n"
            "                      local tt = p.surface.get_tile(tx, ref_y)\n"
            "                      if not tt or not tt.valid or tt.collides_with('water_tile') then clear_path = false break end\n"
            "                    end\n"
            "                    local steps_y = math.max(1, math.ceil(math.abs(sy - ref_y)))\n"
            "                    if clear_path then\n"
            "                      for i = 0, steps_y do\n"
            "                        local ty = ref_y + (sy - ref_y) * (i / steps_y)\n"
            "                        local tt = p.surface.get_tile(sx, ty)\n"
            "                        if not tt or not tt.valid or tt.collides_with('water_tile') then clear_path = false break end\n"
            "                      end\n"
            "                    end\n"
            "                    if clear_path then\n"
            "                      best_d2 = d2\n"
            "                      best_site = {\n"
            "                        pump_x = px, pump_y = py, pump_dir = p_dir,\n"
            "                        boiler_x = bx, boiler_y = by, boiler_dir = b_dir,\n"
            "                        engine_x = ex, engine_y = ey, engine_dir = b_dir,\n"
            "                        pole_x = pox, pole_y = poy,\n"
            "                        asm_x = ax, asm_y = ay,\n"
            "                        lab_x = lx, lab_y = ly,\n"
            "                        furnace_x = fx, furnace_y = fy,\n"
            "                        furnace2_x = f2x, furnace2_y = f2y,\n"
            "                        furnace3_x = f3x, furnace3_y = f3y,\n"
            "                        stand_x = sx, stand_y = sy\n"
            "                      }\n"
            "                      break\n"
            "                    end\n"
            "                  end\n"
            "                end\n"
            "              end\n"
            "            end\n"
            "          end\n"
            "        end\n"
            "        if best_site then\n"
            "          storage.shoreline_site = best_site\n"
            "        end\n"
            "      end\n"
            "      local mc_count = 0\n"
            "      local asm_made = {}\n"
            "      local asms = p.surface.find_entities_filtered({type = 'assembling-machine', force = p.force})\n"
            "      storage.asm_last_pf = storage.asm_last_pf or {}\n"
            "      storage.asm_cum_made = storage.asm_cum_made or {}\n"
            "      for _, am in ipairs(asms) do\n"
            "        local pf = am.products_finished or 0\n"
            "        local un = am.unit_number or 0\n"
            "        local last_pf = storage.asm_last_pf[un] or 0\n"
            "        local delta = (pf >= last_pf) and (pf - last_pf) or pf\n"
            "        storage.asm_last_pf[un] = pf\n"
            "        if delta > 0 then\n"
            "          storage.total_mc_count = (storage.total_mc_count or 0) + delta\n"
            "          local rec = am.get_recipe()\n"
            "          if rec then\n"
            "            storage.asm_cum_made[rec.name] = (storage.asm_cum_made[rec.name] or 0) + delta\n"
            "          end\n"
            "        end\n"
            "      end\n"
            "      mc_count = storage.total_mc_count or 0\n"
            "      for k, v in pairs(storage.asm_cum_made) do\n"
            "        asm_made[k] = v\n"
            "      end\n"
            "      local drills_active = 0\n"
            "      for _, d in ipairs(p.surface.find_entities_filtered({name = 'electric-mining-drill', force = p.force})) do\n"
            "        if d.mining_progress > 0 or d.status == defines.entity_status.working then\n"
            "          drills_active = drills_active + 1\n"
            "        end\n"
            "      end\n"
            "      local plates_made = 0\n"
            "      for _, f in ipairs(p.surface.find_entities_filtered({name = 'stone-furnace', force = p.force})) do\n"
            "        plates_made = plates_made + (f.products_finished or 0)\n"
            "      end\n"
            "      local placed_entities = storage.cached_placed_entities or {}\n"
            "      if not storage.cached_placed_entities or e.tick % 10 == 0 then\n"
            "        placed_entities = {}\n"
            "        for _, ent in ipairs(p.surface.find_entities_filtered({force = p.force})) do\n"
            "          if ent.valid and ent.name ~= 'character' then\n"
            "            local ug_t = (ent.type == 'underground-belt') and ent.belt_to_ground_type or nil\n"
            "            local rec_n = (ent.type == 'assembling-machine' and ent.get_recipe()) and ent.get_recipe().name or nil\n"
            "            placed_entities[#placed_entities + 1] = {\n"
            "              name = ent.name,\n"
            "              x = math.floor(ent.position.x * 100 + 0.5) / 100,\n"
            "              y = math.floor(ent.position.y * 100 + 0.5) / 100,\n"
            "              direction = ent.direction,\n"
            "              type = ug_t,\n"
            "              recipe = rec_n\n"
            "            }\n"
            "          end\n"
            "        end\n"
            "        storage.cached_placed_entities = placed_entities\n"
            "      end\n"
            "      local steam_energy = 0\n"
            "      local poles = p.surface.find_entities_filtered({type = 'electric-pole', force = p.force})\n"
            "      for _, pole in ipairs(poles) do\n"
            "        local stats = pole.electric_network_statistics\n"
            "        if stats then\n"
            "          local val = stats.get_output_count('steam-engine')\n"
            "          if val and val > steam_energy then steam_energy = val end\n"
            "        end\n"
            "      end\n"
            "      if steam_energy > (storage.max_steam_energy or 0) then\n"
            "        storage.max_steam_energy = steam_energy\n"
            "      end\n"
            "      steam_energy = math.max(steam_energy, storage.max_steam_energy or 0)\n"
            "      local sel = nil\n"
            "      if p.selected and p.selected.valid then\n"
            "        sel = {\n"
            "          name = p.selected.name,\n"
            "          x = p.selected.position.x,\n"
            "          y = p.selected.position.y\n"
            "        }\n"
            "      end\n"
            "      local techs = p.force.technologies\n"
            "      local spwr_res = techs['steam-power'] and techs['steam-power'].researched or false\n"
            "      local elec_res = techs['electronics'] and techs['electronics'].researched or false\n"
            "      local sp_res = techs['automation-science-pack'] and techs['automation-science-pack'].researched or false\n"
            "      local auto_res = techs['automation'] and techs['automation'].researched or false\n"
            "      local logi_res = techs['logistics'] and techs['logistics'].researched or false\n"
            "      local edrill_res = techs['electric-mining-drill'] and techs['electric-mining-drill'].researched or false\n"
            "      local active_furnaces = 0\n"
            "      for _, f in ipairs(p.surface.find_entities_filtered({name = 'stone-furnace', force = p.force})) do\n"
            "        if f.valid and f.is_crafting() then\n"
            "          active_furnaces = active_furnaces + 1\n"
            "        end\n"
            "      end\n"
            "      local active_asms = 0\n"
            "      for _, am in ipairs(asms) do\n"
            "        if am.valid and am.is_crafting() then\n"
            "          active_asms = active_asms + 1\n"
            "        end\n"
            "      end\n"
            "      local active_labs = 0\n"
            "      for _, lb in ipairs(p.surface.find_entities_filtered({name = 'lab', force = p.force})) do\n"
            "        if lb.valid and lb.status == defines.entity_status.working then\n"
            "          active_labs = active_labs + 1\n"
            "        end\n"
            "      end\n"
            f"{speed_hook}"
            "      local state = {\n"
            "        tick = e.tick,\n"
            "        player = {\n"
            "          x = p.position.x,\n"
            "          y = p.position.y,\n"
            "          walking = p.walking_state.walking,\n"
            "          mining = p.mining_state.mining,\n"
            "          selected = sel,\n"
            "          inventory = contents,\n"
            "          crafting_queue_size = p.crafting_queue_size,\n"
            "          hand_crafted_count = storage.hand_crafted_count or 0,\n"
            "          machine_crafted_count = mc_count,\n"
            "          steam_energy_generated = steam_energy,\n"
            "          steam_power_researched = spwr_res,\n"
            "          electronics_researched = elec_res,\n"
            "          science_pack_researched = sp_res,\n"
            "          automation_researched = auto_res,\n"
            "          logistics_researched = logi_res,\n"
            "          electric_mining_drill_researched = edrill_res\n"
            "        },\n"
            "        drills_active = drills_active,\n"
            "        plates_made = plates_made,\n"
            "        asm_made = asm_made,\n"
            "        placed_entities = placed_entities,\n"
            "        nearest_resource = storage.nearest_resource,\n"
            "        resources_by_name = storage.resources_by_name or {},\n"
            "        shoreline_site = storage.shoreline_site\n"
            "      }\n"
            "      helpers.write_file('factorilog_state.json', helpers.table_to_json(state) .. '\\n', false)\n"
            "    end"
        )
    if timelapse_interval_ticks is not None and timelapse_interval_ticks > 0:
        res_w, res_h = int(timelapse_resolution[0]), int(timelapse_resolution[1])
        zoom_val = float(timelapse_zoom)
        interval = int(timelapse_interval_ticks)
        timelapse_body = (
            "      local p = game.get_player(1)\n"
            "      if not p or e.tick < 1220 or p.walking_state.walking then return end\n"
            "      storage.timelapse_seq = (storage.timelapse_seq or 0) + 1\n"
            "      game.take_screenshot({\n"
            "        player = p,\n"
            "        by_player = p,\n"
            "        position = p.position,\n"
            f"        resolution = {{{res_w}, {res_h}}},\n"
            f"        zoom = {zoom_val},\n"
            "        show_gui = true,\n"
            "        show_entity_info = true,\n"
            "        path = string.format('timelapse/frame_%08d.jpg', storage.timelapse_seq)\n"
            "      })\n"
        )
        if interval == 2 and nth_tick_entries:
            nth_tick_entries[0] = nth_tick_entries[0][:-7] + timelapse_body + "    end"
        else:
            nth_tick_entries.append(f"    [{interval}] = function(e)\n{timelapse_body}    end")

    telemetry_block = ""
    if nth_tick_entries:
        joined_entries = ",\n".join(nth_tick_entries)
        telemetry_block = f",\n  on_nth_tick = {{\n{joined_entries}\n  }}\n"
    post_hook = (
        "\nlocal handler = require('event_handler')\n"
        "handler.add_lib({\n"
        "  events = {\n"
        "    [defines.events.on_player_created] = function(e)\n"
        "      local p = game.get_player(e.player_index)\n"
        "      if p then\n"
        "        if p.controller_type == defines.controllers.cutscene then\n"
        "          p.exit_cutscene()\n"
        "        end\n"
        "        if p.character then\n"
        "          p.character.destructible = true\n"
        "        end\n"
        "        p.game_view_settings.update_entity_selection = false\n"
        "      end\n"
        f"    end{telemetry_events}\n"
        f"  }}{telemetry_block}"
        "})\n"
    )
    temp_zip = save_zip_path.with_suffix(".tmp.zip")
    with (
        zipfile.ZipFile(save_zip_path, "r") as zin,
        zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zout,
    ):
        for item in zin.infolist():
            raw = zin.read(item.filename)
            if item.filename.endswith("/control.lua") or item.filename == "control.lua":
                raw = pre_hook.encode("utf-8") + raw + post_hook.encode("utf-8")
            zout.writestr(item, raw)
    temp_zip.replace(save_zip_path)


_STRING_ONLY_ACTION_TYPES: frozenset[int] = frozenset(
    {
        0x006B,
        0x00C6,
        0x00C7,
        0x00DF,
        0x00E0,
        0x00FF,
        0x010E,
        0x0143,
        0x015C,
        0x015E,
    }
)


def _consume_action_payload(buf: bytes, pos: int, act_type: int) -> int | None:
    """Return the buffer offset immediately after the payload of act_type, or None if unknown."""
    if act_type == ACTION_SETUP_ASSEMBLING_MACHINE:
        if pos + 2 > len(buf):
            return None
        (recipe_id,) = struct.unpack_from("<H", buf, pos)
        end_pos = pos + (2 if recipe_id == 0 else 3)
        return end_pos if end_pos <= len(buf) else None

    if act_type == 0x009F:
        try:
            _p1, cur = decode_uint16v(buf, pos)
            if cur + 3 > len(buf):
                return None
            cur += 3
            s_len, cur = decode_uint32v(buf, cur)
            end_pos = cur + s_len + 2
            return end_pos if end_pos <= len(buf) else None
        except ValueError:
            return None

    if act_type in _STRING_ONLY_ACTION_TYPES:
        try:
            s_len, cur = decode_uint32v(buf, pos)
            end_pos = cur + s_len
            return end_pos if end_pos <= len(buf) else None
        except ValueError:
            return None

    if act_type in KNOWN_ACTION_PAYLOAD_SIZES:
        end_pos = pos + KNOWN_ACTION_PAYLOAD_SIZES[act_type]
        return end_pos if end_pos <= len(buf) else None

    return None


def strip_client_cancellation_actions(
    buf: bytes,
    suppressed_actions: frozenset[int] = DEFAULT_SUPPRESSED_CLIENT_ACTIONS,
) -> bytes:
    """Remove suppressed client-idle InputActions from an unfragmented ClientToServerHeartbeat."""
    try:
        hdr = parse_network_header(buf)
    except ValueError:
        return buf
    if hdr.msg_type != MSG_CLIENT_TO_SERVER_HEARTBEAT or hdr.fragmented:
        return buf

    pos = hdr.payload_offset
    if pos + 5 > len(buf):
        return buf
    hb_flags = buf[pos]
    if not (hb_flags & HB_HAS_TICK_CLOSURES) or (hb_flags & HB_ALL_TICK_CLOSURES_EMPTY):
        return buf

    cur_pos = pos + 5  # flags (1B) + sequence_number (4B)
    single_closure = bool(hb_flags & HB_HAS_SINGLE_TICK_CLOSURE)
    if single_closure:
        closure_count = 1
        closures_start = cur_pos
    else:
        if cur_pos >= len(buf):
            return buf
        closure_count = buf[cur_pos]
        cur_pos += 1
        closures_start = cur_pos

    parsed_closures: list[tuple[bytes, list[InputActionRecord]]] = []
    any_removed = False
    all_kept_empty = True

    for _ in range(closure_count):
        if cur_pos + 8 > len(buf):
            return buf
        tick_bytes = buf[cur_pos : cur_pos + 8]
        cur_pos += 8
        try:
            count_flagged, cur_pos = decode_uint32v(buf, cur_pos)
        except ValueError:
            return buf
        action_count = count_flagged >> 1
        has_segments = bool(count_flagged & 1)
        if has_segments:
            return buf

        prev_player = PLAYER_INDEX_INIT
        kept_actions: list[InputActionRecord] = []
        for _ in range(action_count):
            try:
                act_type, cur_pos = decode_uint16v(buf, cur_pos)
                delta, cur_pos = decode_uint16v(buf, cur_pos)
            except ValueError:
                return buf
            end_pos = _consume_action_payload(buf, cur_pos, act_type)
            if end_pos is None:
                return buf
            act_payload = buf[cur_pos:end_pos]
            cur_pos = end_pos
            player_idx = (prev_player + delta) & PLAYER_INDEX_MASK
            prev_player = player_idx
            if act_type in suppressed_actions:
                any_removed = True
            else:
                kept_actions.append(
                    InputActionRecord(
                        action_type=act_type,
                        player_index=player_idx,
                        payload=act_payload,
                    )
                )

        if kept_actions:
            all_kept_empty = False
        parsed_closures.append((tick_bytes, kept_actions))

    if not any_removed:
        return buf

    rest = buf[cur_pos:]
    if all_kept_empty:
        new_hb_flags = hb_flags | HB_ALL_TICK_CLOSURES_EMPTY
        ticks_only = b"".join(tick_bytes for tick_bytes, _ in parsed_closures)
        return buf[:pos] + bytes([new_hb_flags]) + buf[pos + 1 : closures_start] + ticks_only + rest

    encoded_closures = bytearray()
    for tick_bytes, kept_actions in parsed_closures:
        encoded_closures.extend(tick_bytes)
        encoded_closures.extend(encode_uint32v(len(kept_actions) << 1))
        prev_player = PLAYER_INDEX_INIT
        for act in kept_actions:
            delta = (act.player_index - prev_player) & PLAYER_INDEX_MASK
            prev_player = act.player_index
            encoded_closures.extend(encode_uint16v(act.action_type))
            encoded_closures.extend(encode_uint16v(delta))
            encoded_closures.extend(act.payload)

    return (
        buf[:pos]
        + bytes([hb_flags])
        + buf[pos + 1 : closures_start]
        + bytes(encoded_closures)
        + rest
    )


def parse_heartbeat_closures(
    buf: bytes,
    payload_offset: int,
    is_client: bool = False,
    unknown_action_out: list[int] | None = None,
) -> list[TickClosureRecord]:
    """Parse TickClosureRecord entries from a ClientToServerHeartbeat or ServerToClientHeartbeat payload."""
    pos = payload_offset
    if pos + 5 > len(buf):
        return []
    hb_flags = buf[pos]
    pos += 5  # flags (1B) + sequence_number (4B)

    if not (hb_flags & HB_HAS_TICK_CLOSURES):
        return []

    if hb_flags & HB_HAS_SINGLE_TICK_CLOSURE:
        closure_count = 1
    elif is_client:
        if pos >= len(buf):
            return []
        closure_count = buf[pos]
        pos += 1
    else:
        try:
            closure_count, pos = decode_uint32v(buf, pos)
        except ValueError:
            return []

    closures: list[TickClosureRecord] = []
    if hb_flags & HB_ALL_TICK_CLOSURES_EMPTY:
        for _ in range(closure_count):
            if pos + 8 > len(buf):
                break
            (update_tick,) = struct.unpack_from("<Q", buf, pos)
            pos += 8
            closures.append(TickClosureRecord(update_tick=update_tick, actions=()))
        return closures

    for _ in range(closure_count):
        if pos + 8 > len(buf):
            break
        (update_tick,) = struct.unpack_from("<Q", buf, pos)
        pos += 8
        try:
            count_flagged, pos = decode_uint32v(buf, pos)
        except ValueError:
            break
        action_count = count_flagged >> 1
        has_segments = bool(count_flagged & 1)
        if action_count == 0 and not has_segments:
            closures.append(TickClosureRecord(update_tick=update_tick, actions=()))
            continue
        actions: list[InputActionRecord] = []
        prev_player = PLAYER_INDEX_INIT
        unknown_action = False
        for _ in range(action_count):
            try:
                act_type, pos = decode_uint16v(buf, pos)
                delta, pos = decode_uint16v(buf, pos)
            except ValueError:
                unknown_action = True
                break
            end_pos = _consume_action_payload(buf, pos, act_type)
            if end_pos is None:
                if unknown_action_out is not None and act_type not in unknown_action_out:
                    unknown_action_out.append(act_type)
                unknown_action = True
                break
            act_payload = buf[pos:end_pos]
            pos = end_pos
            player_idx = (prev_player + delta) & PLAYER_INDEX_MASK
            prev_player = player_idx
            actions.append(
                InputActionRecord(
                    action_type=act_type,
                    player_index=player_idx,
                    payload=act_payload,
                )
            )
        if unknown_action:
            if actions:
                closures.append(TickClosureRecord(update_tick=update_tick, actions=tuple(actions)))
            break
        closures.append(TickClosureRecord(update_tick=update_tick, actions=tuple(actions)))
        if has_segments:
            try:
                seg_count, pos = decode_uint32v(buf, pos)
            except ValueError:
                break
            seg_failed = False
            for _ in range(seg_count):
                if pos + 7 > len(buf):
                    seg_failed = True
                    break
                pos += 7
                try:
                    payload_len, pos = decode_uint32v(buf, pos)
                    _player_delta, pos = decode_uint16v(buf, pos)
                except ValueError:
                    seg_failed = True
                    break
                if pos + payload_len > len(buf):
                    seg_failed = True
                    break
                pos += payload_len
            if seg_failed:
                break
    return closures


def scan_heartbeat_for_achievement(
    buf: bytes,
    payload_offset: int,
    action_id: int = ACTION_ACHIEVEMENT_GAINED,
    unknown_action_out: list[int] | None = None,
) -> list[int]:
    """Scan a heartbeat payload for AchievementGained InputAction records."""
    if not buf:
        return []
    is_client = (buf[0] & HEADER_TYPE_MASK) == MSG_CLIENT_TO_SERVER_HEARTBEAT
    detected: list[int] = []
    for closure in parse_heartbeat_closures(
        buf,
        payload_offset,
        is_client=is_client,
        unknown_action_out=unknown_action_out,
    ):
        for act in closure.actions:
            if act.action_type == action_id and len(act.payload) >= 2:
                (ach_id,) = struct.unpack_from("<H", act.payload, 0)
                detected.append(ach_id)
    return detected


def scan_heartbeat_for_crafts(
    buf: bytes,
    payload_offset: int,
) -> list[tuple[int, int]]:
    """Scan a heartbeat payload for Craft (0x0056) InputAction records as (recipe_id, count)."""
    pos = payload_offset
    if pos + 5 > len(buf):
        return []
    hb_flags = buf[pos]
    pos += 5  # flags (1B) + sequence_number (4B)

    if not (hb_flags & HB_HAS_TICK_CLOSURES):
        return []
    if hb_flags & HB_ALL_TICK_CLOSURES_EMPTY:
        return []

    if hb_flags & HB_HAS_SINGLE_TICK_CLOSURE:
        closure_count = 1
    else:
        if pos >= len(buf):
            return []
        closure_count = buf[pos]
        pos += 1

    crafts: list[tuple[int, int]] = []
    for _ in range(closure_count):
        if pos + 8 > len(buf):
            break
        pos += 8  # update_tick (uint64)
        try:
            count_flagged, pos = decode_uint32v(buf, pos)
        except ValueError:
            break
        action_count = count_flagged >> 1
        has_segments = bool(count_flagged & 1)
        if action_count == 0 and not has_segments:
            continue
        unknown_action = False
        for _ in range(action_count):
            try:
                act_type, pos = decode_uint16v(buf, pos)
                _delta, pos = decode_uint16v(buf, pos)
            except ValueError:
                unknown_action = True
                break
            if act_type == ACTION_CRAFT and pos + 6 <= len(buf):
                recipe_id, count = struct.unpack_from("<HI", buf, pos)
                crafts.append((recipe_id, count))
                pos += 6
            else:
                end_pos = _consume_action_payload(buf, pos, act_type)
                if end_pos is None:
                    unknown_action = True
                    break
                pos = end_pos
        if unknown_action or has_segments:
            break
    return crafts


def inject_actions_into_empty_client_heartbeat(
    buf: bytes,
    actions: list[tuple[int, int, bytes]] | tuple[tuple[int, int, bytes], ...],
) -> bytes | None:
    """Inject one or more InputActions into an unfragmented ClientToServerHeartbeat with empty TickClosures."""
    if not actions:
        return None
    hdr = parse_network_header(buf)
    if hdr.msg_type != MSG_CLIENT_TO_SERVER_HEARTBEAT or hdr.fragmented:
        return None
    pos = hdr.payload_offset
    if pos + 5 > len(buf):
        return None
    hb_flags = buf[pos]
    required = HB_HAS_TICK_CLOSURES | HB_ALL_TICK_CLOSURES_EMPTY
    if (hb_flags & required) != required:
        return None
    if hb_flags & HB_HAS_SINGLE_TICK_CLOSURE:
        closure_count = 1
        closures_start = pos + 5
    else:
        if pos + 6 > len(buf):
            return None
        closure_count = buf[pos + 5]
        if closure_count == 0:
            return None
        closures_start = pos + 6
    if closures_start + closure_count * 8 > len(buf):
        return None

    new_hb_flags = hb_flags & ~HB_ALL_TICK_CLOSURES_EMPTY
    prefix = buf[:pos] + bytes([new_hb_flags]) + buf[pos + 1 : closures_start]
    rest = buf[closures_start + closure_count * 8 :]

    count_flagged = encode_uint32v(len(actions) << 1)
    encoded_actions = bytearray()
    prev_player = PLAYER_INDEX_INIT
    for action_type, player_index, action_payload in actions:
        delta = (player_index - prev_player) & PLAYER_INDEX_MASK
        prev_player = player_index
        encoded_actions.extend(encode_uint16v(action_type))
        encoded_actions.extend(encode_uint16v(delta))
        encoded_actions.extend(action_payload)

    leading = bytearray()
    for i in range(closure_count - 1):
        t_off = closures_start + i * 8
        leading.extend(buf[t_off : t_off + 8])
        leading.append(0)

    last_off = closures_start + (closure_count - 1) * 8
    tick_last = buf[last_off : last_off + 8]

    return prefix + bytes(leading) + tick_last + count_flagged + bytes(encoded_actions) + rest


def inject_action_into_empty_client_heartbeat(
    buf: bytes,
    action_type: int,
    player_index: int,
    action_payload: bytes,
) -> bytes | None:
    """Inject an InputAction into an unfragmented ClientToServerHeartbeat with empty TickClosures."""
    return inject_actions_into_empty_client_heartbeat(
        buf, ((action_type, player_index, action_payload),)
    )


@dataclass
class LockstepProxyStats:
    """Traffic and achievement telemetry collected by the UDP proxy."""

    client_to_server_packets: int = 0
    server_to_client_packets: int = 0
    message_type_counts: dict[int, int] = field(default_factory=dict)
    gained_achievement_ids: list[int] = field(default_factory=list)
    client_emitted_achievement_ids: list[int] = field(default_factory=list)
    server_broadcast_achievement_ids: list[int] = field(default_factory=list)
    injected_actions: int = 0
    empty_client_heartbeats_seen: int = 0
    suppressed_client_packets: int = 0
    hand_crafted_count: int = 0
    blocked_hand_craft_actions: int = 0
    unknown_action_types: list[int] = field(default_factory=list)


class FactorioLockstepProxy:
    """UDP MITM proxy between a Factorio client and a Factorio server."""

    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        server_host: str,
        server_port: int,
        achievement_action_id: int = ACTION_ACHIEVEMENT_GAINED,
        suppress_client_cancellation: bool = True,
        suppressed_client_actions: frozenset[int] = DEFAULT_SUPPRESSED_CLIENT_ACTIONS,
        max_hand_crafts: int | None = None,
        max_actions_per_heartbeat: int = 1,
    ) -> None:
        self.listen_addr = (listen_host, listen_port)
        self.server_addr = (server_host, server_port)
        self.achievement_action_id = achievement_action_id
        self.suppress_client_cancellation = suppress_client_cancellation
        self.suppressed_client_actions = suppressed_client_actions
        self.max_hand_crafts = max_hand_crafts
        self.max_actions_per_heartbeat = max(1, int(max_actions_per_heartbeat))
        self.stats = LockstepProxyStats()
        self._pending_injections: list[tuple[int, int, bytes, int]] = []
        self._unacked_client_achievements: dict[int, tuple[int, int]] = {}
        self._queued_hand_crafts = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._client_addr: tuple[str, int] | None = None

    def queue_action(
        self,
        action_type: int,
        player_index: int,
        payload: bytes,
        min_empty_heartbeats: int = 0,
    ) -> None:
        """Queue an InputAction for injection into an eligible client heartbeat."""
        with self._lock:
            if action_type == ACTION_CRAFT:
                if len(payload) < 6:
                    raise ValueError("Craft payload must be 6 bytes (<HI)")
                _recipe_id, count = struct.unpack_from("<HI", payload, 0)
                projected = self.stats.hand_crafted_count + self._queued_hand_crafts + count
                if self.max_hand_crafts is not None and projected > self.max_hand_crafts:
                    self.stats.blocked_hand_craft_actions += 1
                    raise ValueError(
                        f"Craft count {count} exceeds max_hand_crafts limit {self.max_hand_crafts} "
                        f"(current={self.stats.hand_crafted_count}, queued={self._queued_hand_crafts})"
                    )
                self._queued_hand_crafts += count
            self._pending_injections.append(
                (action_type, player_index, payload, min_empty_heartbeats)
            )

    def pending_action_count(self) -> int:
        """Return the number of queued InputActions waiting for injection."""
        with self._lock:
            return len(self._pending_injections)

    def start(self) -> None:
        """Bind UDP socket and start forwarding thread."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(self.listen_addr)
        sock.settimeout(0.05)
        self._sock = sock
        self.listen_addr = sock.getsockname()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop forwarding thread and close UDP socket."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _run_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop_event.is_set():
            try:
                data, addr = sock.recvfrom(MAX_UDP_PAYLOAD)
            except TimeoutError:
                continue
            except OSError:
                break

            if addr == self.server_addr:
                self._handle_server_packet(sock, data)
            else:
                self._client_addr = addr
                self._handle_client_packet(sock, data)

    def _handle_client_packet(self, sock: socket.socket, data: bytes) -> None:
        with self._lock:
            self.stats.client_to_server_packets += 1
            try:
                hdr = parse_network_header(data)
                self.stats.message_type_counts[hdr.msg_type] = (
                    self.stats.message_type_counts.get(hdr.msg_type, 0) + 1
                )
                unsplit_hb = not hdr.fragmented or (hdr.fragment_number == 0 and hdr.last_fragment)
                if hdr.msg_type == MSG_CLIENT_TO_SERVER_HEARTBEAT and unsplit_hb:
                    # First scan natural client-emitted heartbeats before any proxy injection
                    natural_found: list[int] = []
                    for cl in parse_heartbeat_closures(
                        data,
                        hdr.payload_offset,
                        is_client=True,
                        unknown_action_out=self.stats.unknown_action_types,
                    ):
                        for act in cl.actions:
                            if (
                                act.action_type == self.achievement_action_id
                                and len(act.payload) >= 2
                            ):
                                (ach_id,) = struct.unpack_from("<H", act.payload, 0)
                                natural_found.append(ach_id)
                                if ach_id not in self.stats.client_emitted_achievement_ids:
                                    self.stats.client_emitted_achievement_ids.append(ach_id)
                                if ach_id not in self.stats.gained_achievement_ids:
                                    self.stats.gained_achievement_ids.append(ach_id)
                                if ach_id not in self.stats.server_broadcast_achievement_ids:
                                    self._unacked_client_achievements[ach_id] = (
                                        act.player_index,
                                        self.stats.empty_client_heartbeats_seen,
                                    )

                    if not hdr.fragmented:
                        client_crafts = scan_heartbeat_for_crafts(data, hdr.payload_offset)
                        if client_crafts:
                            craft_total = sum(count for _rid, count in client_crafts)
                            projected = (
                                self.stats.hand_crafted_count
                                + self._queued_hand_crafts
                                + craft_total
                            )
                            if (
                                self.max_hand_crafts is not None
                                and projected > self.max_hand_crafts
                            ):
                                data = strip_client_cancellation_actions(
                                    data, frozenset({ACTION_CRAFT})
                                )
                                self.stats.blocked_hand_craft_actions += len(client_crafts)
                            else:
                                self.stats.hand_crafted_count += craft_total

                        if self.suppress_client_cancellation and not natural_found:
                            stripped = strip_client_cancellation_actions(
                                data, self.suppressed_client_actions
                            )
                            if stripped != data:
                                data = stripped
                                self.stats.suppressed_client_packets += 1

                        pos = hdr.payload_offset
                        required = HB_HAS_TICK_CLOSURES | HB_ALL_TICK_CLOSURES_EMPTY
                        if pos < len(data) and (data[pos] & required) == required:
                            self.stats.empty_client_heartbeats_seen += 1
                            if self._unacked_client_achievements:
                                for ach_id, (pid, last_hb) in list(
                                    self._unacked_client_achievements.items()
                                ):
                                    if ach_id in self.stats.server_broadcast_achievement_ids:
                                        self._unacked_client_achievements.pop(ach_id, None)
                                        continue
                                    if self.stats.empty_client_heartbeats_seen - last_hb >= 30:
                                        ach_payload = struct.pack("<H", ach_id & UINT16_MASK)
                                        if not any(
                                            a_t == self.achievement_action_id and a_p == ach_payload
                                            for a_t, _p, a_p, _m in self._pending_injections
                                        ):
                                            self._pending_injections.insert(
                                                0,
                                                (
                                                    self.achievement_action_id,
                                                    pid,
                                                    ach_payload,
                                                    0,
                                                ),
                                            )
                                        self._unacked_client_achievements[ach_id] = (
                                            pid,
                                            self.stats.empty_client_heartbeats_seen,
                                        )
                            if self._pending_injections:
                                batch: list[tuple[int, int, bytes]] = []
                                for act_type, pid, payload, min_hb in self._pending_injections:
                                    if self.stats.empty_client_heartbeats_seen < min_hb:
                                        break
                                    batch.append((act_type, pid, payload))
                                    if len(batch) >= self.max_actions_per_heartbeat:
                                        break
                                    if act_type in (
                                        ACTION_CLEAR_CURSOR,
                                        ACTION_CLOSE_GUI,
                                        ACTION_STOP_WALKING,
                                        ACTION_STOP_MINING,
                                    ):
                                        break
                                    if (
                                        len(batch) < len(self._pending_injections)
                                        and self._pending_injections[len(batch)][0]
                                        == ACTION_PIPETTE
                                        and any(b_type == ACTION_BUILD for b_type, _p, _pl in batch)
                                    ):
                                        break
                                if batch:
                                    modified = inject_actions_into_empty_client_heartbeat(
                                        data, batch
                                    )
                                    if modified is not None:
                                        data = modified
                                        for act_type, _pid, payload in batch:
                                            self._pending_injections.pop(0)
                                            self.stats.injected_actions += 1
                                            if act_type == ACTION_CRAFT and len(payload) >= 6:
                                                _rid, c_count = struct.unpack_from(
                                                    "<HI", payload, 0
                                                )
                                                self._queued_hand_crafts = max(
                                                    0, self._queued_hand_crafts - c_count
                                                )
                                                self.stats.hand_crafted_count += c_count
                                        injected_found = scan_heartbeat_for_achievement(
                                            data, hdr.payload_offset, self.achievement_action_id
                                        )
                                        for ach_id in injected_found:
                                            if ach_id not in self.stats.gained_achievement_ids:
                                                self.stats.gained_achievement_ids.append(ach_id)
            except ValueError:
                pass
        sock.sendto(data, self.server_addr)

    def _handle_server_packet(self, sock: socket.socket, data: bytes) -> None:
        with self._lock:
            self.stats.server_to_client_packets += 1
            try:
                hdr = parse_network_header(data)
                self.stats.message_type_counts[hdr.msg_type] = (
                    self.stats.message_type_counts.get(hdr.msg_type, 0) + 1
                )
                unsplit_hb = not hdr.fragmented or (hdr.fragment_number == 0 and hdr.last_fragment)
                if hdr.msg_type == MSG_SERVER_TO_CLIENT_HEARTBEAT and unsplit_hb:
                    found = scan_heartbeat_for_achievement(
                        data,
                        hdr.payload_offset,
                        self.achievement_action_id,
                        unknown_action_out=self.stats.unknown_action_types,
                    )
                    for ach_id in found:
                        if ach_id not in self.stats.server_broadcast_achievement_ids:
                            self.stats.server_broadcast_achievement_ids.append(ach_id)
                        if ach_id not in self.stats.gained_achievement_ids:
                            self.stats.gained_achievement_ids.append(ach_id)
                        self._unacked_client_achievements.pop(ach_id, None)
            except ValueError:
                pass
        if self._client_addr is not None:
            sock.sendto(data, self._client_addr)


def _find_free_x11_display(start_display: int = 99, max_display: int = 199) -> int:
    """Return an unused X11 display number that has no lock or socket file in /tmp."""
    for disp in range(start_display, max_display + 1):
        lock_file = Path(f"/tmp/.X{disp}-lock")
        sock_file = Path(f"/tmp/.X11-unix/X{disp}")
        if not lock_file.exists() and not sock_file.exists():
            return disp
    raise RuntimeError("No free X11 display number found")


class XvfbVideoRecorder:
    """Record a headless Factorio client window to an MP4 file through Xvfb and ffmpeg."""

    def __init__(
        self,
        output_mp4: Path,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        display_num: int | None = None,
        playback_slowdown: float = 1.0,
    ) -> None:
        self.output_mp4 = output_mp4
        self.width = width
        self.height = height
        self.fps = fps
        self.display_num = _find_free_x11_display() if display_num is None else display_num
        self.playback_slowdown = playback_slowdown
        self._xvfb_proc: subprocess.Popen[str] | None = None
        self._ffmpeg_proc: subprocess.Popen[str] | None = None

    @property
    def display(self) -> str:
        """Return the X11 DISPLAY string (for example ':99')."""
        return f":{self.display_num}"

    def start_display(self) -> str:
        """Start the Xvfb virtual X11 framebuffer and wait until its socket is ready."""
        if not shutil.which("Xvfb"):
            raise FileNotFoundError("Xvfb executable not found")
        cmd = [
            "Xvfb",
            self.display,
            "-screen",
            "0",
            f"{self.width}x{self.height}x24",
            "-nolisten",
            "tcp",
            "-ac",
        ]
        self._xvfb_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        sock_file = Path(f"/tmp/.X11-unix/X{self.display_num}")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if sock_file.exists():
                break
            if self._xvfb_proc.poll() is not None:
                raise RuntimeError("Xvfb exited before creating X11 socket")
            time.sleep(0.05)
        return self.display

    def start_recording(self) -> None:
        """Start ffmpeg x11grab capture from the Xvfb display into output_mp4."""
        if not shutil.which("ffmpeg"):
            raise FileNotFoundError("ffmpeg executable not found")
        if self._xvfb_proc is None:
            self.start_display()
        self.output_mp4.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "x11grab",
            "-framerate",
            str(self.fps),
            "-video_size",
            f"{self.width}x{self.height}",
            "-draw_mouse",
            "0",
            "-i",
            f"{self.display}.0",
        ]
        if self.playback_slowdown != 1.0:
            cmd.extend(["-vf", f"setpts={self.playback_slowdown}*PTS", "-r", str(self.fps)])
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                str(self.output_mp4),
            ]
        )
        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def stop(self) -> None:
        """Finalize the MP4 file and terminate ffmpeg and Xvfb."""
        if self._ffmpeg_proc is not None:
            if self._ffmpeg_proc.stdin is not None:
                self._ffmpeg_proc.stdin.close()
            if self._ffmpeg_proc.poll() is None:
                self._ffmpeg_proc.send_signal(signal.SIGINT)
                try:
                    self._ffmpeg_proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    self._ffmpeg_proc.kill()
            self._ffmpeg_proc = None

        if self._xvfb_proc is not None:
            if self._xvfb_proc.poll() is None:
                self._xvfb_proc.terminate()
                try:
                    self._xvfb_proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._xvfb_proc.kill()
            self._xvfb_proc = None

    def __enter__(self) -> XvfbVideoRecorder:
        self.start_display()
        self.start_recording()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()


def encode_timelapse_mp4(
    frames_dir: Path,
    output_mp4: Path,
    fps: int = 12,
    frame_pattern: str = "frame_%08d.jpg",
) -> Path:
    """Encode sequential screenshot frames in frames_dir into an H.264 MP4 video."""
    if not shutil.which("ffmpeg"):
        raise FileNotFoundError("ffmpeg executable not found")
    if not frames_dir.is_dir():
        raise FileNotFoundError(f"Timelapse directory does not exist: {frames_dir}")
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frames_dir / frame_pattern),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        str(output_mp4),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg timelapse encode failed:\n{res.stderr}")
    return output_mp4
