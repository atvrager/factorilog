"""Symbol discovery and offset resolution for Factorio simulation binaries."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

DEFAULT_CANDIDATE_PATHS = [
    Path(os.environ.get("FACTORIO_BIN", "")),
    Path.home() / ".steam/steam/steamapps/common/Factorio/bin/x64_/factorio",
    Path.home() / ".local/share/Steam/steamapps/common/Factorio/bin/x64_/factorio",
    Path("/home/atv/.steam/steam/steamapps/common/Factorio/bin/x64_/factorio"),
    Path("/home/atv/.local/share/Steam/steamapps/common/Factorio/bin/x64_/factorio"),
    Path("/usr/bin/factorio"),
]

TARGET_SYMBOL_PATTERNS = {
    "fluid_get_flow_rate": r"^FluidBuffer::getFlowRate\(FluidBuffer const&, FluidBuffer const&\)$",
    "fluid_take_fluid": r"^FluidBuffer::takeFluid\(Fluid&, FixedPointNumberTemplate<long, 24u, true>\)$",
    "fluid_manager_update": r"^FluidFlowManager::update\(unsigned int\)$",
    "fluid_segment_build": r"^FluidSegmentManager::buildNewSegment\(std::vector<FluidBox\*, std::allocator<FluidBox\*> >&&, unsigned int\)$",
    "fluid_segment_merge": r"^FluidSegmentManager::tryToMergeSegments\(FluidBox&, unsigned short\)$",
    "fluid_box_push": r"^FluidBox::pushFluidAway\(\)$",
    "belt_update": r"^TransportLine::update\(MapTick, unsigned int\)$",
    "belt_move": r"^TransportLine::move\(FixedPointNumberTemplate<int, 8u, false>, MapTick, TransportLine\*, TransportLine\*, SplitterMovementResult&, bool, FixedPointNumberTemplate<int, 8u, false>\)$",
    "belt_unsquash": r"^TransportLine::unsquash\(FixedPointNumberTemplate<int, 8u, false>\)$",
    "belt_try_to_insert": r"^TransportLine::tryToInsert\(ItemToInsert, FixedPointNumberTemplate<int, 8u, false>, FixedPointNumberTemplate<int, 8u, false>, bool\)$",
    "belt_try_to_insert_at_back": r"^TransportLine::tryToInsertAtBack\(ItemToInsert, FixedPointNumberTemplate<int, 8u, false>, bool\)$",
    "splitter_resolve": r"^SplitterLogic::resolveSplitting\(SplitterDirection, TransportLine&, TransportLine&, TransportLine&, TransportLine&, MapTick, bool&, bool\)$",
}

COMPILED_SYMBOL_PATTERNS = {k: re.compile(p) for k, p in TARGET_SYMBOL_PATTERNS.items()}
NM_SYMBOL_PARTS = 3
HEX_BASE = 16


def _parse_nm_output(stdout: str) -> dict[str, int]:
    """Parse demangled nm output lines into a symbol-to-address map."""
    raw_map: dict[str, int] = {}
    for line in stdout.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) == NM_SYMBOL_PARTS:
            addr_hex, _, demangled = parts
            try:
                raw_map[demangled] = int(addr_hex, HEX_BASE)
            except ValueError:
                continue
    return raw_map


def resolve_factorio_binary(custom_path: str | Path | None = None) -> Path | None:
    """Find a readable Factorio executable on the system."""
    if custom_path:
        p = Path(custom_path).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p

    for candidate in DEFAULT_CANDIDATE_PATHS:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()

    return None


class FactorioSymbolFinder:
    """Resolves simulation function offsets from an unstripped Factorio ELF."""

    def __init__(self, binary_path: str | Path) -> None:
        self.binary_path = Path(binary_path).resolve()
        if not self.binary_path.is_file():
            raise FileNotFoundError(f"Binary file not found: {self.binary_path}")
        self._offsets: dict[str, int] = {}
        self._scan_symbols()

    def _scan_symbols(self) -> None:
        """Read .symtab using nm and match demangled target patterns."""
        cmd = ["nm", "-C", str(self.binary_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as err:
            raise RuntimeError(
                f"Failed to read symbol table from {self.binary_path}: {err}"
            ) from err

        raw_map = _parse_nm_output(proc.stdout)
        for key, regex in COMPILED_SYMBOL_PATTERNS.items():
            for sym, addr in raw_map.items():
                if regex.search(sym):
                    self._offsets[key] = addr
                    break

    def get_offset(self, symbol_key: str) -> int:
        """Get the VMA offset for a required simulation routine."""
        if symbol_key not in self._offsets:
            raise KeyError(f"Symbol '{symbol_key}' not resolved in {self.binary_path.name}")
        return self._offsets[symbol_key]

    def has_symbol(self, symbol_key: str) -> bool:
        """Check whether a specific symbol was resolved."""
        return symbol_key in self._offsets

    @property
    def all_offsets(self) -> dict[str, int]:
        """Dictionary copy of all resolved symbol offsets."""
        return dict(self._offsets)
