"""Validate Factorio blueprint strings with headless Factorio.

Loads blueprint strings into a temporary Factorio mod.
Runs the game in headless mode to verify entity placement.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_GAME_PATH = Path(
    os.path.expanduser("~/.steam/steam/steamapps/common/Factorio/bin/x64/factorio")
)
FALLBACK_GAME_PATHS: tuple[Path, ...] = (
    Path("/scratch/opt/factorio/bin/x64/factorio"),
    Path("/opt/factorio/bin/x64/factorio"),
)
HOME_DIR = Path("/home")
STEAM_REL_GLOB = "*/.steam/steam/steamapps/common/Factorio/bin/x64/factorio"
LOG_PREFIXES: tuple[str, ...] = (
    "RESULT_",
    "COUNT_",
    "UNPLACED_",
    "REASON_",
    "BOUNDS_",
    "SAMPLE_FAILURE:",
    "FLUID_CLASH:",
)


def find_factorio_binary(
    override: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Find the Factorio executable binary."""
    if override and override.is_file():
        return override
    env_map = os.environ if env is None else env
    env_game = env_map["FACTORIO_BIN"] if "FACTORIO_BIN" in env_map else ""
    if env_game and Path(env_game).is_file():
        return Path(env_game)
    which_bin = shutil.which("factorio")
    if which_bin and Path(which_bin).is_file():
        return Path(which_bin)
    if DEFAULT_GAME_PATH.is_file():
        return DEFAULT_GAME_PATH
    for candidate in (*FALLBACK_GAME_PATHS, *HOME_DIR.glob(STEAM_REL_GLOB)):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Factorio binary not found.")


