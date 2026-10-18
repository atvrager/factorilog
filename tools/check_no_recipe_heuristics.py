"""AST linter that forbids recipe-specific and item-specific conditionals in foundry compiler code.

Physical layout, routing, and netlist synthesis algorithms must operate purely
on cell geometry, graph topology, pin coordinates, and commodity counts.
Branching on specific Factorio recipes or material strings is strictly prohibited.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

FORBIDDEN_RECIPE_PATTERNS = re.compile(
    r"(^|[-_])(science(-pack)?|plate|ore|gear|cable|brick|magazine|grenade|wall|circuit"
    r"|plastic|sulfur|battery|explosives|robot-frame|engine|turret|ammo)($|[-_])",
    re.IGNORECASE,
)

FORBIDDEN_LITERALS = frozenset(
    {
        "automation-science-pack",
        "logistic-science-pack",
        "military-science-pack",
        "chemical-science-pack",
        "production-science-pack",
        "utility-science-pack",
        "space-science-pack",
        "automation-science",
        "logistic-science",
        "military-science",
        "chemical-science",
        "production-science",
        "utility-science",
        "space-science",
        "iron-plate",
        "copper-plate",
        "steel-plate",
        "stone-brick",
        "iron-gear-wheel",
        "copper-cable",
        "electronic-circuit",
        "advanced-circuit",
        "processing-unit",
        "iron-ore",
        "copper-ore",
        "coal",
        "stone",
        "uranium-ore",
        "firearm-magazine",
        "piercing-rounds-magazine",
        "grenade",
        "stone-wall",
        "gear_belt_combo",
        "gear_inserter_combo",
    }
)

FORBIDDEN_SUFFIXES = frozenset({"-ore", "-plate", "-cable", "-wheel", "-brick", "-wall"})

HARDWARE_CATEGORIES = frozenset(
    {
        "assembling-machine",
        "furnace",
        "electric-furnace",
        "steel-furnace",
        "stone-furnace",
        "rocket-silo",
        "inserter",
        "fast-inserter",
        "long-handed-inserter",
        "filter-inserter",
        "stack-inserter",
        "transport-belt",
        "fast-transport-belt",
        "express-transport-belt",
        "underground-belt",
        "fast-underground-belt",
        "express-underground-belt",
        "splitter",
        "fast-splitter",
        "express-splitter",
        "electric-pole",
        "small-electric-pole",
        "medium-electric-pole",
        "big-electric-pole",
        "substation",
        "pipe",
        "pump",
        "combinator",
        "lab",
        "radar",
        "lamp",
        "boiler",
        "steam-engine",
    }
)

EXCLUDED_FILES = frozenset(
    {
        "solve.py",
        "pdk.py",
        "make_fixture.py",
    }
)


def _is_forbidden_string(val: str) -> bool:
    """Return True if val is a specific recipe or item string."""
    if val in HARDWARE_CATEGORIES or val == "circuit_cell":
        return False
    if val in FORBIDDEN_LITERALS:
        return True
    if val in FORBIDDEN_SUFFIXES:
        return True
    return bool(FORBIDDEN_RECIPE_PATTERNS.search(val))


class RecipeHeuristicVisitor(ast.NodeVisitor):
    """AST visitor that checks for recipe-specific branching in layout code."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[str] = []
        self._in_condition = False

    def _check_string_constant(self, node: ast.AST, val: str, context: str) -> None:
        if _is_forbidden_string(val):
            line = getattr(node, "lineno", 0)
            self.violations.append(
                f"{self.filename}:{line}: Forbidden recipe-specific heuristic ({context}): "
                f"literal '{val}' used in conditional logic. Physical layout must be recipe-agnostic."
            )

    def _inspect_condition_node(self, node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                self._check_string_constant(child, child.value, "conditional expression")
            elif isinstance(child, (ast.Tuple, ast.List)):
                for elt in child.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        self._check_string_constant(elt, elt.value, "collection test")

    def visit_If(self, node: ast.If) -> None:
        self._inspect_condition_node(node.test)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._inspect_condition_node(node.test)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        for if_clause in node.ifs:
            self._inspect_condition_node(if_clause)
        self.generic_visit(node)


def check_file(path: Path) -> list[str]:
    """Check a single Python file for recipe heuristics."""
    if path.name.startswith("test_") or path.name in EXCLUDED_FILES:
        return []
    try:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(path))
    except Exception as exc:
        return [f"{path}: Failed to parse AST: {exc}"]

    visitor = RecipeHeuristicVisitor(str(path))
    visitor.visit(tree)
    return visitor.violations


def check_directory(dir_path: Path) -> list[str]:
    """Check all Python files in a directory."""
    violations: list[str] = []
    for py_file in sorted(dir_path.glob("*.py")):
        violations.extend(check_file(py_file))
    return violations


def main() -> int:
    """CLI entrypoint."""
    target_dirs = sys.argv[1:] if len(sys.argv) > 1 else ["foundry"]
    all_violations: list[str] = []
    for target in target_dirs:
        p = Path(target)
        if p.is_file():
            all_violations.extend(check_file(p))
        elif p.is_dir():
            all_violations.extend(check_directory(p))

    if all_violations:
        sys.stderr.write("RECIPE HEURISTIC LINTER ERROR: Found forbidden recipe-specific logic:\n")
        for v in all_violations:
            sys.stderr.write(f"  {v}\n")
        return 1

    sys.stdout.write("Recipe heuristic linter: PASSED (no recipe-specific conditionals found)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
