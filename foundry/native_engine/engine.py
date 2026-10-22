"""Native simulation engine loader and execution interface for Factorio."""

from __future__ import annotations

import ctypes
import os
import struct
from pathlib import Path
from typing import Any, TypeAlias

from foundry.native_engine.symbol_finder import (
    FactorioSymbolFinder,
    resolve_factorio_binary,
)

ProtoTable: TypeAlias = dict[str, Any]

ONE_24 = 1 << 24
ONE_8 = 1 << 8

DEFAULT_ENGINE_CACHE_DIR = "/tmp/factorilog_engine"
PROC_SELF_MAPS_PATH = "/proc/self/maps"
COPY_CHUNK_BYTES = 16 * 1024 * 1024
PT_INTERP_OFFSET = 120
DT_FLAGS_1_TAG = (0x6FFF << 16) | 0xFFFB
DF_1_PIE_MASK = 1 << 27
TAG_VAL_START_OFFSET = 8
TAG_VAL_END_OFFSET = 16
HEX_RADIX = 16

FLUID_BUFFER_SIZE_BYTES = 64
FLUID_TEMP_OFFSET = 4
FLUID_AMOUNT_OFFSET = 8
FLUID_CAPACITY_OFFSET = 0x10
FLUID_MIN_TEMP_OFFSET = 28
FLUID_MAX_TEMP_OFFSET = 32
FLUID_FLAGS_OFFSET = 0x24
DEFAULT_FLUID_CAPACITY = 100.0
DEFAULT_FLUID_TEMP = 15.0
DEFAULT_FLUID_MAX_TEMP = 1000.0

DEFAULT_DEQUE_CAPACITY = 16
DEFAULT_LINE_LENGTH = 256
TRANSPORT_LINE_SIZE_BYTES = 256
DEQUE_STRIDE_BYTES = 32
ITEM_STANDARD_SPACING = 64
LINE_FRONT_OFFSET = 0x38
LINE_BACK_OFFSET = 0x3C
LINE_SPACING_OFFSET = 0x40
LINE_FIRST_SQUASHED_OFFSET = 0x44
LINE_LAST_SQUASHED_OFFSET = 0x48
LINE_LENGTH_OFFSET = 0x4C
LINE_DEQUE_PTR_OFFSET = 0x50
LINE_DEQUE_CAP_OFFSET = 0x58
LINE_DEQUE_HEAD_OFFSET = 0x5C
LINE_ITEM_COUNT_OFFSET = 0x60
LINE_AUX_PTR_OFFSET = 0x68