def validate_blueprint(blueprint_string: str, game_bin: Path, work_dir: Path) -> dict[str, int]:
    """Load blueprint in headless Factorio and count placed entities."""
    mods_dir = work_dir / "mods"
    mod_dir = mods_dir / "bpcheck"
    data_dir = work_dir / "data"

    mod_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Write private config
    config_file = work_dir / "config.ini"
    config_file.write_text(
        f"[path]\nread-data=__PATH__executable__/../../data\nwrite-data={data_dir}\n"
    )

    # Write mod metadata
    info_file = mod_dir / "info.json"
    info_file.write_text(
        """{
  "name": "bpcheck",
  "version": "0.1.0",
  "title": "Blueprint Check Mod",
  "author": "factorilog",
  "factorio_version": "2.1",
  "dependencies": ["base >= 2.1"]
}
"""
    )

    # Write blueprint string as Lua module
    bp_lua = mod_dir / "blueprint.lua"
    bp_lua.write_text(f'return "{blueprint_string.strip()}"\n')

    # Write control script
    control_lua = mod_dir / "control.lua"
    control_lua.write_text(
        """
local BLUEPRINT = require("blueprint")

script.on_init(function()
    local surface = game.surfaces[1]
    for cx = -10, 10 do
        for cy = -10, 10 do
            surface.request_to_generate_chunks({cx * 32, cy * 32}, 1)
        end
    end
    surface.force_generate_chunk_requests()

    local tiles = {}
    for x = -300, 300 do
        for y = -300, 300 do
            tiles[#tiles + 1] = {name = "lab-dark-1", position = {x, y}}
        end
    end
    surface.set_tiles(tiles)
    for _, entity in pairs(surface.find_entities_filtered{area = {{-300, -300}, {300, 300}}}) do
        if entity.name ~= "character" then
            entity.destroy()
        end
    end

    local inv = game.create_inventory(1)
    local stack = inv[1]
    stack.set_stack{name = "blueprint", count = 1}
    local res = stack.import_stack(BLUEPRINT)
    log("RESULT_IMPORT: " .. tostring(res))

    local built = stack.build_blueprint{
        surface = surface,
        force = game.forces.player,
        position = {0, 0},
        force_build = true
    }
    log("RESULT_BUILT: " .. tostring(built and #built or 0))

    local ghosts = surface.find_entities_filtered{name = "entity-ghost"}
    log("RESULT_GHOSTS: " .. tostring(#ghosts))

    local counts = {}
    for _, g in pairs(ghosts) do
        local gn = g.ghost_name
        counts[gn] = (counts[gn] or 0) + 1
    end
    for k, v in pairs(counts) do
        log("COUNT_" .. k .. ": " .. tostring(v))
    end

    local min_gx, max_gx, min_gy, max_gy = 1e9, -1e9, 1e9, -1e9
    for _, g in pairs(ghosts) do
        if g.position.x < min_gx then min_gx = g.position.x end
        if g.position.x > max_gx then max_gx = g.position.x end
        if g.position.y < min_gy then min_gy = g.position.y end
        if g.position.y > max_gy then max_gy = g.position.y end
    end
    log(string.format("BOUNDS_GHOSTS: x=[%.1f, %.1f], y=[%.1f, %.1f]", min_gx, max_gx, min_gy, max_gy))

    local bp_entities = stack.get_blueprint_entities() or {}
    log("RESULT_TOTAL: " .. tostring(#bp_entities))

    local min_bx, max_bx, min_by, max_by = 1e9, -1e9, 1e9, -1e9
    for _, e in pairs(bp_entities) do
        if e.position.x < min_bx then min_bx = e.position.x end
        if e.position.x > max_bx then max_bx = e.position.x end
        if e.position.y < min_by then min_by = e.position.y end
        if e.position.y > max_by then max_by = e.position.y end
    end
    log(string.format("BOUNDS_BP: x=[%.1f, %.1f], y=[%.1f, %.1f]", min_bx, max_bx, min_by, max_by))

    local shift_x = math.floor((min_gx - min_bx) + 0.5)
    local shift_y = math.floor((min_gy - min_by) + 0.5)
    log(string.format("BOUNDS_SHIFT: dx=%d, dy=%d", shift_x, shift_y))

    -- Map placed ghost positions
    local placed = {}
    for _, g in pairs(ghosts) do
        local key = string.format("%.2f,%.2f", g.position.x, g.position.y)
        placed[key] = g
    end

    local unplaced_by_name = {}
    local blocked_by_reason = {}
    local sample_failures = {}
    local match_count = 0
    local unplaced_count = 0

    for _, e in pairs(bp_entities) do
        local sx = e.position.x + shift_x
        local sy = e.position.y + shift_y
        local key = string.format("%.2f,%.2f", sx, sy)
        local ghost = placed[key]
        if ghost and ghost.ghost_name == e.name then
            match_count = match_count + 1
        else
            unplaced_count = unplaced_count + 1
            unplaced_by_name[e.name] = (unplaced_by_name[e.name] or 0) + 1

            -- Find colliding entity at this surface position
            local colliders = surface.find_entities_filtered{
                position = {sx, sy}
            }
            local obstacle_name = "none"
            for _, c in pairs(colliders) do
                local name = c.name == "entity-ghost" and c.ghost_name or c.name
                if name ~= e.name then
                    obstacle_name = name
                    break
                end
            end
            local reason = e.name .. " blocked by " .. obstacle_name
            blocked_by_reason[reason] = (blocked_by_reason[reason] or 0) + 1
            if #sample_failures < 15 then
                table.insert(sample_failures, string.format("%s at (%.1f, %.1f) [surface (%.1f, %.1f)] blocked by %s", e.name, e.position.x, e.position.y, sx, sy, obstacle_name))
            end
        end
    end

    log("RESULT_MATCHED: " .. tostring(match_count))
    log("RESULT_UNPLACED_TOTAL: " .. tostring(unplaced_count))

    for k, v in pairs(unplaced_by_name) do
        log("UNPLACED_" .. k .. ": " .. tostring(v))
    end
    for k, v in pairs(blocked_by_reason) do
        log("REASON_" .. k .. ": " .. tostring(v))
    end
    for _, s in ipairs(sample_failures) do
        log("SAMPLE_FAILURE: " .. s)
    end

    -- Revive all ghosts into real entities
    local revived_count = 0
    for _, g in ipairs(ghosts) do
        if g.valid then
            local ok = g.silent_revive()
            if ok then
                revived_count = revived_count + 1
            end
        end
    end
    log("RESULT_REVIVED: " .. tostring(revived_count))

    -- Map all pipes by coordinates
    local pipes = surface.find_entities_filtered{name = {"pipe", "pipe-to-ground"}}
    local pipe_map = {}
    for _, p in ipairs(pipes) do
        pipe_map[string.format("%.1f,%.1f", p.position.x, p.position.y)] = p
    end

    local function inject_pipe(x, y, fluid_name)
        local key = string.format("%.1f,%.1f", x, y)
        local p = pipe_map[key]
        if p and p.valid then
            local ins = p.insert_fluid{name = fluid_name, amount = 1000}
            return ins, p
        end
        return 0, nil
    end

    -- Inject fluids from machine ports
    local machines = surface.find_entities_filtered{type = {"assembling-machine", "furnace"}}
    local port_injects = {}
    for _, m in ipairs(machines) do
        local r = m.get_recipe()
        local cx, cy = m.position.x, m.position.y
        if m.name == "oil-refinery" and r and r.name == "advanced-oil-processing" then
            table.insert(port_injects, {cx - 3, cy - 1, "water", "oil-refinery water in"})
            table.insert(port_injects, {cx - 3, cy + 1, "crude-oil", "oil-refinery crude in"})
            table.insert(port_injects, {cx + 3, cy - 2, "heavy-oil", "oil-refinery heavy out"})
            table.insert(port_injects, {cx + 3, cy, "light-oil", "oil-refinery light out"})
            table.insert(port_injects, {cx + 3, cy + 2, "petroleum-gas", "oil-refinery petgas out"})
        elseif m.name == "chemical-plant" and r then
            if r.name == "basic-oil-processing" then
                table.insert(port_injects, {cx - 2, cy - 1, "crude-oil", "chem crude in"})
            elseif r.name == "heavy-oil-cracking" then
                table.insert(port_injects, {cx - 2, cy - 1, "heavy-oil", "chem heavy in"})
                table.insert(port_injects, {cx - 2, cy + 1, "water", "chem water in"})
                table.insert(port_injects, {cx + 2, cy - 1, "light-oil", "chem light out"})
            elseif r.name == "light-oil-cracking" then
                table.insert(port_injects, {cx - 2, cy - 1, "light-oil", "chem light in"})
                table.insert(port_injects, {cx - 2, cy + 1, "water", "chem water in"})
                table.insert(port_injects, {cx + 2, cy - 1, "petroleum-gas", "chem petgas out"})
            elseif r.name == "sulfur" then
                table.insert(port_injects, {cx - 2, cy - 1, "petroleum-gas", "chem petgas in"})
                table.insert(port_injects, {cx - 2, cy + 1, "water", "chem water in"})
            elseif r.name == "sulfuric-acid" then
                table.insert(port_injects, {cx - 2, cy - 1, "water", "chem water in"})
                table.insert(port_injects, {cx + 2, cy - 1, "sulfuric-acid", "chem acid out"})
            elseif r.name == "plastic-bar" then
                table.insert(port_injects, {cx - 2, cy - 1, "petroleum-gas", "chem petgas in"})
            end
        end
    end

    local failed_injects = 0
    local fluid_samples = {}
    for _, inj in ipairs(port_injects) do
        local ins, p = inject_pipe(inj[1], inj[2], inj[3])
        if ins == 0 and p then
            failed_injects = failed_injects + 1
            local existing = serpent.line(p.get_fluid_contents())
            if #fluid_samples < 15 then
                table.insert(fluid_samples, string.format("Fluid conflict: wanted %s at (%.1f, %.1f) [%s], but pipe has %s",
                    inj[3], inj[1], inj[2], inj[4], existing))
            end
        end
    end
    log("RESULT_FLUID_INJECT_CONFLICTS: " .. tostring(failed_injects))

    -- Check for adjacent surface pipes with differing fluids
    local surface_clashes = 0
    for _, p in ipairs(pipes) do
        if p.valid and p.name == "pipe" then
            local contents = p.get_fluid_contents()
            local f1 = nil
            for fname, _ in pairs(contents) do f1 = fname break end
            if f1 then
                for _, off in ipairs({{1,0}, {-1,0}, {0,1}, {0,-1}}) do
                    local nkey = string.format("%.1f,%.1f", p.position.x + off[1], p.position.y + off[2])
                    local n = pipe_map[nkey]
                    if n and n.valid and n.name == "pipe" then
                        for f2, _ in pairs(n.get_fluid_contents()) do
                            if f2 ~= f1 then
                                surface_clashes = surface_clashes + 1
                                if #fluid_samples < 15 then
                                    table.insert(fluid_samples, string.format("Adjacent pipe clash: (%.1f, %.1f) [%s] <-> (%.1f, %.1f) [%s]",
                                        p.position.x, p.position.y, f1, n.position.x, n.position.y, f2))
                                end
                            end
                        end
                    end
                end
            end
        end
    end
    log("RESULT_SURFACE_PIPE_CLASHES: " .. tostring(surface_clashes))
    for _, s in ipairs(fluid_samples) do
        log("FLUID_CLASH: " .. s)
    end
end)
"""
    )

    save_path = work_dir / "check.zip"
    cmd = [
        str(game_bin),
        "-c",
        str(config_file),
        "--mod-directory",
        str(mods_dir),
        "--create",
        str(save_path),
    ]

    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return _parse_validation_log(data_dir / "factorio-current.log")


