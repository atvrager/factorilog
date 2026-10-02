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
from pathlib import Path

DEFAULT_GAME_PATH = Path(
    os.path.expanduser("~/.steam/steam/steamapps/common/Factorio/bin/x64/factorio")
)


def find_factorio_binary(override: Path | None = None) -> Path:
    """Find the Factorio executable binary."""
    if override and override.is_file():
        return override
    env_game = os.environ.get("FACTORIO_BIN")
    if env_game and Path(env_game).is_file():
        return Path(env_game)
    which_bin = shutil.which("factorio")
    if DEFAULT_GAME_PATH.is_file():
        return DEFAULT_GAME_PATH
    for candidate in [
        Path("/scratch/opt/factorio/bin/x64/factorio"),
        Path("/opt/factorio/bin/x64/factorio"),
    ]:
        if candidate.is_file():
            return candidate
    for candidate in Path("/home").glob(
        "*/.steam/steam/steamapps/common/Factorio/bin/x64/factorio"
    ):
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

    log_path = data_dir / "factorio-current.log"
    results: dict[str, Any] = {
        "results": {},
        "counts": {},
        "unplaced": {},
        "reasons": {},
        "bounds": {},
        "samples": [],
    }
    if log_path.is_file():
        for line in log_path.read_text().splitlines():
            for prefix in (
                "RESULT_",
                "COUNT_",
                "UNPLACED_",
                "REASON_",
                "BOUNDS_",
                "SAMPLE_FAILURE:",
            ):
                if prefix in line:
                    idx = line.find(prefix)
                    payload = line[idx:].strip()
                    if prefix == "SAMPLE_FAILURE:":
                        sample_text = payload.replace("SAMPLE_FAILURE:", "").strip()
                        results["samples"].append(sample_text)
                    elif ":" in payload:
                        key, val = payload.split(":", 1)
                        key = key.strip()
                        val_str = val.strip()
                        if prefix == "BOUNDS_":
                            results["bounds"][key.replace("BOUNDS_", "")] = val_str
                        else:
                            val_int = (
                                int(val_str)
                                if val_str.isdigit()
                                or (val_str.startswith("-") and val_str[1:].isdigit())
                                else val_str
                            )
                            if prefix == "RESULT_":
                                results["results"][key] = val_int
                            elif prefix == "COUNT_":
                                results["counts"][key.replace("COUNT_", "")] = val_int
                            elif prefix == "UNPLACED_":
                                results["unplaced"][key.replace("UNPLACED_", "")] = val_int
                            elif prefix == "REASON_":
                                results["reasons"][key.replace("REASON_", "")] = val_int

    # Flat compatibility keys for existing callers
    flat: dict[str, Any] = {**results["results"]}
    for k, v in results["counts"].items():
        flat[f"COUNT_{k}"] = v
    flat["_details"] = results
    return flat


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

    details = results.get("_details", {})
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

    print("\nPlaced entities breakdown:")
    for name, count in sorted(details.get("counts", {}).items()):
        print(f"  {name:25s}: {count}")

    unplaced = details.get("unplaced", {})
    if unplaced:
        print("\nUnplaced entities breakdown:")
        for name, count in sorted(unplaced.items()):
            print(f"  {name:25s}: {count}")

    reasons = details.get("reasons", {})
    if reasons:
        print("\nFailure reasons:")
        for reason, count in sorted(reasons.items(), key=lambda item: item[1], reverse=True):
            print(f"  [{count:4d}] {reason}")

    samples = details.get("samples", [])
    if samples:
        print("\nSample failures:")
        for s in samples:
            print(f"  - {s}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
