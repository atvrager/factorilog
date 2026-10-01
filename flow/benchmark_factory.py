"""Benchmark Factorio factory blueprint performance with headless Factorio.

Loads the factory blueprint, revives entities, feeds raw inputs,
transfers items across machine boundaries, and measures production rates.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    for candidate in Path("/home").glob(
        "*/.steam/steam/steamapps/common/Factorio/bin/x64/factorio"
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Factorio binary not found.")


def run_benchmark(
    blueprint_string: str,
    netlist_data: dict[str, Any],
    game_bin: Path,
    work_dir: Path,
    ticks: int = 7200,
) -> dict[str, Any]:
    """Run dynamic simulation benchmark in headless Factorio."""
    mods_dir = work_dir / "mods"
    mod_dir = mods_dir / "factory_bench"
    data_dir = work_dir / "data"

    mod_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    config_file = work_dir / "config.ini"
    config_file.write_text(
        f"[path]\nread-data=__PATH__executable__/../../data\nwrite-data={data_dir}\n"
    )

    # Save netlist inputs and outputs metadata for Lua script
    inputs_json = mod_dir / "netlist.json"
    inputs_json.write_text(json.dumps(netlist_data))

    info_file = mod_dir / "info.json"
    info_file.write_text(
        """{
  "name": "factory_bench",
  "version": "0.1.0",
  "title": "Factory Benchmark Mod",
  "author": "factorilog",
  "factorio_version": "2.1",
  "dependencies": ["base >= 2.1"]
}
"""
    )

    bp_lua = mod_dir / "blueprint.lua"
    bp_lua.write_text(f'return "{blueprint_string.strip()}"\n')

    control_lua = mod_dir / "control.lua"
    control_lua.write_text(
        """
local BLUEPRINT = require("blueprint")