def _parse_int_or_str(val_str: str) -> int | str:
    """Parse an integer string if numeric, otherwise return the original string."""
    if val_str.isdigit() or (val_str.startswith("-") and val_str[1:].isdigit()):
        return int(val_str)
    return val_str


def _record_key_val(
    results: dict[str, dict[str, object]],
    prefix: str,
    payload: str,
) -> None:
    """Parse a colon-delimited log payload into the structured results dictionary."""
    if ":" not in payload:
        return
    key, val = payload.split(":", 1)
    key = key.strip()
    val_str = val.strip()
    if prefix == "BOUNDS_":
        results["bounds"][key.replace("BOUNDS_", "")] = val_str
        return
    parsed = _parse_int_or_str(val_str)
    bucket_map = {
        "RESULT_": ("results", ""),
        "COUNT_": ("counts", "COUNT_"),
        "UNPLACED_": ("unplaced", "UNPLACED_"),
        "REASON_": ("reasons", "REASON_"),
    }
    if prefix in bucket_map:
        bucket, strip_prefix = bucket_map[prefix]
        results[bucket][key.replace(strip_prefix, "")] = parsed


def _process_log_line(
    results: dict[str, dict[str, object]],
    samples: list[str],
    fluid_clashes: list[str],
    line: str,
) -> None:
    """Match known validation log prefixes on a single log line."""
    for prefix in LOG_PREFIXES:
        if prefix not in line:
            continue
        payload = line[line.find(prefix) :].strip()
        if prefix == "SAMPLE_FAILURE:":
            samples.append(payload.replace("SAMPLE_FAILURE:", "").strip())
        elif prefix == "FLUID_CLASH:":
            fluid_clashes.append(payload.replace("FLUID_CLASH:", "").strip())
        else:
            _record_key_val(results, prefix, payload)


