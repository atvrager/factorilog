"""AST linter that forbids gameplay cheats in bot, campaign, and multiplayer modules.

Autonomous gameplay and starter base construction must execute purely through
Factorio UDP InputAction packets within native character reach limits.
Lua scenario scripts in bot, campaign, and multiplayer modules may only inspect
world state, emit telemetry JSON, capture screenshots, or adjust game.speed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# Maximum allowed stop_tolerance in tiles (must stay within native reach limits).
MAX_ALLOWED_STOP_TOLERANCE = 3.5

TARGET_FILENAMES = frozenset(
    {
        "mp_proxy.py",
        "bot_agent.py",
        "cartographer.py",
        "test_mp_proxy.py",
        "test_bot_agent.py",
    }
)

FORBIDDEN_LUA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bcharacter_build_distance_bonus\b"),
        "Forbidden character_build_distance_bonus cheat",
    ),
    (
        re.compile(r"\bcharacter_reach_distance_bonus\b"),
        "Forbidden character_reach_distance_bonus cheat",
    ),
    (
        re.compile(r"\bcharacter_resource_reach_distance_bonus\b"),
        "Forbidden character_resource_reach_distance_bonus cheat",
    ),
    (
        re.compile(r"\bcharacter_running_speed_modifier\b"),
        "Forbidden character_running_speed_modifier cheat",
    ),
    (
        re.compile(r"\bcharacter_mining_speed_modifier\b"),
        "Forbidden character_mining_speed_modifier cheat",
    ),
    (
        re.compile(r"\bcharacter_crafting_speed_modifier\b"),
        "Forbidden character_crafting_speed_modifier cheat",
    ),
    (
        re.compile(r"\.teleport\s*\("),
        "Forbidden Lua .teleport() cheat; use collision-free walking InputActions",
    ),
    (
        re.compile(r"\.insert\s*\("),
        "Forbidden Lua .insert() item grant cheat; craft or mine items via InputActions",
    ),
    (
        re.compile(r"\.remove_item\s*\("),
        "Forbidden Lua .remove_item() inventory mutation; consume items via InputActions",
    ),
    (
        re.compile(r"\.destroy\s*\("),
        "Forbidden Lua .destroy() obstacle or entity wipe cheat; mine via InputActions",
    ),
    (
        re.compile(r"\.set_recipe\s*\("),
        "Forbidden Lua .set_recipe() cheat; use SetupAssemblingMachine InputAction",
    ),
    (
        re.compile(r"\.researched\s*=\s*true\b"),
        "Forbidden Lua .researched = true cheat; research via lab and StartResearch InputAction",
    ),
    (
        re.compile(r"\.add_research\s*\("),
        "Forbidden Lua .add_research() cheat; use StartResearch InputAction",
    ),
    (
        re.compile(r"\bphase1_bom_granted\b"),
        "Forbidden phase1_bom_granted item grant trigger",
    ),
    (
        re.compile(r"\bsurface\.create_entity\s*\("),
        "Forbidden Lua surface.create_entity() in bot execution; build via InputAction::Build",
    ),
    (
        re.compile(r"\bp\.surface\.create_entity\s*\("),
        "Forbidden Lua p.surface.create_entity() in bot execution; build via InputAction::Build",
    ),
)

FORBIDDEN_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "build_lua_plan_executor_snippet",
        "build_lua_progressive_plan_executor_snippet",
    }
)

FORBIDDEN_PARAMETER_NAMES: frozenset[str] = frozenset(
    {
        "extra_items",
        "phase1_bom_items",
    }
)


def _is_target_file(path: Path) -> bool:
    """Return True if path is a bot, campaign, or multiplayer module checked by this linter."""
    if path.name in TARGET_FILENAMES:
        return True
    return "campaign" in path.parts


class NoBotCheatsVisitor(ast.NodeVisitor):
    """AST visitor that detects forbidden Lua cheats and reach overrides."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[str] = []

    def _check_string(self, node: ast.AST, text: str) -> None:
        line = getattr(node, "lineno", 0)
        for pattern, message in FORBIDDEN_LUA_PATTERNS:
            if pattern.search(text):
                self.violations.append(f"{self.filename}:{line}: {message}")

    def visit_Constant(self, node: ast.Constant) -> None:
        """Inspect string constants for forbidden Lua API calls."""
        if isinstance(node.value, str):
            self._check_string(node, node.value)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Inspect function definitions for forbidden Lua executor generators and parameters."""
        if node.name in FORBIDDEN_FUNCTION_NAMES:
            self.violations.append(
                f"{self.filename}:{node.lineno}: Forbidden Lua plan executor function "
                f"'{node.name}'; execute plans via UDP InputActions."
            )
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if arg.arg in FORBIDDEN_PARAMETER_NAMES:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: Forbidden item-spawning parameter "
                    f"'{arg.arg}' in '{node.name}'."
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Inspect function calls for forbidden executors, item grants, and stop_tolerance cheats."""
        if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_FUNCTION_NAMES:
            self.violations.append(
                f"{self.filename}:{node.lineno}: Forbidden call to '{node.func.attr}'."
            )
        for kw in node.keywords:
            if kw.arg in FORBIDDEN_PARAMETER_NAMES:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: Forbidden keyword argument '{kw.arg}'."
                )
            if (
                kw.arg == "stop_tolerance"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, (int, float))
                and not isinstance(kw.value.value, bool)
                and float(kw.value.value) > MAX_ALLOWED_STOP_TOLERANCE
            ):
                self.violations.append(
                    f"{self.filename}:{node.lineno}: Forbidden stop_tolerance={kw.value.value} "
                    f"(exceeds max allowed {MAX_ALLOWED_STOP_TOLERANCE})."
                )
        self.generic_visit(node)


def check_file(path: Path, force: bool = False) -> list[str]:
    """Check a single Python file for forbidden gameplay cheats."""
    if not force and not _is_target_file(path):
        return []
    try:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(path))
    except Exception as exc:
        return [f"{path}: Failed to parse AST: {exc}"]

    visitor = NoBotCheatsVisitor(str(path))
    visitor.visit(tree)
    return visitor.violations


def check_directory(dir_path: Path) -> list[str]:
    """Check all target Python files in a directory."""
    violations: list[str] = []
    for path in sorted(dir_path.rglob("*.py")):
        violations.extend(check_file(path))
    return violations


def main() -> int:
    """CLI entry point for the anti-cheat AST linter."""
    import sys

    targets = (
        [Path("foundry"), Path("test")]
        if len(sys.argv) < 2
        else [Path(arg) for arg in sys.argv[1:]]
    )
    violations: list[str] = []
    for target in targets:
        if target.is_file():
            violations.extend(check_file(target, force=True))
        elif target.is_dir():
            violations.extend(check_directory(target))

    if violations:
        for v in violations:
            sys.stderr.write(f"{v}\n")
        sys.stderr.write(f"\nFound {len(violations)} forbidden bot/MP gameplay cheat(s).\n")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