local TICKS_TOTAL = """
        + str(ticks)
        + """

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
    stack.import_stack(BLUEPRINT)

    local built = stack.build_blueprint{
        surface = surface,
        force = game.forces.player,
        position = {0, 0},
        force_build = true
    }

    local ghosts = surface.find_entities_filtered{name = "entity-ghost"}
    local min_gx, min_gy = 1e9, 1e9
    for _, g in pairs(ghosts) do
        if g.position.x < min_gx then min_gx = g.position.x end
        if g.position.y < min_gy then min_gy = g.position.y end
    end
    local bp_entities = stack.get_blueprint_entities() or {}
    local min_bx, min_by = 1e9, 1e9
    for _, e in pairs(bp_entities) do
        if e.position.x < min_bx then min_bx = e.position.x end
        if e.position.y < min_by then min_by = e.position.y end
    end
    local shift_x = math.floor((min_gx - min_bx) + 0.5)
    local shift_y = math.floor((min_gy - min_by) + 0.5)

    -- Revive all ghosts
    local revived = 0
    for _, ghost in pairs(ghosts) do
        local colliders, ent = ghost.revive()
        if ent and ent.valid then
            revived = revived + 1
        end
    end
    storage.revived_count = revived
    log("BENCH_REVIVED: " .. tostring(revived))

    -- Map inputs and outputs from netlist pin positions
    local input_pins = {
        {name = "coal", x = 80.5 + shift_x, y = 0.5 + shift_y, type = "belt"},
        {name = "copper-ore", x = 92.5 + shift_x, y = 0.5 + shift_y, type = "belt"},
        {name = "iron-ore", x = 76.5 + shift_x, y = 0.5 + shift_y, type = "belt"},
        {name = "stone", x = 84.5 + shift_x, y = 0.5 + shift_y, type = "belt"},
        {name = "crude-oil", x = 90.5 + shift_x, y = 0.5 + shift_y, type = "pipe"},
        {name = "water", x = 88.5 + shift_x, y = 0.5 + shift_y, type = "pipe"}
    }
    local output_pins = {
        {name = "automation-science-pack", x = 94.5 + shift_x, y = 0.5 + shift_y},
        {name = "chemical-science-pack", x = 82.5 + shift_x, y = 0.5 + shift_y},
        {name = "logistic-science-pack", x = 74.5 + shift_x, y = 0.5 + shift_y},
        {name = "military-science-pack", x = 86.5 + shift_x, y = 0.5 + shift_y},
        {name = "production-science-pack", x = 78.5 + shift_x, y = 0.5 + shift_y},
        {name = "utility-science-pack", x = 72.5 + shift_x, y = 0.5 + shift_y}
    }

    local input_entities = {}
    for _, pin in ipairs(input_pins) do
        local ents = surface.find_entities_filtered{position = {pin.x, pin.y}}
        for _, ent in pairs(ents) do
            if (pin.type == "belt" and (ent.type == "transport-belt" or ent.type == "underground-belt")) or
               (pin.type == "pipe" and (ent.type == "pipe" or ent.type == "pipe-to-ground")) then
                input_entities[#input_entities + 1] = {name = pin.name, entity = ent, type = pin.type}
                break
            end
        end
    end

    log("BENCH_INPUT_COUNT: " .. tostring(#input_entities))
    for _, ie in ipairs(input_entities) do
        log("BENCH_INPUT_PIN: " .. ie.name .. " pos=" .. ie.entity.position.x .. "," .. ie.entity.position.y)
    end

    local output_entities = {}
    for _, pin in ipairs(output_pins) do
        local ents = surface.find_entities_filtered{position = {pin.x, pin.y}, type = {"transport-belt", "underground-belt"}}
        for _, ent in pairs(ents) do
            output_entities[#output_entities + 1] = {name = pin.name, entity = ent}
            break
        end
    end
    log("BENCH_OUTPUT_COUNT: " .. tostring(#output_entities))

    -- Map all active machines and transfer connections
    local machines = surface.find_entities_filtered{type = {"assembling-machine", "furnace"}}
    local machine_map = {}
    local machines_with_belts = 0
    for _, m in pairs(machines) do
        local box = m.bounding_box
        -- Scan perimeter belts
        local nearby_belts = surface.find_entities_filtered{
            type = "transport-belt",
            area = {{box.left_top.x - 1.2, box.left_top.y - 1.2}, {box.right_bottom.x + 1.2, box.right_bottom.y + 1.2}}
        }
        if #nearby_belts > 0 then
            machines_with_belts = machines_with_belts + 1
        end
        machine_map[#machine_map + 1] = {
            entity = m,
            belts = nearby_belts
        }
    end
    log("BENCH_MACHINES_WITH_BELTS: " .. tostring(machines_with_belts) .. "/" .. tostring(#machines))
    storage.machine_map = machine_map
    storage.input_entities = input_entities
    storage.output_entities = output_entities
    storage.stats = {
        produced = {},
        consumed = {},
        samples = {}
    }
end)

script.on_event(defines.events.on_tick, function(event)
    local surface = game.surfaces[1]
    local tick = event.tick

    if tick == 0 then
        log("BENCH_REVIVED: " .. tostring(storage.revived_count or 0))
        log("BENCH_INPUT_COUNT: " .. tostring(#(storage.input_entities or {})))
        for _, ie in ipairs(storage.input_entities or {}) do
            log("BENCH_INPUT_PIN: " .. ie.name .. " pos=" .. ie.entity.position.x .. "," .. ie.entity.position.y)
        end
        log("BENCH_OUTPUT_COUNT: " .. tostring(#(storage.output_entities or {})))
        for _, oe in ipairs(storage.output_entities or {}) do
            log("BENCH_OUTPUT_PIN: " .. oe.name .. " pos=" .. oe.entity.position.x .. "," .. oe.entity.position.y)
        end
        local machines_with_belts = 0
        for _, entry in pairs(storage.machine_map or {}) do
            if #(entry.belts) > 0 then
                machines_with_belts = machines_with_belts + 1
            end
        end
        log("BENCH_MACHINES_WITH_BELTS: " .. tostring(machines_with_belts) .. "/" .. tostring(#(storage.machine_map or {})))
    end

    -- 1. Power all machines and transfer items
    for _, entry in pairs(storage.machine_map or {}) do
        local m = entry.entity
        if m.valid then
            m.energy = 1e9

            -- Transfer items between adjacent belts and machine
            for _, belt in pairs(entry.belts) do
                if belt.valid then
                    -- Pull input items from belt
                    for line_idx = 1, 2 do
                        local line = belt.get_transport_line(line_idx)
                        for _, it in pairs(line.get_contents()) do
                            local inserted = m.insert({name = it.name, count = 1})
                            if inserted > 0 then
                                line.remove_item({name = it.name, count = 1})
                                storage.stats.consumed[it.name] = (storage.stats.consumed[it.name] or 0) + 1
                                break
                            end
                        end
                    end

                    -- Push output items from machine onto belt
                    local out_inv = m.get_output_inventory()
                    if out_inv and not out_inv.is_empty() then
                        for _, item_entry in pairs(out_inv.get_contents()) do
                            local line = belt.get_transport_line(1)
                            if line.can_insert_at_back() then
                                line.insert_at_back({name = item_entry.name, count = 1})
                                out_inv.remove({name = item_entry.name, count = 1})
                            end
                        end
                    end
                end
            end
        end
    end

    -- 2. Feed raw inputs at input pins
    for _, in_ent in pairs(storage.input_entities or {}) do
        local ent = in_ent.entity
        if ent.valid then
            if in_ent.type == "pipe" then
                ent.insert_fluid({name = in_ent.name, amount = 100})
            elseif in_ent.type == "belt" then
                for line_idx = 1, 2 do
                    local line = ent.get_transport_line(line_idx)
                    local can = line.can_insert_at_back()
                    if tick < 5 then
                        log(string.format("BENCH_FEED_DEBUG: tick=%d pin=%s line=%d can=%s count=%d", tick, in_ent.name, line_idx, tostring(can), #line))
                    end
                    if can then
                        line.insert_at_back({name = in_ent.name, count = 1})
                    end
                end
            end
        end
    end

    -- 3. Drain science packs at output pins
    for _, out_ent in pairs(storage.output_entities or {}) do
        local belt = out_ent.entity
        if belt.valid then
            for line_idx = 1, 2 do
                local line = belt.get_transport_line(line_idx)
                for _, item_entry in pairs(line.get_contents()) do
                    log(string.format("BENCH_DRAINED_%s_AT_%s: count=%d", item_entry.name, out_ent.name, item_entry.count))
                    storage.stats.produced[item_entry.name] = (storage.stats.produced[item_entry.name] or 0) + item_entry.count
                    line.remove_item({name = item_entry.name, count = item_entry.count})
                end
            end
        end
    end

    -- 4. Periodic reporting
    if tick % 600 == 0 then
        local active_count = 0
        local total_count = 0
        for _, entry in pairs(storage.machine_map or {}) do
            total_count = total_count + 1
            if entry.entity.valid and entry.entity.is_crafting() then
                active_count = active_count + 1
            end
        end
        log(string.format("BENCH_STATUS: tick=%d active_machines=%d/%d", tick, active_count, total_count))
        local total_consumed = 0
        for k, v in pairs(storage.stats.consumed) do
            total_consumed = total_consumed + v
            log(string.format("BENCH_CONSUMED_%s: %d", k, v))
        end
        log(string.format("BENCH_TOTAL_CONSUMED: %d", total_consumed))
        for k, v in pairs(storage.stats.produced) do
            local rate_spm = (v / (tick / 3600.0))
            log(string.format("BENCH_RATE_%s: total=%d rate_spm=%.2f", k, v, rate_spm))
        end
    end

    -- 5. Completion
    if tick >= TICKS_TOTAL then
        log("BENCH_COMPLETE: true")
        for k, v in pairs(storage.stats.produced) do
            local rate_spm = (v / (TICKS_TOTAL / 3600.0))
            log(string.format("FINAL_RATE_%s: %d (%.2f/min)", k, v, rate_spm))
        end
    end
end)
"""
    )

    save_path = work_dir / "bench.zip"
    cmd_create = [
        str(game_bin),
        "-c",
        str(config_file),
        "--mod-directory",
        str(mods_dir),
        "--create",
        str(save_path),
    ]
    res_create = subprocess.run(cmd_create, capture_output=True, text=True)
    if res_create.returncode != 0:
        log_file = data_dir / "factorio-current.log"
        log_text = log_file.read_text() if log_file.is_file() else "No log"
        raise RuntimeError(f"Factorio create failed:\nSTDOUT:\n{res_create.stdout}\nSTDERR:\n{res_create.stderr}\nLOG:\n{log_text}")

    cmd_bench = [
        str(game_bin),
        "-c",
        str(config_file),
        "--mod-directory",
        str(mods_dir),
        "--benchmark",
        str(save_path),
        "--benchmark-ticks",
        str(ticks),
        "--benchmark-sanitize",
    ]
    bench_res = subprocess.run(cmd_bench, capture_output=True, text=True)
    if bench_res.returncode != 0:
        log_file = data_dir / "factorio-current.log"
        log_text = log_file.read_text() if log_file.is_file() else "No log"
        raise RuntimeError(f"Factorio benchmark failed:\nSTDOUT:\n{bench_res.stdout}\nSTDERR:\n{bench_res.stderr}\nLOG:\n{log_text}")

    log_path = data_dir / "factorio-current.log"
    results: dict[str, Any] = {
        "ticks": ticks,
        "revived": 0,
        "status_reports": [],
        "final_rates": {},
        "ups": 0.0,
    }
    if log_path.is_file():
        for line in log_path.read_text().splitlines():
            if "BENCH_" in line:
                results.setdefault("bench_logs", []).append(line)
            if "BENCH_REVIVED:" in line:
                results["revived"] = int(line.split(":")[-1].strip())
            elif "BENCH_STATUS:" in line:
                results["status_reports"].append(line.split("BENCH_STATUS:")[-1].strip())
            elif "FINAL_RATE_" in line:
                parts = line.split("FINAL_RATE_")[-1].split(":")
                pack = parts[0].strip()
                val = parts[1].strip()
                results["final_rates"][pack] = val

    # Extract benchmark execution time and UPS
    if bench_res.stdout:
        for line in bench_res.stdout.splitlines():
            if "Performed" in line and "in" in line:
                results["summary"] = line.strip()

    return results


def main() -> int:
    """CLI entrypoint for running factory benchmark."""
    parser = argparse.ArgumentParser(description="Benchmark Factorio factory throughput.")
    parser.add_argument("blueprint_file", type=Path, help="Path to blueprint file")
    parser.add_argument("netlist_file", type=Path, help="Path to netlist JSON file")
    parser.add_argument("--ticks", type=int, default=3600, help="Simulation ticks to run")
    parser.add_argument("--game", type=Path, default=None, help="Path to Factorio binary")

    args = parser.parse_args()
    game_bin = find_factorio_binary(args.game)
    bp_string = args.blueprint_file.read_text().strip()
    netlist_data = json.loads(args.netlist_file.read_text())

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="factorilog-bench-") as tmp:
        work_dir = Path(tmp)
        results = run_benchmark(bp_string, netlist_data, game_bin, work_dir, ticks=args.ticks)

    print("\nBenchmark Execution Results:")
    print(f"  Simulation ticks: {results['ticks']} ({results['ticks'] / 60.0:.1f} game seconds)")
    print(f"  Revived entities: {results['revived']}")
    if "summary" in results:
        print(f"  Speed: {results['summary']}")

    if "bench_logs" in results:
        print("\nDiagnostic Logs:")
        for log_line in results["bench_logs"]:
            print(f"  {log_line}")

    print("\nStatus Progression:")
    for status in results["status_reports"][:10]:
        print(f"  {status}")

    print("\nFinal Science Pack Production Rates:")
    if results["final_rates"]:
        for pack, rate in sorted(results["final_rates"].items()):
            print(f"  {pack:25s}: {rate}")
    else:
        print("  No science pack output recorded during this interval.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
