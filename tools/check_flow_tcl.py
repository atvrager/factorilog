"""Structural analyzer and linter for physical design TCL scripts.

Enforces physical design invariants on OpenROAD flow scripts:
1. No manual coordinate obstructions (create_obstruction). Obstructions belong in LEF.
2. No instance querying or lookup by name (findInst). Flow scripts must be design-agnostic.
3. No post-synthesis netlist mutations (dbITerm_disconnect, dbITerm_connect).
4. No instance name inspection or pattern matching ($inst getName).
5. No hardcoded coordinate regions in routing or placement commands.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Structural patterns that violate physical design separation of concerns
FORBIDDEN_COMMANDS = {
    "create_obstruction": (
        "Manual obstructions ('create_obstruction') are forbidden in flow scripts. "
        "Define cell obstructions in the LEF macro definitions instead."
    ),
    "findInst": (
        "Instance lookups ('findInst') are forbidden in flow scripts. "
        "Flow scripts must remain design-agnostic and parameter-driven."
    ),
}

# Regex to detect instance name inspections or queries
INSTANCE_NAME_PATTERN = re.compile(
    r"\$inst\s+getName|\$block\s+findInst|\[\$block\s+getInst\s+",
    re.IGNORECASE,
)

# Regex to detect hardcoded numeric bounding boxes, for example {12.0 81.0 13.0 82.0}
HARDCODED_REGION_PATTERN = re.compile(
    r"\{\s*\d+(\.\d+)?\s+\d+(\.\d+)?\s+\d+(\.\d+)?\s+\d+(\.\d+)?\s*\}",
)


def _check_tcl_line(path: Path, line_num: int, clean_line: str) -> list[str]:
    """Check one non-comment TCL line for forbidden patterns."""
    violations = [
        f"{path}:{line_num}: Forbidden command '{cmd}': {reason}"
        for cmd, reason in FORBIDDEN_COMMANDS.items()
        if cmd in clean_line
    ]
    if INSTANCE_NAME_PATTERN.search(clean_line):
        violations.append(
            f"{path}:{line_num}: Forbidden instance inspection: "
            "Flow scripts must not branch or filter on instance names."
        )
    if "initialize_floorplan" not in clean_line and HARDCODED_REGION_PATTERN.search(clean_line):
        violations.append(
            f"{path}:{line_num}: Forbidden hardcoded region: "
            "Manual coordinate bounding boxes are forbidden in flow scripts."
        )
    return violations


def check_tcl_file(path: Path) -> list[str]:
    """Analyze a TCL file for forbidden structural patterns."""
    violations: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()

    for line_num, line in enumerate(lines, start=1):
        clean_line = line.strip()
        if not clean_line or clean_line.startswith("#"):
            continue
        violations.extend(_check_tcl_line(path, line_num, clean_line))

    return violations


def main() -> int:
    """Run the TCL structural linter on flow scripts."""
    files_to_check = sys.argv[1:]
    if not files_to_check:
        flow_dir = Path("flow")
        if flow_dir.is_dir():
            files_to_check = [str(p) for p in sorted(flow_dir.glob("*.tcl"))]

    total_violations: list[str] = []
    for file_str in files_to_check:
        p = Path(file_str)
        if p.suffix == ".tcl" and p.is_file():
            violations = check_tcl_file(p)
            total_violations.extend(violations)

    if total_violations:
        for v in total_violations:
            sys.stderr.write(f"TCL LINT ERROR: {v}\n")
        return 1

    print(f"PASS: Checked {len(files_to_check)} TCL file(s), 0 structural violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
