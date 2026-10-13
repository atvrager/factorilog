"""Native simulation engine loader and execution interface for Factorio."""

from __future__ import annotations

import ctypes
import os
import struct
from pathlib import Path
from typing import Any

from foundry.native_engine.symbol_finder import (
    FactorioSymbolFinder,
    resolve_factorio_binary,
)

ONE_24 = 1 << 24
ONE_8 = 1 << 8


class FactorioNativeEngine:
    """Prepares, loads, and executes native Factorio simulation functions."""

    def __init__(
        self,
        binary_path: str | Path | None = None,
        cache_dir: str | Path = "/tmp/factorilog_engine",
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

        # Stream copy to cache directory
        with self.binary_path.open("rb") as f_in, target_so.open("wb") as f_out:
            chunk = 16 * 1024 * 1024
            while buf := f_in.read(chunk):
                f_out.write(buf)

        # Patch PT_INTERP program header (offset 120, uint32 0x03 -> 0x00)
        with target_so.open("r+b") as f:
            f.seek(120)
            f.write(struct.pack("<I", 0))

            # Patch DT_FLAGS_1: locate tag 0x6ffffffb and mask out DF_1_PIE (0x08000000)
            f.seek(0)
            content = bytearray(f.read())
            tag_bytes = struct.pack("<Q", 0x6FFFFFFB)
            tag_idx = content.find(tag_bytes)
            if tag_idx != -1:
                val = struct.unpack("<Q", content[tag_idx + 8 : tag_idx + 16])[0]
                new_val = val & ~0x08000000
                content[tag_idx + 8 : tag_idx + 16] = struct.pack("<Q", new_val)
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

        # Resolve lowest virtual address mapped from the library file (ELF load base)
        target_name = self.lib_path.name
        with Path("/proc/self/maps").open(encoding="utf-8") as m:
            for line in m:
                if target_name in line and "00000000" in line:
                    self._base_address = int(line.split("-")[0], 16)
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

        buf = bytearray(64)
        struct.pack_into("<q", buf, 8, int(src_amount * ONE_24))
        struct.pack_into("<q", buf, 0x10, int(src_capacity * ONE_24))
        struct.pack_into("<H", buf, 0x24, 0)

        buf_ptr = ctypes.cast((ctypes.c_char * len(buf)).from_buffer(buf), ctypes.c_void_p)
        dst_amount_fp = int(dst_amount * ONE_24)
        dst_cap_fp = int(dst_capacity * ONE_24)

        flow_fp = native_fn(buf_ptr, dst_amount_fp, dst_cap_fp)
        return flow_fp / float(ONE_24)
