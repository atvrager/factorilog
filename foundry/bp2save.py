"""Factorio blueprint to playable save converter.

Converts a Factorio blueprint exchange string into a standalone, playable save file (.zip).
Configures infinite resource inputs, power interfaces, and output sinks.
The generated save runs on vanilla Factorio 2.1 with zero mod dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_GAME_PATH = Path("~/.steam/steam/steamapps/common/Factorio/bin/x64/factorio").expanduser()


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

    try:
        from foundry.blueprint import decode
    except ImportError:
        from blueprint import decode

    min_x, max_x = -150.0, 150.0
    min_y, max_y = -150.0, 150.0

    try:
        doc = decode(bp_clean)
        bp_ents = doc.get("blueprint", {}).get("entities", [])
        if bp_ents:
            xs = [e["position"]["x"] for e in bp_ents if "position" in e and "x" in e["position"]]
            ys = [e["position"]["y"] for e in bp_ents if "position" in e and "y" in e["position"]]
            for pin in pins_data.get("inputs", []) + pins_data.get("outputs", []):
                if "x" in pin and "y" in pin:
                    xs.append(pin["x"])
                    ys.append(pin["y"])
            if xs and ys:
                min_x = min(xs)
                max_x = max(xs)
                min_y = min(ys)
                max_y = max(ys)
    except Exception as exc:
        sys.stderr.write(f"Warning: could not decode blueprint bounds: {exc}\n")

    span_x = max_x - min_x
    span_y = max_y - min_y
    half_w = max(span_x / 2.0, abs(min_x), abs(max_x)) + 64.0
    half_h = max(span_y / 2.0, abs(min_y), abs(max_y)) + 64.0

    chunk_min_x = math.floor(-half_w / 32.0) - 1
    chunk_max_x = math.ceil(half_w / 32.0) + 1
    chunk_min_y = math.floor(-half_h / 32.0) - 1
    chunk_max_y = math.ceil(half_h / 32.0) + 1

    chunk_min_x = min(chunk_min_x, -10)
    chunk_max_x = max(chunk_max_x, 10)
    chunk_min_y = min(chunk_min_y, -10)
    chunk_max_y = max(chunk_max_y, 10)

    clear_min_x = chunk_min_x * 32
    clear_max_x = (chunk_max_x + 1) * 32
    clear_min_y = chunk_min_y * 32
    clear_max_y = (chunk_max_y + 1) * 32

    return f"""-- Control script for factorilog compiled factory save.
local BP = [==[{bp_clean}]==]
local PINS_JSON = [==[{pins_json}]==]