def _parse_validation_log(log_path: Path) -> dict[str, Any]:
    """Parse headless Factorio blueprint validation log into result dictionaries."""
    buckets: dict[str, dict[str, object]] = {
        "results": {},
        "counts": {},
        "unplaced": {},
        "reasons": {},
        "bounds": {},
    }
    samples: list[str] = []
    fluid_clashes: list[str] = []
    if log_path.is_file():
        for line in log_path.read_text().splitlines():
            _process_log_line(buckets, samples, fluid_clashes, line)

    details: dict[str, Any] = {
        **buckets,
        "samples": samples,
        "fluid_clashes": fluid_clashes,
    }
    flat: dict[str, Any] = {**buckets["results"]}
    for k, v in buckets["counts"].items():
        flat[f"COUNT_{k}"] = v
    flat["_details"] = details
    return flat


def _print_dict_section(title: str, mapping: Mapping[str, object]) -> None:
    """Print a titled section for a non-empty string-keyed mapping."""
    if not mapping:
        return
    print(f"\n{title}:")
    for name, count in sorted(mapping.items()):
        print(f"  {name:25s}: {count}")


def _print_list_section(title: str, items: list[str]) -> None:
    """Print a titled bulleted section for a non-empty list of strings."""
    if not items:
        return
    print(f"\n{title}:")
    for item in items:
        print(f"  - {item}")


