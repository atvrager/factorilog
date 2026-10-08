"""Factorio blueprint to playable save converter.

Converts a Factorio blueprint exchange string into a standalone, playable save file (.zip).
Configures infinite resource inputs, power interfaces, and output sinks.
The generated save runs on vanilla Factorio 2.1 with zero mod dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

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
    if which_bin and Path(which_bin).is_file():
        return Path(which_bin)
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


def generate_control_lua(blueprint_string: str, pins_data: dict[str, Any]) -> str:
    """Generate control.lua for scenario initialization and factory execution."""
    pins_json = json.dumps(pins_data)
    bp_clean = blueprint_string.strip()

    return f"""-- Control script for factorilog compiled factory save.
local BP = [==[{bp_clean}]==]
local PINS_JSON = [==[{pins_json}]==]

local function setup_player(player)
    if not (player and player.valid) then return end
    local cx = storage.center_x or 0
    local cy = storage.center_y or 0
    player.teleport({{cx, cy}})
    player.force.chart(player.surface, {{
        {{cx - 120, cy - 120}},
        {{cx + 120, cy + 120}}
    }})
    player.cheat_mode = true
    player.insert{{name = "power-armor", count = 1}}
    player.insert{{name = "construction-robot", count = 50}}
    player.insert{{name = "submachine-gun", count = 1}}
    player.insert{{name = "firearm-magazine", count = 200}}
end