class FactorioNativeEngine:
    """Prepares, loads, and executes native Factorio simulation functions."""

    def __init__(
        self,
        binary_path: str | Path | None = None,
        cache_dir: str | Path = DEFAULT_ENGINE_CACHE_DIR,
    ) -> None:
        resolved = resolve_factorio_binary(binary_path)
        if not resolved:
            raise FileNotFoundError(
                "Factorio binary not found. Set FACTORIO_BIN environment variable or install Factorio."
            )
        self.binary_path = resolved
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.finder = FactorioSymbolFinder(self.binary_path)
        self.lib_path = self._prepare_shared_library()
        self._handle: Any = None
        self._base_address: int = 0
        self._load_library()

    def _prepare_shared_library(self) -> Path:
        """Create a patched shared library copy with PT_INTERP and DF_1_PIE disabled."""
        target_so = self.cache_dir / f"{self.binary_path.name}_lib.so"
        stat_src = self.binary_path.stat()

        if target_so.exists() and target_so.stat().st_size == stat_src.st_size:
            return target_so

        with self.binary_path.open("rb") as f_in, target_so.open("wb") as f_out:
            while buf := f_in.read(COPY_CHUNK_BYTES):
                f_out.write(buf)

        with target_so.open("r+b") as f:
            f.seek(PT_INTERP_OFFSET)
            f.write(struct.pack("<I", 0))

            f.seek(0)
            content = bytearray(f.read())
            tag_bytes = struct.pack("<Q", DT_FLAGS_1_TAG)
            tag_idx = content.find(tag_bytes)
            if tag_idx != -1:
                val_start = tag_idx + TAG_VAL_START_OFFSET
                val_end = tag_idx + TAG_VAL_END_OFFSET
                val = struct.unpack("<Q", content[val_start:val_end])[0]
                new_val = val & ~DF_1_PIE_MASK
                content[val_start:val_end] = struct.pack("<Q", new_val)
                f.seek(0)
                f.write(content)

        return target_so

    def _load_library(self) -> None:
        """Load dependencies and the patched Factorio library through dlopen."""
        lib_dir = self.binary_path.parent
        steam_so = lib_dir / "libsteam_api.so"
        if steam_so.exists():
            ctypes.CDLL(str(steam_so), mode=ctypes.RTLD_GLOBAL)

        self._handle = ctypes.CDLL(str(self.lib_path), mode=os.RTLD_LAZY | os.RTLD_GLOBAL)

        target_name = self.lib_path.name
        with Path(PROC_SELF_MAPS_PATH).open(encoding="utf-8") as m:
            for line in m:
                if target_name in line and "00000000" in line:
                    self._base_address = int(line.split("-")[0], HEX_RADIX)
                    break

        if not self._base_address:
            raise RuntimeError(
                f"Failed to find load base address for {target_name} in process memory"
            )

    def get_function_address(self, symbol_key: str) -> int:
        """Calculate runtime memory address of a simulation routine."""
        offset = self.finder.get_offset(symbol_key)
        return self._base_address + offset

    def calculate_fluid_flow(
        self,
        src_amount: float,
        src_capacity: float,
        dst_amount: float,
        dst_capacity: float,
    ) -> float:
        """Execute native FluidBuffer::getFlowRate directly in the game binary."""
        fn_addr = self.get_function_address("fluid_get_flow_rate")
        proto = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.c_long)
        native_fn = proto(fn_addr)

        buf = bytearray(FLUID_BUFFER_SIZE_BYTES)
        struct.pack_into("<q", buf, FLUID_AMOUNT_OFFSET, int(src_amount * ONE_24))
        struct.pack_into("<q", buf, FLUID_CAPACITY_OFFSET, int(src_capacity * ONE_24))
        struct.pack_into("<H", buf, FLUID_FLAGS_OFFSET, 0)

        buf_ptr = ctypes.cast((ctypes.c_char * len(buf)).from_buffer(buf), ctypes.c_void_p)
        dst_amount_fp = int(dst_amount * ONE_24)
        dst_cap_fp = int(dst_capacity * ONE_24)

        flow_fp = native_fn(buf_ptr, dst_amount_fp, dst_cap_fp)
        return flow_fp / float(ONE_24)

    def transfer_fluid(
        self,
        dst_buffer: bytearray,
        src_buffer: bytearray,
        max_amount: float,
    ) -> float:
        """Execute native FluidBuffer::takeFluid to transfer fluid between buffers."""
        fn_addr = self.get_function_address("fluid_take_fluid")
        proto = ctypes.CFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
        )
        native_fn = proto(fn_addr)

        dst_ptr = ctypes.cast(
            (ctypes.c_char * len(dst_buffer)).from_buffer(dst_buffer),
            ctypes.c_void_p,
        )
        src_ptr = ctypes.cast(
            (ctypes.c_char * len(src_buffer)).from_buffer(src_buffer),
            ctypes.c_void_p,
        )
        max_fp = int(max_amount * ONE_24)
        transferred_fp = native_fn(dst_ptr, src_ptr, max_fp)
        return transferred_fp / float(ONE_24)

    def unsquash_transport_line(
        self,
        line_buffer: bytearray,
        delta_distance: int,
    ) -> None:
        """Execute native TransportLine::unsquash to expand squashed belt items."""
        fn_addr = self.get_function_address("belt_unsquash")
        proto = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)
        native_fn = proto(fn_addr)

        line_ptr = ctypes.cast(
            (ctypes.c_char * len(line_buffer)).from_buffer(line_buffer),
            ctypes.c_void_p,
        )
        native_fn(line_ptr, delta_distance)

    def move_transport_line(
        self,
        line_buffer: bytearray,
        delta_distance: int,
        tick: int = 1,
    ) -> int:
        """Execute native TransportLine::move to advance items along the line."""
        fn_addr = self.get_function_address("belt_move")
        proto = ctypes.CFUNCTYPE(
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_int,
        )
        native_fn = proto(fn_addr)
        line_ptr = ctypes.cast(
            (ctypes.c_char * len(line_buffer)).from_buffer(line_buffer),
            ctypes.c_void_p,
        )
        res_buf = (ctypes.c_uint32 * 2)()
        return int(
            native_fn(
                line_ptr,
                delta_distance,
                tick,
                None,
                None,
                ctypes.byref(res_buf),
                False,
                0,
            )
        )