def _print_validation_summary(results: Mapping[str, object]) -> None:
    """Print human-readable blueprint validation results to stdout."""
    details_obj = results.get("_details", {})
    details: dict[str, Any] = dict(details_obj) if isinstance(details_obj, Mapping) else {}
    print("Headless Factorio Validation Results:")
    print(f"  Import return code: {results.get('RESULT_IMPORT', -1)}")
    print(f"  Total blueprint:   {results.get('RESULT_TOTAL', 0)}")
    print(f"  Built entities:    {results.get('RESULT_BUILT', 0)}")
    print(f"  Total ghost count: {results.get('RESULT_GHOSTS', 0)}")
    bounds = details.get("bounds", {})
    if bounds:
        print("Entity Coordinate Bounds:")
        for k, v in bounds.items():
            print(f"  {k}: {v}")

    _print_dict_section("Placed entities breakdown", details.get("counts", {}))
    _print_dict_section("Unplaced entities breakdown", details.get("unplaced", {}))

    reasons = details.get("reasons", {})
    if reasons:
        print("\nFailure reasons:")
        for reason, count in sorted(reasons.items(), key=lambda item: item[1], reverse=True):
            print(f"  [{count:4d}] {reason}")

    _print_list_section("Sample failures", details.get("samples", []))
    print("\nFluid network validation:")
    print(f"  Injection conflicts    : {results.get('RESULT_FLUID_INJECT_CONFLICTS', 0)}")
    print(f"  Surface pipe clashes   : {results.get('RESULT_SURFACE_PIPE_CLASHES', 0)}")
    _print_list_section("Fluid clashes", details.get("fluid_clashes", []))


def main() -> int:
    """CLI entry point for blueprint validation."""
    parser = argparse.ArgumentParser(
        description="Validate Factorio blueprint with headless game engine."
    )
    parser.add_argument(
        "blueprint_file",
        type=Path,
        help="Path to blueprint file containing exchange string",
    )
    parser.add_argument(
        "--game",
        type=Path,
        default=None,
        help="Optional path to Factorio binary",
    )

    args = parser.parse_args()

    try:
        game_bin = find_factorio_binary(args.game)
    except FileNotFoundError as err:
        sys.stderr.write(f"Error: {err}\n")
        return 1

    bp_string = args.blueprint_file.read_text().strip()

    with tempfile.TemporaryDirectory(prefix="factorilog-check-") as tmp:
        work_dir = Path(tmp)
        results = validate_blueprint(bp_string, game_bin, work_dir)

    _print_validation_summary(results)
    fluid_conflicts = int(results.get("RESULT_FLUID_INJECT_CONFLICTS", 0))
    surface_clashes = int(results.get("RESULT_SURFACE_PIPE_CLASHES", 0))
    unplaced_total = int(results.get("RESULT_UNPLACED_TOTAL", 0))
    if unplaced_total > 0 or fluid_conflicts > 0 or surface_clashes > 0:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
