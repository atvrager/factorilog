"""Factorio UDP multiplayer protocol dissector and lockstep proxy.

Parses NetworkMessageHeader, ClientToServerHeartbeat, ServerToClientHeartbeat,
TickClosure, and InputAction records on the UDP wire.
All docstrings and comments follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import dataclass, field

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

ACTION_ACHIEVEMENT_GAINED = 0x0159


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
        if action_count == 0:
            continue
        if action_count == 1:
            try:
                act_type, pos = decode_uint16v(buf, pos)
                _delta, pos = decode_uint16v(buf, pos)
            except ValueError:
                break
            if act_type == action_id and pos + 2 <= len(buf):
                (ach_id,) = struct.unpack_from("<H", buf, pos)
                detected.append(ach_id)
                pos += 2
                continue
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
    injected_actions: int = 0


class FactorioLockstepProxy:
    """UDP MITM proxy between a Factorio client and a Factorio server."""

    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        server_host: str,
        server_port: int,
        achievement_action_id: int = ACTION_ACHIEVEMENT_GAINED,
    ) -> None:
        self.listen_addr = (listen_host, listen_port)
        self.server_addr = (server_host, server_port)
        self.achievement_action_id = achievement_action_id
        self.stats = LockstepProxyStats()
        self._pending_injections: list[tuple[int, int, bytes]] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._client_addr: tuple[str, int] | None = None

    def queue_action(self, action_type: int, player_index: int, payload: bytes) -> None:
        """Queue an InputAction for injection into the next eligible client heartbeat."""
        with self._lock:
            self._pending_injections.append((action_type, player_index, payload))

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
                    if self._pending_injections:
                        act_type, pid, payload = self._pending_injections[0]
                        modified = inject_action_into_empty_client_heartbeat(
                            data, act_type, pid, payload
                        )
                        if modified is not None:
                            data = modified
                            self._pending_injections.pop(0)
                            self.stats.injected_actions += 1

                    found = scan_heartbeat_for_achievement(
                        data, hdr.payload_offset, self.achievement_action_id
                    )
                    for ach_id in found:
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
                        if ach_id not in self.stats.gained_achievement_ids:
                            self.stats.gained_achievement_ids.append(ach_id)
            except ValueError:
                pass
        if self._client_addr is not None:
            sock.sendto(data, self._client_addr)
