"""Factorio UDP multiplayer protocol dissector and lockstep proxy.

Parses NetworkMessageHeader, ClientToServerHeartbeat, ServerToClientHeartbeat,
TickClosure, and InputAction records on the UDP wire.
All docstrings and comments follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import json
import math
import socket
import struct
import threading
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

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
ACTION_SELECTED_ENTITY_CLEARED = 0x0009
ACTION_CLEAR_CURSOR = 0x000A
ACTION_BUILD = 0x0045
ACTION_START_WALKING = 0x0046
ACTION_BEGIN_MINING_TERRAIN = 0x0047
ACTION_CRAFT = 0x0056
ACTION_SETUP_ASSEMBLING_MACHINE = 0x0059
ACTION_SELECTED_ENTITY_CHANGED = 0x005A
ACTION_PIPETTE = 0x005B
ACTION_SELECT_ENTITY_VERY_CLOSE = 0x010A
ACTION_SELECT_ENTITY_VERY_CLOSE_PRECISE = 0x010B
ACTION_SELECT_ENTITY_RELATIVE = 0x010C
ACTION_SELECT_ENTITY_UNIT_NUMBER = 0x010D
ACTION_ACHIEVEMENT_GAINED = 0x0159
ACTION_CHECK_CRC_HEURISTIC = 0x015D

KNOWN_ACTION_PAYLOAD_SIZES: dict[int, int] = {
    ACTION_NOTHING: 0,
    ACTION_STOP_WALKING: 0,
    ACTION_BEGIN_MINING: 0,
    ACTION_STOP_MINING: 0,
    ACTION_SELECTED_ENTITY_CLEARED: 0,
    ACTION_CLEAR_CURSOR: 0,
    ACTION_BUILD: 12,
    ACTION_START_WALKING: 16,
    ACTION_BEGIN_MINING_TERRAIN: 8,
    ACTION_CRAFT: 6,
    ACTION_SETUP_ASSEMBLING_MACHINE: 3,
    ACTION_SELECTED_ENTITY_CHANGED: 8,
    ACTION_PIPETTE: 10,
    ACTION_SELECT_ENTITY_VERY_CLOSE: 1,
    ACTION_SELECT_ENTITY_VERY_CLOSE_PRECISE: 2,
    ACTION_SELECT_ENTITY_RELATIVE: 4,
    ACTION_SELECT_ENTITY_UNIT_NUMBER: 8,
    ACTION_ACHIEVEMENT_GAINED: 2,
    ACTION_CHECK_CRC_HEURISTIC: 12,
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
    """Build ClearCursor (0x000b) InputAction."""
    return ACTION_CLEAR_CURSOR, b""


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


def build_setup_assembling_machine_action(recipe_id: int, quality_id: int = 0) -> tuple[int, bytes]:
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


def _try_parse_id_mapping_table(
    buf: bytes, start_pos: int
) -> tuple[dict[str, dict[str, int]], int] | None:
    """Attempt to parse one savedIDMapping table from buf at start_pos."""
    if start_pos + 2 > len(buf):
        return None
    (num_types,) = struct.unpack_from("<H", buf, start_pos)
    if num_types == 0 or num_types > 512:
        return None

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

        proto_map: dict[str, int] = {}
        for _ in range(num_protos):
            try:
                p_len, pos = decode_uint32v(buf, pos)
            except ValueError:
                return None
            if p_len == 0 or p_len > 256 or pos + p_len + 2 > len(buf):
                return None
            p_bytes = buf[pos : pos + p_len]
            if not all(32 <= b <= 126 for b in p_bytes):
                return None
            p_name = p_bytes.decode("utf-8", errors="strict")
            pos += p_len

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
    return vx / dist, vy / dist


def augment_save_freeplay_created_items(
    save_zip_path: Path,
    extra_items: dict[str, int],
    enable_telemetry: bool = True,
) -> None:
    """Augment control.lua inside save_zip_path to skip cutscene, add items, and emit telemetry."""
    lua_entries = ", ".join(f'["{name}"] = {int(count)}' for name, count in extra_items.items())
    pre_hook = (
        "local handler = require('event_handler')\n"
        "handler.add_lib({\n"
        "  events = {\n"
        "    [defines.events.on_player_created] = function(e)\n"
        "      storage.disable_crashsite = true\n"
        "      storage.skip_intro = true\n"
        "      storage.chart_distance = 64\n"
        "    end\n"
        "  }\n"
        "})\n"
    )
    telemetry_block = ""
    if enable_telemetry:
        telemetry_block = (
            ",\n"
            "  on_nth_tick = {\n"
            "    [5] = function(e)\n"
            "      local p = game.get_player(1)\n"
            "      if not p then return end\n"
            "      local inv = p.get_main_inventory()\n"
            "      local contents = {}\n"
            "      if inv then\n"
            "        for _, item in ipairs(inv.get_contents()) do\n"
            "          contents[item.name] = (contents[item.name] or 0) + item.count\n"
            "        end\n"
            "      end\n"
            "      if not storage.nearest_resource or not storage.nearest_resource_entity or not storage.nearest_resource_entity.valid then\n"
            "        local resources = p.surface.find_entities_filtered({\n"
            "          type = 'resource',\n"
            "          position = p.position,\n"
            "          radius = 96\n"
            "        })\n"
            "        local best = nil\n"
            "        local best_d2 = 1e18\n"
            "        for _, r in ipairs(resources) do\n"
            "          local mp = r.prototype.mineable_properties\n"
            "          if mp and mp.minable and not mp.required_fluid then\n"
            "            local dx = r.position.x - p.position.x\n"
            "            local dy = r.position.y - p.position.y\n"
            "            local d2 = dx * dx + dy * dy\n"
            "            if d2 < best_d2 then\n"
            "              best_d2 = d2\n"
            "              best = r\n"
            "            end\n"
            "          end\n"
            "        end\n"
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
            "      end\n"
            "      local sel = nil\n"
            "      if p.selected and p.selected.valid then\n"
            "        sel = {\n"
            "          name = p.selected.name,\n"
            "          x = p.selected.position.x,\n"
            "          y = p.selected.position.y\n"
            "        }\n"
            "      end\n"
            "      local state = {\n"
            "        tick = e.tick,\n"
            "        player = {\n"
            "          x = p.position.x,\n"
            "          y = p.position.y,\n"
            "          walking = p.walking_state.walking,\n"
            "          mining = p.mining_state.mining,\n"
            "          selected = sel,\n"
            "          inventory = contents\n"
            "        },\n"
            "        nearest_resource = storage.nearest_resource\n"
            "      }\n"
            "      helpers.write_file('factorilog_state.json', helpers.table_to_json(state) .. '\\n', false)\n"
            "    end\n"
            "  }\n"
        )
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
        f"        for k, v in pairs({{{lua_entries}}}) do\n"
        "          p.insert({name = k, count = v})\n"
        "        end\n"
        "      end\n"
        "    end\n"
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
            if act_type not in KNOWN_ACTION_PAYLOAD_SIZES:
                return buf
            p_size = KNOWN_ACTION_PAYLOAD_SIZES[act_type]
            if cur_pos + p_size > len(buf):
                return buf
            act_payload = buf[cur_pos : cur_pos + p_size]
            cur_pos += p_size
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


def scan_heartbeat_for_achievement(
    buf: bytes,
    payload_offset: int,
    action_id: int = ACTION_ACHIEVEMENT_GAINED,
) -> list[int]:
    """Scan a heartbeat payload for AchievementGained InputAction records."""
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

    detected: list[int] = []
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
            if act_type == action_id and pos + 2 <= len(buf):
                (ach_id,) = struct.unpack_from("<H", buf, pos)
                detected.append(ach_id)
                pos += 2
            elif act_type in KNOWN_ACTION_PAYLOAD_SIZES:
                p_size = KNOWN_ACTION_PAYLOAD_SIZES[act_type]
                if pos + p_size > len(buf):
                    unknown_action = True
                    break
                pos += p_size
            else:
                unknown_action = True
                break
        if unknown_action or has_segments:
            break
    return detected


def inject_action_into_empty_client_heartbeat(
    buf: bytes,
    action_type: int,
    player_index: int,
    action_payload: bytes,
) -> bytes | None:
    """Inject an InputAction into an unfragmented ClientToServerHeartbeat with one empty TickClosure."""
    hdr = parse_network_header(buf)
    if hdr.msg_type != MSG_CLIENT_TO_SERVER_HEARTBEAT or hdr.fragmented:
        return None
    pos = hdr.payload_offset
    if pos + 5 > len(buf):
        return None
    hb_flags = buf[pos]
    required = HB_HAS_TICK_CLOSURES | HB_HAS_SINGLE_TICK_CLOSURE | HB_ALL_TICK_CLOSURES_EMPTY
    if (hb_flags & required) != required:
        return None
    if pos + 13 > len(buf):
        return None

    new_hb_flags = hb_flags & ~HB_ALL_TICK_CLOSURES_EMPTY
    seq_and_tick = buf[pos + 1 : pos + 13]  # 4B seq + 8B update_tick
    rest = buf[pos + 13 :]

    count_flagged = encode_uint32v(1 << 1)
    delta = (player_index - PLAYER_INDEX_INIT) & PLAYER_INDEX_MASK
    encoded_action = encode_uint16v(action_type) + encode_uint16v(delta) + action_payload

    return buf[:pos] + bytes([new_hb_flags]) + seq_and_tick + count_flagged + encoded_action + rest


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
    ) -> None:
        self.listen_addr = (listen_host, listen_port)
        self.server_addr = (server_host, server_port)
        self.achievement_action_id = achievement_action_id
        self.suppress_client_cancellation = suppress_client_cancellation
        self.suppressed_client_actions = suppressed_client_actions
        self.stats = LockstepProxyStats()
        self._pending_injections: list[tuple[int, int, bytes, int]] = []
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
                if hdr.msg_type == MSG_CLIENT_TO_SERVER_HEARTBEAT and not hdr.fragmented:
                    # First scan natural client-emitted heartbeats before any proxy injection
                    natural_found = scan_heartbeat_for_achievement(
                        data, hdr.payload_offset, self.achievement_action_id
                    )
                    for ach_id in natural_found:
                        if ach_id not in self.stats.client_emitted_achievement_ids:
                            self.stats.client_emitted_achievement_ids.append(ach_id)
                        if ach_id not in self.stats.gained_achievement_ids:
                            self.stats.gained_achievement_ids.append(ach_id)

                    if self.suppress_client_cancellation:
                        stripped = strip_client_cancellation_actions(
                            data, self.suppressed_client_actions
                        )
                        if stripped != data:
                            data = stripped
                            self.stats.suppressed_client_packets += 1

                    pos = hdr.payload_offset
                    required = (
                        HB_HAS_TICK_CLOSURES
                        | HB_HAS_SINGLE_TICK_CLOSURE
                        | HB_ALL_TICK_CLOSURES_EMPTY
                    )
                    if pos < len(data) and (data[pos] & required) == required:
                        self.stats.empty_client_heartbeats_seen += 1
                        if self._pending_injections:
                            act_type, pid, payload, min_hb = self._pending_injections[0]
                            if self.stats.empty_client_heartbeats_seen >= min_hb:
                                modified = inject_action_into_empty_client_heartbeat(
                                    data, act_type, pid, payload
                                )
                                if modified is not None:
                                    data = modified
                                    self._pending_injections.pop(0)
                                    self.stats.injected_actions += 1
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
                if hdr.msg_type == MSG_SERVER_TO_CLIENT_HEARTBEAT and not hdr.fragmented:
                    found = scan_heartbeat_for_achievement(
                        data, hdr.payload_offset, self.achievement_action_id
                    )
                    for ach_id in found:
                        if ach_id not in self.stats.server_broadcast_achievement_ids:
                            self.stats.server_broadcast_achievement_ids.append(ach_id)
                        if ach_id not in self.stats.gained_achievement_ids:
                            self.stats.gained_achievement_ids.append(ach_id)
            except ValueError:
                pass
        if self._client_addr is not None:
            sock.sendto(data, self._client_addr)
