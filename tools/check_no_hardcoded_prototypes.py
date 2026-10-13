"""AST linter that forbids hardcoded prototype constants and expansion items.

Enforces dynamic prototype discovery from game data.
Forbids hardcoded spans, speeds, and expansion content like turbo belts.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Expansion prototype names not present in core Factorio
FORBIDDEN_EXPANSION_PATTERNS = re.compile(
    r"turbo-(transport-belt|underground-belt|splitter|loader)",
    re.IGNORECASE,
)

# Constant identifiers for spans, distances, or speeds (module or class level)
FORBIDDEN_SPAN_CONST_PATTERNS = re.compile(
    r"^(MAX|DEFAULT)_.*(SPAN|DISTANCE|SPEED)",
)

# Variable names for span or distance that must not take numeric literals
SPAN_NUMERIC_VAR_PATTERNS = re.compile(
    r".*(span|distance|max_distance)$",
    re.IGNORECASE,
)

FORBIDDEN_TIER_TABLE_NAMES = frozenset(
    {
        "known_tiers",
        "CORE_BELT_TIERS",
        "BELT_TIER_TABLE",
        "TIER_TABLE",
        "BELT_TIERS",
        "PIPE_TIERS",
    }
)

EXCLUDED_FILES = frozenset(
    {
        "pdk.py",
        "make_fixture.py",
        "solve.py",
        "belt_library.py",
        "entity_helpers.py",
        "belt_fuzzer.py",
    }
)


class HardcodedPrototypeVisitor(ast.NodeVisitor):
    """AST visitor that detects hardcoded prototypes and expansion entities."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[str] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        """Check string constants for expansion entity names."""
        if isinstance(node.value, str) and FORBIDDEN_EXPANSION_PATTERNS.search(node.value):
            line = getattr(node, "lineno", 0)
            self.violations.append(
                f"{self.filename}:{line}: Forbidden expansion prototype '{node.value}'. "
                f"Use core prototypes only or query dynamically."
            )
        self.generic_visit(node)

    def _check_target_and_value(self, target: ast.AST, value: ast.AST, lineno: int) -> None:
        target_name = getattr(target, "id", None)
        if not target_name:
            return

        # Check uppercase span constants
        if FORBIDDEN_SPAN_CONST_PATTERNS.match(target_name):
            self.violations.append(
                f"{self.filename}:{lineno}: Forbidden hardcoded span constant '{target_name}'. "
                f"Query prototype limits dynamically."
            )
            return

        # Check numeric literal assignments to span variables
        if (
            SPAN_NUMERIC_VAR_PATTERNS.match(target_name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, (int, float))
            and not isinstance(value.value, bool)
        ):
            self.violations.append(
                f"{self.filename}:{lineno}: Forbidden hardcoded numeric assignment to '{target_name}' ({value.value}). "
                f"Query prototype limits dynamically from game data."
            )
            return

        # Check hardcoded tier lists
        if target_name in FORBIDDEN_TIER_TABLE_NAMES:
            self.violations.append(
                f"{self.filename}:{lineno}: Forbidden hardcoded tier table '{target_name}'. "
                f"Query tiers dynamically from game prototypes."
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        """Check variable assignments for hardcoded spans or tier tables."""
        for target in node.targets:
            self._check_target_and_value(target, node.value, getattr(node, "lineno", 0))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Check annotated variable assignments."""
        if node.value is not None:
            self._check_target_and_value(node.target, node.value, getattr(node, "lineno", 0))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Check for fallback defaults in prototype property lookups."""
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
        ):
            key = str(node.args[0].value)
            if key in ("max_distance", "max_underground_distance", "speed"):
                default_arg = node.args[1]
                if isinstance(default_arg, ast.Constant) and isinstance(
                    default_arg.value, (int, float)
                ):
                    line = getattr(node, "lineno", 0)
                    self.violations.append(
                        f"{self.filename}:{line}: Forbidden fallback default for prototype parameter '{key}' ({default_arg.value}). "
                        f"Query prototype values dynamically without hardcoded fallback assumptions."
                    )
        self.generic_visit(node)


def check_file(path: Path) -> list[str]:
    """Check a single Python file for hardcoded prototype constructs."""
    if path.name.startswith("test_") or path.name in EXCLUDED_FILES:
        return []
    try:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(path))
    except Exception as exc:
        return [f"{path}: Failed to parse AST: {exc}"]

    visitor = HardcodedPrototypeVisitor(str(path))
    visitor.visit(tree)
    return visitor.violations


def check_directory(dir_path: Path) -> list[str]:
    """Recursively check Python files in a directory."""
    violations: list[str] = []
    for p in sorted(dir_path.rglob("*.py")):
        if p.is_file():
            violations.extend(check_file(p))
    return violations


def main() -> int:
    """Run prototype linter across foundry sources."""
    if len(sys.argv) > 1:
        target_path = Path(sys.argv[1])
        all_violations = (
            check_file(target_path) if target_path.is_file() else check_directory(target_path)
        )
    else:
        root = Path(__file__).resolve().parent.parent
        target_dirs = [
            root / "foundry" / "router",
            root / "foundry" / "native_engine",
        ]
        all_violations = []
        for d in target_dirs:
            if d.is_dir():
                all_violations.extend(check_directory(d))

    if all_violations:
        for v in all_violations:
            print(v, file=sys.stderr)
        return 1

    print("check_no_hardcoded_prototypes: clean (0 violations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