local function setup_player(player)
    if not (player and player.valid) then return end
    local cx = storage.center_x or 0
    local cy = storage.center_y or 0
    player.teleport({{cx, cy}})
    player.force.chart(player.surface, {{
        {{{clear_min_x}, {clear_min_y}}},
        {{{clear_max_x}, {clear_max_y}}}
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
    for cx = {chunk_min_x}, {chunk_max_x} do
        for cy = {chunk_min_y}, {chunk_max_y} do
            surface.request_to_generate_chunks({{cx * 32, cy * 32}}, 1)
        end
    end
    surface.force_generate_chunk_requests()

    -- Fill water with landfill
    local water_tiles = surface.find_tiles_filtered{{
        area = {{{{{clear_min_x}, {clear_min_y}}}, {{{clear_max_x}, {clear_max_y}}}}},
        name = {{"water", "deepwater", "water-green", "deepwater-green", "water-shallow", "water-mud"}}
    }}
    if #water_tiles > 0 then
        local landfill_tiles = {{}}
        for _, t in ipairs(water_tiles) do
            landfill_tiles[#landfill_tiles + 1] = {{name = "landfill", position = t.position}}
        end
        surface.set_tiles(landfill_tiles)
    end

    -- Pave floor with concrete in chunk column batches
    for cx = {chunk_min_x}, {chunk_max_x} do
        local concrete_tiles = {{}}
        local bx = cx * 32
        for cy = {chunk_min_y}, {chunk_max_y} do
            local by = cy * 32
            for tx = 0, 31 do
                for ty = 0, 31 do
                    concrete_tiles[#concrete_tiles + 1] = {{name = "concrete", position = {{bx + tx, by + ty}}}}
                end
            end
        end
        surface.set_tiles(concrete_tiles)
    end

    -- Clear natural debris
    for _, entity in pairs(surface.find_entities_filtered{{area = {{{{{clear_min_x}, {clear_min_y}}}, {{{clear_max_x}, {clear_max_y}}}}}}}) do
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
    log("SAVE_GEN_GHOSTS: " .. tostring(#ghosts) .. ", BP_ENTITIES: " .. tostring(#bp_entities))

    if #ghosts == 0 then
        error(string.format("build_blueprint failed: 0 ghosts placed (import_res=%%s, bp_entities=%%d)", tostring(import_res), #bp_entities))
    end

    local min_gx, max_gx, min_bx = 1e9, -1e9, 1e9
    local min_gy, max_gy, min_by = 1e9, -1e9, 1e9
    for _, g in pairs(ghosts) do
        if g.position.x < min_gx then min_gx = g.position.x end
        if g.position.x > max_gx then max_gx = g.position.x end
        if g.position.y < min_gy then min_gy = g.position.y end
        if g.position.y > max_gy then max_gy = g.position.y end
    end
    for _, e in pairs(bp_entities) do
        if e.position.x < min_bx then min_bx = e.position.x end
        if e.position.y < min_by then min_by = e.position.y end
    end
    local shift_x = math.floor((min_gx - min_bx) + 0.5)
    local shift_y = math.floor((min_gy - min_by) + 0.5)
    storage.shift_x = shift_x
    storage.shift_y = shift_y
    storage.center_x = math.floor((min_gx + max_gx) / 2)
    storage.center_y = math.floor((min_gy + max_gy) / 2)

    for _, g in ipairs(ghosts) do
        if g and g.valid then
            g.silent_revive()
        end
    end

    -- Off-chip power supply: single generator outside floorplan hooked to PDN
    local all_poles = surface.find_entities_filtered{{type = "electric-pole"}}
    if #all_poles > 0 then
        local min_x = 1e9
        for _, p in ipairs(all_poles) do
            if p.valid and p.position.x < min_x then min_x = p.position.x end
        end

        local target_pole = nil
        local switches = surface.find_entities_filtered{{name = "power-switch"}}
        if #switches > 0 then
            local ps = switches[1]
            local left_conn = ps.get_wire_connector(defines.wire_connector_id.power_switch_left_copper, true)
            if left_conn and #left_conn.connections > 0 then
                local owner = left_conn.connections[1].target.owner
                if owner and owner.valid then
                    target_pole = owner
                end
            end
            if not (target_pole and target_pole.valid) then
                local best_d = 1e9
                for _, p in ipairs(all_poles) do
                    if p.valid then
                        local d = (p.position.x - ps.position.x)^2 + (p.position.y - ps.position.y)^2
                        if d < best_d then
                            best_d = d
                            target_pole = p
                        end
                    end
                end
            end
        else
            local best_x = 1e9
            for _, p in ipairs(all_poles) do
                if p.valid and p.position.x < best_x then
                    best_x = p.position.x
                    target_pole = p
                end
            end
        end

        if target_pole and target_pole.valid then
            local gen_x = math.floor(min_x - 5.5) + 0.5
            local gen_y = target_pole.position.y

            local curr_x = gen_x
            local prev_pole = nil
            while (target_pole.position.x - curr_x) > 8.0 do
                local p = surface.create_entity{{
                    name = "medium-electric-pole",
                    position = {{curr_x, gen_y}},
                    force = game.forces.player
                }}
                if prev_pole and p then
                    local c1 = prev_pole.get_wire_connector(defines.wire_connector_id.pole_copper, true)
                    local c2 = p.get_wire_connector(defines.wire_connector_id.pole_copper, true)
                    c1.connect_to(c2, false)
                end
                prev_pole = p
                curr_x = curr_x + 7.0
            end

            local feed_pole = surface.create_entity{{
                name = "medium-electric-pole",
                position = {{curr_x, gen_y}},
                force = game.forces.player
            }}
            if prev_pole and feed_pole and prev_pole.valid and feed_pole.valid then
                local c1 = prev_pole.get_wire_connector(defines.wire_connector_id.pole_copper, true)
                local c2 = feed_pole.get_wire_connector(defines.wire_connector_id.pole_copper, true)
                c1.connect_to(c2, false)
            end
            if feed_pole and target_pole and feed_pole.valid and target_pole.valid then
                local c1 = feed_pole.get_wire_connector(defines.wire_connector_id.pole_copper, true)
                local c2 = target_pole.get_wire_connector(defines.wire_connector_id.pole_copper, true)
                c1.connect_to(c2, false)
            end

            surface.create_entity{{
                name = "electric-energy-interface",
                position = {{gen_x, gen_y}},
                force = game.forces.player
            }}
        end
    end

    local final_eeis = surface.find_entities_filtered{{name = "electric-energy-interface"}}
    log("SAVE_GEN_EEIS: " .. tostring(#final_eeis))

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

                -- Infinity chest collecting output items (retains up to 2000 items, voids excess)
                local chest = surface.create_entity{{
                    name = "infinity-chest",
                    position = {{px + 3.0*dx, py + 3.0*dy}},
                    force = game.forces.player
                }}
                if chest then
                    chest.set_infinity_container_filter(1, {{name = pin.name, count = 2000, mode = "at-most"}})
                    chest.remove_unfiltered_items = true
                    chest.insert{{name = pin.name, count = 50}}
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


def _load_pins(
    pins_file: Path | None,
    netlist_file: Path | None,
    def_file: Path | None,
) -> dict[str, Any]:
    """Load pin positions and orientations from pins JSON or parse from DEF."""
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
    return pins_data


def build_scenario_archive(
    blueprint_file: Path,
    out_save_file: Path,
    pins_file: Path | None = None,
    netlist_file: Path | None = None,
    def_file: Path | None = None,
) -> None:
    """Create a scenario archive containing Lua control script and metadata when Factorio binary is absent."""
    blueprint_string = blueprint_file.read_text().strip()
    pins_data = _load_pins(pins_file, netlist_file, def_file)

    save_stem = out_save_file.stem
    scenario_name = re.sub(r"[^a-zA-Z0-9_-]", "_", save_stem)
    control_code = generate_control_lua(blueprint_string, pins_data)
    description_json = {
        "order": "a",
        "multiplayer-compatible": True,
        "is-main-game": True,
    }
    readme_text = (
        "Factorio binary not found during build.\n"
        "This zip archive contains the full scenario definition.\n"
        "Install Factorio and run:\n"
        f"  factorio --scenario2map {scenario_name}\n"
        "to generate a playable save.\n"
    )

    out_save_file.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_save_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{scenario_name}/control.lua", control_code)
        zf.writestr(f"{scenario_name}/description.json", json.dumps(description_json, indent=2))
        zf.writestr(f"{scenario_name}/blueprint.txt", blueprint_string)
        zf.writestr(f"{scenario_name}/pins.json", json.dumps(pins_data, indent=2))
        zf.writestr(f"{scenario_name}/README.txt", readme_text)


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
    pins_data = _load_pins(pins_file, netlist_file, def_file)

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
        sys.stderr.write(
            f"Warning: {err} Building scenario archive without headless game binary.\n"
        )
        build_scenario_archive(
            blueprint_file=args.blueprint,
            out_save_file=args.out_file,
            pins_file=args.pins_file,
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
                "stub": True,
            }
            args.time_file.write_text(json.dumps(timing_data, indent=2) + "\n")

        print(
            f"Generated scenario archive: {args.out_file} ({args.out_file.stat().st_size} bytes in {elapsed}s)"
        )
        return 0

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

    print(
        f"Generated save file: {args.out_file} ({args.out_file.stat().st_size} bytes in {elapsed}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