script.on_init(function()
    game.forces.player.research_all_technologies()
    if game.forces.player.recipes["steel-plate"] then
        game.forces.player.recipes["steel-plate"].enabled = false
    end

    local PINS = helpers.json_to_table(PINS_JSON) or {{inputs = {{}}, outputs = {{}}}}
    local surface = game.surfaces[1]

    -- Request chunks around factory
    for cx = -6, 6 do
        for cy = -6, 6 do
            surface.request_to_generate_chunks({{cx * 32, cy * 32}}, 1)
        end
    end
    surface.force_generate_chunk_requests()

    -- Pave floor with concrete
    local tiles = {{}}
    for x = -120, 120 do
        for y = -120, 120 do
            tiles[#tiles + 1] = {{name = "concrete", position = {{x, y}}}}
        end
    end
    surface.set_tiles(tiles)

    -- Clear natural debris
    for _, entity in pairs(surface.find_entities_filtered{{area = {{{{-120, -120}}, {{120, 120}}}}}}) do
        if entity.name ~= "character" then
            entity.destroy()
        end
    end

    -- Build factory blueprint
    local inv = game.create_inventory(1)
    local stack = inv[1]
    stack.set_stack{{name = "blueprint", count = 1}}
    local import_res = stack.import_stack(BP)
    log("SAVE_GEN_IMPORT: " .. tostring(import_res))

    local built = stack.build_blueprint{{
        surface = surface,
        force = game.forces.player,
        position = {{0, 0}},
        force_build = true
    }}
    log("SAVE_GEN_BUILT: " .. tostring(built and #built or 0))

    local ghosts = surface.find_entities_filtered{{name = "entity-ghost"}}
    local bp_entities = stack.get_blueprint_entities() or {{}}

    local min_gx, min_bx = 1e9, 1e9
    local min_gy, min_by = 1e9, 1e9
    for _, g in pairs(ghosts) do
        if g.position.x < min_gx then min_gx = g.position.x end
        if g.position.y < min_gy then min_gy = g.position.y end
    end
    for _, e in pairs(bp_entities) do
        if e.position.x < min_bx then min_bx = e.position.x end
        if e.position.y < min_by then min_by = e.position.y end
    end
    local shift_x = math.floor((min_gx - min_bx) + 0.5)
    local shift_y = math.floor((min_gy - min_by) + 0.5)
    storage.shift_x = shift_x
    storage.shift_y = shift_y
    storage.center_x = shift_x + 30
    storage.center_y = shift_y + 15

    for _, g in ipairs(ghosts) do
        g.silent_revive()
    end

    -- Power supply: place electric energy interface at each pole
    local poles = surface.find_entities_filtered{{type = "electric-pole"}}
    for _, pole in ipairs(poles) do
        local ee = surface.create_entity{{
            name = "electric-energy-interface",
            position = {{pole.position.x, pole.position.y}},
            force = game.forces.player
        }}
        if not ee then
            surface.create_entity{{
                name = "electric-energy-interface",
                position = {{pole.position.x + 1, pole.position.y}},
                force = game.forces.player
            }}
        end
    end

    -- Setup input pins with Loaders
    for _, pin in ipairs(PINS.inputs or {{}}) do
        local px = pin.x + shift_x
        local py = pin.y + shift_y
        local ents = surface.find_entities_filtered{{position = {{px, py}}}}
        local ent = ents[1]
        if ent and ent.valid then
            if pin.type == "pipe" then
                -- Place infinity pipe next to input pipe
                local p = surface.create_entity{{
                    name = "infinity-pipe",
                    position = {{px, py - 1}},
                    force = game.forces.player
                }}
                if p then
                    p.set_infinity_pipe_filter({{name = pin.name, percentage = 1.0, mode = "at-least"}})
                end
            else
                local dir = ent.direction
                local dx, dy = 0, 0
                if dir == defines.direction.south then dx, dy = 0, 1
                elseif dir == defines.direction.north then dx, dy = 0, -1
                elseif dir == defines.direction.east then dx, dy = 1, 0
                elseif dir == defines.direction.west then dx, dy = -1, 0
                end

                local loader_name = "loader"
                if ent.name:find("express") then loader_name = "express-loader"
                elseif ent.name:find("fast") then loader_name = "fast-loader"
                end

                -- Infinity chest supplying resources
                local chest = surface.create_entity{{
                    name = "infinity-chest",
                    position = {{px - 3.0*dx, py - 3.0*dy}},
                    force = game.forces.player
                }}
                if chest then
                    chest.set_infinity_container_filter(1, {{name = pin.name, count = 100, index = 1}})
                end

                -- Loader unloads both lanes from chest onto belt
                local loader = surface.create_entity{{
                    name = loader_name,
                    position = {{px - 1.5*dx, py - 1.5*dy}},
                    direction = dir,
                    force = game.forces.player,
                    type = "output"
                }}
                if loader then loader.loader_type = "output" end
            end
        end
    end

    -- Setup output pins with Loaders
    for _, pin in ipairs(PINS.outputs or {{}}) do
        local px = pin.x + shift_x
        local py = pin.y + shift_y
        local ents = surface.find_entities_filtered{{position = {{px, py}}}}
        local ent = ents[1]
        if ent and ent.valid then
            if pin.type == "pipe" then
                -- Place void infinity pipe
                local p = surface.create_entity{{
                    name = "infinity-pipe",
                    position = {{px + 1, py}},
                    force = game.forces.player
                }}
                if p then
                    p.set_infinity_pipe_filter({{name = pin.name, percentage = 0.0, mode = "at-most"}})
                end
            else
                local dir = ent.direction
                local dx, dy = 0, 0
                if dir == defines.direction.south then dx, dy = 0, 1
                elseif dir == defines.direction.north then dx, dy = 0, -1
                elseif dir == defines.direction.east then dx, dy = 1, 0
                elseif dir == defines.direction.west then dx, dy = -1, 0
                end

                local loader_name = "loader"
                if ent.name:find("express") then loader_name = "express-loader"
                elseif ent.name:find("fast") then loader_name = "fast-loader"
                end

                -- Infinity chest voiding items
                local chest = surface.create_entity{{
                    name = "infinity-chest",
                    position = {{px + 3.0*dx, py + 3.0*dy}},
                    force = game.forces.player
                }}
                if chest then
                    chest.remove_unfiltered_items = true
                end

                -- Loader loads both lanes from belt into chest
                local loader = surface.create_entity{{
                    name = loader_name,
                    position = {{px + 1.5*dx, py + 1.5*dy}},
                    direction = dir,
                    force = game.forces.player,
                    type = "input"
                }}
                if loader then loader.loader_type = "input" end
            end
        end
    end

    for _, p in pairs(game.players) do
        setup_player(p)
    end
end)

script.on_event(defines.events.on_player_created, function(event)
    setup_player(game.get_player(event.player_index))
end)
"""


def build_save(
    blueprint_file: Path,
    pins_file: Path | None,
    out_save_file: Path,
    game_bin: Path,
    netlist_file: Path | None = None,
    def_file: Path | None = None,
) -> None:
    """Create a standalone Factorio save file from blueprint and pin metadata."""
    blueprint_string = blueprint_file.read_text().strip()

    pins_data: dict[str, Any] = {"inputs": [], "outputs": []}
    if pins_file and pins_file.is_file():
        pins_data = json.loads(pins_file.read_text())
    elif netlist_file and def_file and netlist_file.is_file() and def_file.is_file():
        try:
            from foundry.def2bp import extract_pins, parse_def
        except ImportError:
            from def2bp import extract_pins, parse_def
        def_data = parse_def(def_file.read_text())
        netlist_data = json.loads(netlist_file.read_text())
        pins_data = extract_pins(def_data, netlist_data)

    save_stem = out_save_file.stem
    # Sanitize scenario name for Factorio path rules
    scenario_name = re.sub(r"[^a-zA-Z0-9_-]", "_", save_stem)

    with tempfile.TemporaryDirectory(prefix="factorilog-save-") as tmp:
        work_dir = Path(tmp)
        mods_dir = work_dir / "mods"
        mods_dir.mkdir(parents=True, exist_ok=True)

        # Force vanilla base mod only. Explicitly disable all DLC / expansion mods.
        mod_list = {
            "mods": [
                {"name": "base", "enabled": True},
                {"name": "elevated-rails", "enabled": False},
                {"name": "quality", "enabled": False},
                {"name": "recycler", "enabled": False},
                {"name": "space-age", "enabled": False},
            ]
        }
        (mods_dir / "mod-list.json").write_text(json.dumps(mod_list, indent=2))

        config_file = work_dir / "config.ini"
        config_file.write_text(
            f"[path]\nread-data=__PATH__executable__/../../data\nwrite-data={work_dir}\n"
        )

        scenario_dir = work_dir / "scenarios" / scenario_name
        scenario_dir.mkdir(parents=True, exist_ok=True)

        description_json = {
            "order": "a",
            "multiplayer-compatible": True,
            "is-main-game": True,
        }
        (scenario_dir / "description.json").write_text(json.dumps(description_json, indent=2))

        control_code = generate_control_lua(blueprint_string, pins_data)
        (scenario_dir / "control.lua").write_text(control_code)

        # Convert scenario to standalone save archive
        cmd_create = [
            str(game_bin),
            "-c",
            str(config_file),
            "--mod-directory",
            str(mods_dir),
            "--scenario2map",
            scenario_name,
        ]
        res_create = subprocess.run(cmd_create, capture_output=True, text=True)
        if res_create.returncode != 0:
            log_file = work_dir / "factorio-current.log"
            log_text = log_file.read_text() if log_file.is_file() else "No log"
            raise RuntimeError(
                f"Factorio scenario2map failed with code {res_create.returncode}:\n"
                f"STDOUT:\n{res_create.stdout}\nSTDERR:\n{res_create.stderr}\nLOG:\n{log_text}"
            )

        generated_save = work_dir / "saves" / f"{scenario_name}.zip"
        if not generated_save.is_file():
            raise FileNotFoundError(f"Generated save file not found at {generated_save}")

        # Quick validation check: benchmark 60 ticks to ensure save loads and executes cleanly
        cmd_verify = [
            str(game_bin),
            "-c",
            str(config_file),
            "--mod-directory",
            str(mods_dir),
            "--benchmark",
            str(generated_save),
            "--benchmark-ticks",
            "60",
        ]
        res_verify = subprocess.run(cmd_verify, capture_output=True, text=True)
        if res_verify.returncode != 0:
            log_file = work_dir / "factorio-current.log"
            log_text = log_file.read_text() if log_file.is_file() else "No log"
            raise RuntimeError(
                f"Factorio verification benchmark failed with code {res_verify.returncode}:\n"
                f"STDOUT:\n{res_verify.stdout}\nSTDERR:\n{res_verify.stderr}\nLOG:\n{log_text}"
            )

        out_save_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated_save, out_save_file)


def main() -> int:
    """CLI entry point for converting blueprints to save files."""
    parser = argparse.ArgumentParser(
        description="Convert Factorio blueprint exchange string to a loadable save file."
    )
    parser.add_argument("blueprint", type=Path, help="Path to blueprint string file")
    parser.add_argument(
        "--out",
        dest="out_file",
        type=Path,
        required=True,
        help="Path to write output save file (.zip)",
    )
    parser.add_argument(
        "--pins",
        dest="pins_file",
        type=Path,
        default=None,
        help="Path to pins JSON file",
    )
    parser.add_argument(
        "--netlist",
        dest="netlist_file",
        type=Path,
        default=None,
        help="Path to netlist JSON file",
    )
    parser.add_argument(
        "--def",
        dest="def_file",
        type=Path,
        default=None,
        help="Path to DEF file",
    )
    parser.add_argument(
        "--game",
        type=Path,
        default=None,
        help="Path to Factorio executable binary",
    )
    parser.add_argument(
        "--time-out",
        dest="time_file",
        type=Path,
        default=None,
        help="Optional path to write timing JSON output",
    )

    args = parser.parse_args()
    t0 = time.perf_counter()

    try:
        game_bin = find_factorio_binary(args.game)
    except FileNotFoundError as err:
        sys.stderr.write(f"Error: {err}\n")
        return 1

    build_save(
        blueprint_file=args.blueprint,
        pins_file=args.pins_file,
        out_save_file=args.out_file,
        game_bin=game_bin,
        netlist_file=args.netlist_file,
        def_file=args.def_file,
    )

    t1 = time.perf_counter()
    elapsed = round(t1 - t0, 3)

    if args.time_file:
        args.time_file.parent.mkdir(parents=True, exist_ok=True)
        timing_data = {
            "stage": "save",
            "elapsed_seconds": elapsed,
            "output_size_bytes": args.out_file.stat().st_size if args.out_file.is_file() else 0,
        }
        args.time_file.write_text(json.dumps(timing_data, indent=2) + "\n")

    print(f"Generated save file: {args.out_file} ({args.out_file.stat().st_size} bytes in {elapsed}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