def make_fluid_buffer(
    fluid_id: int = 0,
    amount: float = 0.0,
    capacity: float = DEFAULT_FLUID_CAPACITY,
    temperature: float = DEFAULT_FLUID_TEMP,
    min_temp: float = 0.0,
    max_temp: float = DEFAULT_FLUID_MAX_TEMP,
) -> bytearray:
    """Create a structured 64-byte FluidBuffer memory block."""
    buf = bytearray(FLUID_BUFFER_SIZE_BYTES)
    struct.pack_into("<h", buf, 0, fluid_id)
    struct.pack_into("<f", buf, FLUID_TEMP_OFFSET, temperature)
    struct.pack_into("<q", buf, FLUID_AMOUNT_OFFSET, int(amount * ONE_24))
    struct.pack_into("<q", buf, FLUID_CAPACITY_OFFSET, int(capacity * ONE_24))
    struct.pack_into("<f", buf, FLUID_MIN_TEMP_OFFSET, min_temp)
    struct.pack_into("<f", buf, FLUID_MAX_TEMP_OFFSET, max_temp)
    return buf


def read_fluid_buffer(buf: bytearray) -> dict[str, Any]:
    """Inspect fluid ID, temperature, amount, and capacity from a FluidBuffer block."""
    fluid_id = struct.unpack_from("<h", buf, 0)[0]
    temperature = struct.unpack_from("<f", buf, FLUID_TEMP_OFFSET)[0]
    amount = struct.unpack_from("<q", buf, FLUID_AMOUNT_OFFSET)[0] / float(ONE_24)
    capacity = struct.unpack_from("<q", buf, FLUID_CAPACITY_OFFSET)[0] / float(ONE_24)
    return {
        "fluid_id": fluid_id,
        "temperature": temperature,
        "amount": amount,
        "capacity": capacity,
    }


def make_transport_line_with_items(
    item_distances: list[int],
    deque_capacity: int = DEFAULT_DEQUE_CAPACITY,
    front_distance: int = 0,
    line_length: int = DEFAULT_LINE_LENGTH,
) -> tuple[bytearray, bytearray]:
    """Create a TransportLine and backing CompactDeque with specified item distances."""
    line = bytearray(TRANSPORT_LINE_SIZE_BYTES)
    deque = bytearray(deque_capacity * DEQUE_STRIDE_BYTES)

    count = len(item_distances)
    first_squashed = -1
    last_squashed = -1

    for idx, dist in enumerate(item_distances):
        struct.pack_into("<i", deque, idx * DEQUE_STRIDE_BYTES, dist)
        if dist < ITEM_STANDARD_SPACING:
            if first_squashed == -1:
                first_squashed = idx
            last_squashed = idx

    deque_ptr = ctypes.cast(
        (ctypes.c_char * len(deque)).from_buffer(deque),
        ctypes.c_void_p,
    ).value

    if first_squashed == last_squashed:
        first_squashed = -1

    struct.pack_into("<I", line, LINE_FRONT_OFFSET, front_distance)
    total_dist = sum(item_distances)
    back_dist = max(0, line_length - total_dist)
    struct.pack_into("<i", line, LINE_BACK_OFFSET, back_dist)
    struct.pack_into("<i", line, LINE_SPACING_OFFSET, ITEM_STANDARD_SPACING)
    struct.pack_into("<i", line, LINE_FIRST_SQUASHED_OFFSET, first_squashed)
    struct.pack_into("<i", line, LINE_LAST_SQUASHED_OFFSET, last_squashed)
    struct.pack_into("<i", line, LINE_LENGTH_OFFSET, line_length)
    struct.pack_into("<Q", line, LINE_DEQUE_PTR_OFFSET, deque_ptr if deque_ptr is not None else 0)
    struct.pack_into("<I", line, LINE_DEQUE_CAP_OFFSET, deque_capacity)
    struct.pack_into("<i", line, LINE_DEQUE_HEAD_OFFSET, 0)
    struct.pack_into("<i", line, LINE_ITEM_COUNT_OFFSET, count)
    struct.pack_into("<Q", line, LINE_AUX_PTR_OFFSET, 0)

    return line, deque


