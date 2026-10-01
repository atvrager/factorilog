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
    for candidate in Path("/home").glob("*/.steam/steam/steamapps/common/Factorio/bin/x64/factorio"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Factorio binary not found.")


def validate_blueprint(
    blueprint_string: str, game_bin: Path, work_dir: Path
) -> dict[str, int]:
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
    results: dict[str, int] = {}
    if log_path.is_file():
        for line in log_path.read_text().splitlines():
            for prefix in ("RESULT_", "COUNT_"):
                if prefix in line:
                    idx = line.find(prefix)
                    payload = line[idx:].strip()
                    if ":" in payload:
                        key, val = payload.split(":", 1)
                        try:
                            results[key.strip()] = int(val.strip())
                        except ValueError:
                            pass

    return results


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

    print("Headless Factorio Validation Results:")
    print(f"  Import return code: {results.get('RESULT_IMPORT', -1)}")
    print(f"  Built entities:    {results.get('RESULT_BUILT', 0)}")
    print(f"  Total ghost count: {results.get('RESULT_GHOSTS', 0)}")
    print("Placed entities breakdown:")
    for key, count in sorted(results.items()):
        if key.startswith("COUNT_"):
            name = key.replace("COUNT_", "")
            print(f"  {name:25s}: {count}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
