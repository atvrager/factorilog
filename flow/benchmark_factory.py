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

try:
    from foundry import def2bp
except ImportError:
    foundry_path = Path(__file__).resolve().parent.parent / "foundry"
    if str(foundry_path) not in sys.path:
        sys.path.insert(0, str(foundry_path))
    import def2bp

DEFAULT_GAME_PATH = Path("~/.steam/steam/steamapps/common/Factorio/bin/x64/factorio").expanduser()


def resolve_factory_pins(
    blueprint_file: Path | None,
    netlist_data: dict[str, Any],
    pins_file: Path | None = None,
    def_file: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve input and output pin locations dynamically from DEF or pins JSON."""
    # 1. Direct pins JSON file
    if pins_file and pins_file.is_file():
        data = json.loads(pins_file.read_text())
        return data.get("inputs", []), data.get("outputs", [])

    # 2. Direct DEF file
    if def_file and def_file.is_file():
        def_data = def2bp.parse_def(def_file.read_text())
        pins_data = def2bp.extract_pins(def_data, netlist_data)
        return pins_data.get("inputs", []), pins_data.get("outputs", [])

    # 3. Auto-discover alongside blueprint file
    if blueprint_file:
        candidate_pins = [
            blueprint_file.with_suffix(".pins.json"),
            blueprint_file.parent / "pins.json",
        ]
        for cp in candidate_pins:
            if cp.is_file():
                data = json.loads(cp.read_text())
                return data.get("inputs", []), data.get("outputs", [])

        candidate_defs = [
            blueprint_file.with_suffix(".def"),
            blueprint_file.parent / "factory.def",
            blueprint_file.parent / f"{blueprint_file.stem}.def",
        ]
        for cd in candidate_defs:
            if cd.is_file():
                def_data = def2bp.parse_def(cd.read_text())
                pins_data = def2bp.extract_pins(def_data, netlist_data)
                return pins_data.get("inputs", []), pins_data.get("outputs", [])

    # 4. Fallback: check foundry/factory.def in repository
    repo_def = Path(__file__).resolve().parent.parent / "foundry" / "factory.def"
    if repo_def.is_file():
        def_data = def2bp.parse_def(repo_def.read_text())
        pins_data = def2bp.extract_pins(def_data, netlist_data)
        return pins_data.get("inputs", []), pins_data.get("outputs", [])

    raise FileNotFoundError("Could not discover pin definitions. Provide --def or --pins flag.")


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


def extract_blueprint_taps(blueprint_string: str) -> list[dict[str, Any]]:
    """Extract telemetry tap coordinates from blueprint string."""
    import base64
    import zlib

    raw = zlib.decompress(base64.b64decode(blueprint_string.strip()[1:]))
    bp = json.loads(raw).get("blueprint", {})
    entities = bp.get("entities", [])
    ent_by_num = {e["entity_number"]: e for e in entities if "entity_number" in e}
    taps = []
    for e in entities:
        if e.get("name") == "display-panel":
            mat = e.get("text", "")
            disp_pos = e["position"]
            conns = e.get("connections", {}).get("1", {}).get("red", [])
            acc_pos = None
            if conns:
                target_id = conns[0].get("entity_id")
                target = ent_by_num.get(target_id)
                if target and target.get("name") == "arithmetic-combinator":
                    acc_pos = target["position"]
            taps.append(
                {
                    "material": mat,
                    "disp_pos": [disp_pos["x"], disp_pos["y"]],
                    "acc_pos": [acc_pos["x"], acc_pos["y"]] if acc_pos else None,
                }
            )
    return taps


def serialize_lua(obj: Any) -> str:
    """Serialize a Python data structure into valid Lua literal code."""
    if obj is None:
        return "nil"
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif isinstance(obj, (int, float)):
        return str(obj)
    elif isinstance(obj, str):
        return json.dumps(obj)
    elif isinstance(obj, (list, tuple)):
        return "{" + ", ".join(serialize_lua(x) for x in obj) + "}"
    elif isinstance(obj, dict):
        return (
            "{" + ", ".join(f"[{json.dumps(k)}] = {serialize_lua(v)}" for k, v in obj.items()) + "}"
        )
    raise TypeError(f"Cannot serialize {type(obj)} to Lua")


def run_benchmark(
    blueprint_string: str,
    netlist_data: dict[str, Any],
    game_bin: Path,
    work_dir: Path,
    input_pins: list[dict[str, Any]] | None = None,
    output_pins: list[dict[str, Any]] | None = None,
    ticks: int = 7200,
) -> dict[str, Any]:
    """Run dynamic simulation benchmark in headless Factorio."""
    if input_pins is None or output_pins is None:
        auto_in, auto_out = resolve_factory_pins(None, netlist_data)
        input_pins = input_pins or auto_in
        output_pins = output_pins or auto_out

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

    taps_list = extract_blueprint_taps(blueprint_string)
    taps_lua = mod_dir / "taps.lua"
    taps_lua.write_text(f"return {serialize_lua(taps_list)}\n")

    pins_lua = mod_dir / "pins.lua"
    pins_lua.write_text(
        f"return {{\n"
        f"  inputs = {serialize_lua(input_pins)},\n"
        f"  outputs = {serialize_lua(output_pins)},\n"
        f"}}\n"
    )

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
local TAPS = require("taps")
local PINS = require("pins")

local TICKS_TOTAL = """
        + str(ticks)
        + """


script.on_init(function()
    game.forces.player.research_all_technologies()
    if game.forces.player.recipes["steel-plate"] then
        game.forces.player.recipes["steel-plate"].enabled = false
    end
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
        if ghost.valid then
            local colliders, ent = ghost.revive()
            if ent and ent.valid then
                revived = revived + 1
            end
        end
    end
    storage.revived_count = revived
    log("BENCH_REVIVED: " .. tostring(revived))

    -- Map inputs and outputs dynamically from pins
    local input_pins = {}
    for _, pin in ipairs(PINS.inputs) do
        input_pins[#input_pins + 1] = {
            name = pin.name,
            x = pin.x + shift_x,
            y = pin.y + shift_y,
            type = pin.type
        }
    end
    local output_pins = {}
    for _, pin in ipairs(PINS.outputs) do
        output_pins[#output_pins + 1] = {
            name = pin.name,
            x = pin.x + shift_x,
            y = pin.y + shift_y
        }
    end

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
        -- Scan perimeter belts cardinally (exclude diagonal corners)
        local nearby_belts = surface.find_entities_filtered{
            type = {"transport-belt", "underground-belt"},
            area = {{box.left_top.x - 1.2, box.left_top.y - 1.2}, {box.right_bottom.x + 1.2, box.right_bottom.y + 1.2}}
        }
        local in_belts = {}
        local out_belts = {}
        for _, belt in pairs(nearby_belts) do
            local bx = belt.position.x
            local by = belt.position.y
            local on_west = (bx < box.left_top.x) and (by >= box.left_top.y - 0.2 and by <= box.right_bottom.y + 0.2)
            local on_north = (by < box.left_top.y) and (bx >= box.left_top.x - 0.2 and bx <= box.right_bottom.x + 0.2)
            local on_east = (bx > box.right_bottom.x) and (by >= box.left_top.y - 0.2 and by <= box.right_bottom.y + 0.2)
            local on_south = (by > box.right_bottom.y) and (bx >= box.left_top.x - 0.2 and bx <= box.right_bottom.x + 0.2)

            local is_ug = (belt.type == "underground-belt")
            local ug_type = is_ug and belt.belt_to_ground_type or ""

            if on_west or on_north then
                if not is_ug or ug_type == "output" then
                    in_belts[#in_belts + 1] = belt
                end
            end
            if on_east or on_south then
                if not is_ug or ug_type == "input" then
                    out_belts[#out_belts + 1] = belt
                end
            end
        end

        if #in_belts > 0 or #out_belts > 0 then
            machines_with_belts = machines_with_belts + 1
        end
        machine_map[#machine_map + 1] = {
            entity = m,
            in_belts = in_belts,
            out_belts = out_belts,
            belts = nearby_belts
        }
    end
    log("BENCH_MACHINES_WITH_BELTS: " .. tostring(machines_with_belts) .. "/" .. tostring(#machines))

    -- Map telemetry tap entities
    local telemetry_taps = {}
    for _, tap in ipairs(TAPS or {}) do
        local entry = {
            material = tap.material,
            acc = nil,
            disp = nil
        }
        if tap.acc_pos then
            local ents = surface.find_entities_filtered{
                name = "arithmetic-combinator",
                position = {tap.acc_pos[1] + shift_x, tap.acc_pos[2] + shift_y}
            }
            entry.acc = ents[1]
        end
        if tap.disp_pos then
            local ents = surface.find_entities_filtered{
                name = "display-panel",
                position = {tap.disp_pos[1] + shift_x, tap.disp_pos[2] + shift_y}
            }
            entry.disp = ents[1]
        end
        telemetry_taps[#telemetry_taps + 1] = entry
    end
    storage.telemetry_taps = telemetry_taps
    storage.telemetry_samples = {}

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
        for k, v in pairs(defines.entity_status) do
            if v == 70 or v == 28 or v == 18 or v == 1 then
                log("DEFINES_STATUS: " .. k .. "=" .. tostring(v))
            end
        end
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
        log("BENCH_TELEMETRY_TAPS: " .. tostring(#(storage.telemetry_taps or {})))
    end

    -- Power combinator accumulators
    for _, tap in pairs(storage.telemetry_taps or {}) do
        if tap.acc and tap.acc.valid then
            tap.acc.energy = 1e9
        end
    end

    -- 1. Power all machines and transfer items
    for _, entry in pairs(storage.machine_map or {}) do
        local m = entry.entity
        if m.valid then
            m.energy = 1e9

            -- Pull input items from input belts
            for _, belt in pairs(entry.in_belts or {}) do
                if belt.valid then
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
                end
            end

            -- Push output items from machine onto output belts
            local out_inv = m.get_output_inventory()
            if out_inv and not out_inv.is_empty() then
                for _, item_entry in pairs(out_inv.get_contents()) do
                    for _, belt in pairs(entry.out_belts or {}) do
                        if belt.valid then
                            local line = belt.get_transport_line(1)
                            if line.can_insert_at_back() then
                                line.insert_at_back({name = item_entry.name, count = 1})
                                out_inv.remove({name = item_entry.name, count = 1})
                                break
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

    -- 4. Periodic telemetry sampling
    if tick % 60 == 0 then
        local snap = {
            tick = tick,
            taps = {}
        }
        for _, tap in ipairs(storage.telemetry_taps or {}) do
            local count = 0
            if tap.acc and tap.acc.valid then
                local net = tap.acc.get_circuit_network(defines.wire_connector_id.combinator_output_green)
                if net and net.signals then
                    for _, s in pairs(net.signals) do
                        if s.count then
                            count = count + s.count
                        end
                    end
                end
            end
            snap.taps[tap.material] = count
        end
        storage.telemetry_samples[#storage.telemetry_samples + 1] = snap
    end

    -- 5. Periodic reporting
    if tick % 600 == 0 then
        local active_count = 0
        local total_count = 0
        for _, entry in pairs(storage.machine_map or {}) do
            total_count = total_count + 1
            if entry.entity.valid and entry.entity.is_crafting() then
                active_count = active_count + 1
                log("BENCH_ACTIVE_MACHINE: " .. entry.entity.name .. " pos=" .. entry.entity.position.x .. "," .. entry.entity.position.y)
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

    -- 6. Completion
    if tick >= TICKS_TOTAL - 1 then
        log("BENCH_COMPLETE: true")
        for _, entry in pairs(storage.machine_map or {}) do
            local m = entry.entity
            if m.valid then
                local inv = m.get_output_inventory()
                local in_items = {}
                for inv_id = 1, 5 do
                    pcall(function()
                        local inv_obj = m.get_inventory(inv_id)
                        if inv_obj and not inv_obj.is_empty() then
                            for _, it in pairs(inv_obj.get_contents()) do
                                in_items[#in_items + 1] = it
                            end
                        end
                    end)
                end
                local r = m.get_recipe()
                local status_name = "unknown"
                for k, v in pairs(defines.entity_status) do
                    if v == m.status then status_name = k break end
                end
                log(string.format("BENCH_MACHINE_INV: %s pos=%.1f,%.1f recipe=%s status=%s(%s) crafting=%s energy=%.1f in_items=%s out_items=%s",
                    m.name, m.position.x, m.position.y,
                    r and r.name or "none",
                    tostring(m.status),
                    status_name,
                    tostring(m.is_crafting()),
                    m.energy,
                    helpers.table_to_json(in_items),
                    helpers.table_to_json(inv and inv.get_contents() or {})))
            end
        end
        for _, out_ent in pairs(storage.output_entities or {}) do
            local belt = out_ent.entity
            if belt.valid then
                log(string.format("BENCH_OUT_PIN_STATUS: %s pos=%.1f,%.1f", out_ent.name, belt.position.x, belt.position.y))
            end
        end
        local belts = surface.find_entities_filtered{type = {"transport-belt", "underground-belt"}}
        for _, b in pairs(belts) do
            if b.valid then
                local l1 = b.get_transport_line(1).get_contents()
                local l2 = b.get_transport_line(2).get_contents()
                if next(l1) or next(l2) then
                    log(string.format("BENCH_BELT_ITEMS: pos=%.1f,%.1f name=%s l1=%s l2=%s",
                        b.position.x, b.position.y, b.name, helpers.table_to_json(l1), helpers.table_to_json(l2)))
                end
            end
        end
        for k, v in pairs(storage.stats.produced) do
            local rate_spm = (v / (TICKS_TOTAL / 3600.0))
            log(string.format("FINAL_RATE_%s: %d (%.2f/min)", k, v, rate_spm))
        end
        local trace_payload = {
            ticks_total = TICKS_TOTAL,
            sample_interval = 60,
            samples = storage.telemetry_samples
        }
        helpers.write_file("telemetry_trace.json", helpers.table_to_json(trace_payload))
        log("BENCH_TRACE_WRITTEN: telemetry_trace.json")
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
        raise RuntimeError(
            f"Factorio create failed:\nSTDOUT:\n{res_create.stdout}\nSTDERR:\n{res_create.stderr}\nLOG:\n{log_text}"
        )

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
        raise RuntimeError(
            f"Factorio benchmark failed:\nSTDOUT:\n{bench_res.stdout}\nSTDERR:\n{bench_res.stderr}\nLOG:\n{log_text}"
        )

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

    # Load telemetry trace JSON if generated
    trace_file = data_dir / "script-output" / "telemetry_trace.json"
    if trace_file.is_file():
        try:
            results["telemetry_trace"] = json.loads(trace_file.read_text())
        except Exception as e:
            results["telemetry_trace_error"] = str(e)

    return results


def main() -> int:
    """CLI entrypoint for running factory benchmark."""
    parser = argparse.ArgumentParser(description="Benchmark Factorio factory throughput.")
    parser.add_argument("blueprint_file", type=Path, help="Path to blueprint file")
    parser.add_argument("netlist_file", type=Path, help="Path to netlist JSON file")
    parser.add_argument("--ticks", type=int, default=3600, help="Simulation ticks to run")
    parser.add_argument("--game", type=Path, default=None, help="Path to Factorio binary")
    parser.add_argument(
        "--def", dest="def_file", type=Path, default=None, help="Path to factory.def file"
    )
    parser.add_argument(
        "--pins", dest="pins_file", type=Path, default=None, help="Path to pins.json file"
    )
    parser.add_argument(
        "--trace-out", type=Path, default=None, help="Path to export telemetry trace JSON"
    )
    parser.add_argument("--analyze", action="store_true", help="Run shape-based telemetry analysis")
    parser.add_argument(
        "--json-out", type=Path, default=None, help="Path to export analysis results as JSON"
    )

    args = parser.parse_args()
    game_bin = find_factorio_binary(args.game)
    bp_string = args.blueprint_file.read_text().strip()
    netlist_data = json.loads(args.netlist_file.read_text())

    input_pins, output_pins = resolve_factory_pins(
        args.blueprint_file,
        netlist_data,
        pins_file=args.pins_file,
        def_file=args.def_file,
    )

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="factorilog-bench-") as tmp:
        work_dir = Path(tmp)
        results = run_benchmark(
            bp_string,
            netlist_data,
            game_bin,
            work_dir,
            input_pins=input_pins,
            output_pins=output_pins,
            ticks=args.ticks,
        )

    if args.trace_out and "telemetry_trace" in results:
        args.trace_out.parent.mkdir(parents=True, exist_ok=True)
        args.trace_out.write_text(json.dumps(results["telemetry_trace"], indent=2))
        print(f"\nExported telemetry trace to: {args.trace_out}")

    print("\nBenchmark Execution Results:")
    print(f"  Simulation ticks: {results['ticks']} ({results['ticks'] / 60.0:.1f} game seconds)")
    print(f"  Revived entities: {results['revived']}")
    if "summary" in results:
        print(f"  Speed: {results['summary']}")

    if "telemetry_trace" in results:
        t_data = results["telemetry_trace"]
        num_samples = len(t_data.get("samples", []))
        print(f"  Telemetry trace: {num_samples} samples collected across taps")

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

    if args.analyze and "telemetry_trace" in results:
        try:
            import analyze_telemetry
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import analyze_telemetry

        analysis = analyze_telemetry.analyze_shapes(results["telemetry_trace"], netlist_data)
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(analysis, indent=2))
            print(f"\nExported shape analysis JSON to: {args.json_out}")

        print("\n" + analyze_telemetry.format_markdown_report(analysis))

    return 0


if __name__ == "__main__":
    sys.exit(main())