def read_item_distances(deque: bytearray, item_count: int) -> list[int]:
    """Read distance fields from items in a CompactDeque."""
    return [struct.unpack_from("<i", deque, i * DEQUE_STRIDE_BYTES)[0] for i in range(item_count)]


def read_transport_line(line: bytearray) -> dict[str, int]:
    """Inspect transport line state including front distance, back distance, and count."""
    return {
        "front_distance": struct.unpack_from("<I", line, LINE_FRONT_OFFSET)[0],
        "back_distance": struct.unpack_from("<i", line, LINE_BACK_OFFSET)[0],
        "item_count": struct.unpack_from("<i", line, LINE_ITEM_COUNT_OFFSET)[0],
        "first_squashed": struct.unpack_from("<i", line, LINE_FIRST_SQUASHED_OFFSET)[0],
        "last_squashed": struct.unpack_from("<i", line, LINE_LAST_SQUASHED_OFFSET)[0],
        "line_length": struct.unpack_from("<i", line, LINE_LENGTH_OFFSET)[0],
    }


def simulate_pipe_network(
    engine: FactorioNativeEngine,
    buffers: list[bytearray],
    ticks: int = 1,
) -> None:
    """Simulate fluid transfer across a series of connected pipe buffers."""
    for _ in range(ticks):
        for i in range(len(buffers) - 1):
            src = buffers[i]
            dst = buffers[i + 1]
            src_info = read_fluid_buffer(src)
            dst_info = read_fluid_buffer(dst)
            if src_info["amount"] > dst_info["amount"]:
                flow = engine.calculate_fluid_flow(
                    src_info["amount"],
                    src_info["capacity"],
                    dst_info["amount"],
                    dst_info["capacity"],
                )
                if flow > 0.0:
                    engine.transfer_fluid(dst, src, flow)


def validate_underground_belt_span(
    belt_name: str,
    dx: int,
    dy: int,
    prototypes: ProtoTable | None = None,
) -> bool:
    """Validate underground belt span distance and cardinal axis alignment."""
    if (dx != 0 and dy != 0) or (dx == 0 and dy == 0):
        return False
    distance = abs(dx) + abs(dy)
    if prototypes is None:
        from foundry.native_engine.verifier import load_game_prototypes

        prototypes = load_game_prototypes()

    ug_dict = prototypes.get("underground-belt", {})
    proto = ug_dict.get(belt_name)
    if proto is None:
        tb_dict = prototypes.get("transport-belt", {})
        tb_proto = tb_dict.get(belt_name, {})
        related = tb_proto.get("related_underground_belt")
        if related and related in ug_dict:
            proto = ug_dict[related]
    if proto is None or "max_distance" not in proto:
        return False
    return distance <= int(proto["max_distance"])


def _route_filtered_splitter_item(
    item: int,
    filter_item: int,
    output_1_blocked: bool,
    output_2_blocked: bool,
    out1: list[int],
    out2: list[int],
) -> None:
    """Route a single item through a filtered splitter into `out1` or `out2`."""
    if item == filter_item:
        if not output_1_blocked:
            out1.append(item)
    elif not output_2_blocked:
        out2.append(item)


def _route_unfiltered_splitter_item(
    item: int,
    toggle: int,
    output_1_blocked: bool,
    output_2_blocked: bool,
    out1: list[int],
    out2: list[int],
) -> int:
    """Route an unfiltered splitter item and return the updated toggle state."""
    if output_1_blocked and not output_2_blocked:
        out2.append(item)
        return toggle
    if output_2_blocked and not output_1_blocked:
        out1.append(item)
        return toggle
    if not output_1_blocked and not output_2_blocked:
        (out1 if toggle == 0 else out2).append(item)
        return 1 - toggle
    return toggle


def simulate_splitter_step(
    input_items: list[int],
    output_1_blocked: bool = False,
    output_2_blocked: bool = False,
    filter_item: int | None = None,
) -> tuple[list[int], list[int]]:
    """Simulate Factorio splitter routing for incoming items."""
    out1: list[int] = []
    out2: list[int] = []
    toggle = 0

    for item in input_items:
        if filter_item is not None:
            _route_filtered_splitter_item(
                item, filter_item, output_1_blocked, output_2_blocked, out1, out2
            )
        else:
            toggle = _route_unfiltered_splitter_item(
                item, toggle, output_1_blocked, output_2_blocked, out1, out2
            )

    return out1, out2
