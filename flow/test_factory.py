"""Automated factory regression test runner.

Runs headless Factorio simulation on generated factory saves.
Asserts production throughput, machine utilization, and physical metrics.
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
    raise FileNotFoundError("Factorio executable binary not found.")


def check_drc_report(drc_path: Path) -> list[str]:
    """Check routing DRC report for violations."""
    if not drc_path.is_file():
        return []
    content = drc_path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    violations = []
    for line in content.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue
        if "violation" in line_clean.lower() or "error" in line_clean.lower():
            violations.append(line_clean)
    return violations


def check_timing_report(timing_path: Path, min_slack: float = 0.0) -> float | None:
    """Parse worst slack from STA report and assert non-negative margin."""
    if not timing_path.is_file():
        return None
    content = timing_path.read_text(encoding="utf-8")
    if not content.strip():
        return None

    # Search for "worst slack [max|min] <float>" or "<float> slack (MET|VIOLATED)"
    m = re.search(r"worst\s+slack(?:\s+\w+)?\s+(-?[\d.]+)", content, re.IGNORECASE)
    if not m:
        m = re.search(r"(-?[\d.]+)\s+slack\s+\((?:MET|VIOLATED)\)", content, re.IGNORECASE)
    if not m:
        m = re.search(r"slack\s+\((?:MET|VIOLATED)\)\s+(-?[\d.]+)", content, re.IGNORECASE)
    if m:
        slack = float(m.group(1))
        if slack < min_slack:
            raise AssertionError(
                f"Timing slack regression: worst slack {slack:.4f} is less than minimum {min_slack:.4f}"
            )
        return slack
    return None


def check_def_area(def_path: Path, max_area: float | None = None) -> tuple[float, float, float]:
    """Parse die area from DEF file and assert maximum tile area."""
    content = def_path.read_text(encoding="utf-8")
    m_units = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", content)
    units = float(m_units.group(1)) if m_units else 1000.0

    m_die = re.search(
        r"DIEAREA\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*;",
        content,
    )
    if not m_die:
        return 0.0, 0.0, 0.0

    x1 = float(m_die.group(1)) / units
    y1 = float(m_die.group(2)) / units
    x2 = float(m_die.group(3)) / units
    y2 = float(m_die.group(4)) / units
    width = round(abs(x2 - x1), 2)
    height = round(abs(y2 - y1), 2)
    area = round(width * height, 2)

    if max_area is not None and area > max_area:
        raise AssertionError(
            f"Tile area regression: factory area {area:.1f} tiles ({width:.0f}x{height:.0f}) "
            f"exceeds maximum allowed area {max_area:.1f} tiles"
        )
    return width, height, area


def run_factory_simulation(
    save_zip: Path,
    expected_item: str,
    ticks: int,
    game_bin: Path,
    work_dir: Path,
    expected_items: list[str] | None = None,
    min_produced: int = 20,
    min_active_ratio: float = 0.70,
) -> dict[str, Any]:
    """Execute save in headless Factorio and extract production metrics."""
    mods_dir = work_dir / "mods"
    mod_dir = mods_dir / "factory_test_harness"
    data_dir = work_dir / "data"

    mod_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    config_file = work_dir / "config.ini"
    config_file.write_text(
        f"[path]\nread-data=__PATH__executable__/../../data\nwrite-data={data_dir}\n",
        encoding="utf-8",
    )

    info_json = {
        "name": "factory_test_harness",
        "version": "0.1.0",
        "title": "Factory Test Harness",
        "author": "factorilog",
        "factorio_version": "2.1",
        "dependencies": ["base >= 2.1"],
    }
    (mod_dir / "info.json").write_text(json.dumps(info_json), encoding="utf-8")

    mod_list = {
        "mods": [
            {"name": "base", "enabled": True},
            {"name": "factory_test_harness", "enabled": True},
            {"name": "elevated-rails", "enabled": False},
            {"name": "quality", "enabled": False},
            {"name": "recycler", "enabled": False},
            {"name": "space-age", "enabled": False},
        ]
    }
    (mods_dir / "mod-list.json").write_text(json.dumps(mod_list, indent=2), encoding="utf-8")

    exp_list = (
        expected_items
        if expected_items
        else [it.strip() for it in expected_item.split(",") if it.strip()]
    )
    lua_exp_items = ", ".join(f'"{it}"' for it in exp_list)
    warmup_tick = max(60, ticks // 2)
    sample_interval = max(300, ticks // 800)
    control_lua = f"""
local warmup_tick = {warmup_tick}
local warmup_done = false
local test_finished = false
local sample_interval = {sample_interval}
local expected_items = {{{lua_exp_items}}}
local min_produced = {min_produced}
local min_active_ratio = {min_active_ratio}
local warmup_input_counts = {{}}
local warmup_output_counts = {{}}
local first_output_tick = {{}}
local machine_cache = nil
local recipe_stats = {{}}

local function out_log(msg)
    log(msg)
    print(msg)
end

local function get_tl_item_counts(tl)
    local res = {{}}
    if tl then
        for k, item in pairs(tl.get_contents()) do
            local iname = (type(item) == "table" and item.name) or tostring(k)
            local icount = tonumber((type(item) == "table" and item.count) or item) or 0
            if iname and icount > 0 then
                res[iname] = (res[iname] or 0) + icount
            end
        end
    end
    return res
end

local function build_machine_cache(surface)
    local cache = {{}}
    local machs = surface.find_entities_filtered{{type = {{"assembling-machine", "furnace", "rocket-silo"}}}}
    for _, m in ipairs(machs) do
        local box = m.bounding_box
        local in_ins = {{}}
        local out_ins = {{}}
        local near_ins = surface.find_entities_filtered{{
            area = {{{{box.left_top.x - 1.5, box.left_top.y - 1.5}}, {{box.right_bottom.x + 1.5, box.right_bottom.y + 1.5}}}},
            type = "inserter"
        }}
        for _, ins in ipairs(near_ins) do
            local dx_drop = ins.drop_position.x
            local dy_drop = ins.drop_position.y
            local dx_pick = ins.pickup_position.x
            local dy_pick = ins.pickup_position.y
            if dx_drop >= box.left_top.x - 0.6 and dx_drop <= box.right_bottom.x + 0.6 and
               dy_drop >= box.left_top.y - 0.6 and dy_drop <= box.right_bottom.y + 0.6 then
                local ptx = math.floor(dx_pick) + 0.5
                local pty = math.floor(dy_pick) + 0.5
                local pbelt = nil
                for _, be in ipairs(surface.find_entities_filtered{{position = {{ptx, pty}}}}) do
                    if be.type == "transport-belt" or be.type == "underground-belt" or be.type == "splitter" then
                        pbelt = be
                        break
                    end
                end
                table.insert(in_ins, {{ins = ins, belt = pbelt}})
            elseif dx_pick >= box.left_top.x - 0.6 and dx_pick <= box.right_bottom.x + 0.6 and
                   dy_pick >= box.left_top.y - 0.6 and dy_pick <= box.right_bottom.y + 0.6 then
                local dtx = math.floor(dx_drop) + 0.5
                local dty = math.floor(dy_drop) + 0.5
                local dbelt = nil
                for _, be in ipairs(surface.find_entities_filtered{{position = {{dtx, dty}}}}) do
                    if be.type == "transport-belt" or be.type == "underground-belt" or be.type == "splitter" then
                        dbelt = be
                        break
                    end
                end
                table.insert(out_ins, {{ins = ins, belt = dbelt}})
            end
        end
        table.insert(cache, {{
            ent = m,
            in_ins = in_ins,
            out_ins = out_ins,
            warmup_finished = m.products_finished or 0
        }})
    end
    return cache
end

local function get_machine_recipe(m, force)
    local rec = m.get_recipe()
    if rec then return rec, rec.name end
    if m.type == "furnace" and m.previous_recipe then
        local pr = m.previous_recipe.name
        local pname = (type(pr) == "string" and pr) or (pr and pr.name) or nil
        if pname and force and force.recipes[pname] then
            return force.recipes[pname], pname
        end
        if pname then return nil, tostring(pname) end
    end
    return nil, m.name
end

local function check_all_pass_criteria(surface, stats)
    for _, it in ipairs(expected_items) do
        if stats.get_input_count(it) < min_produced then
            return false
        end
    end
    if not machine_cache then
        machine_cache = build_machine_cache(surface)
    end
    local total_m = 0
    local active_m = 0
    local prod_m = 0
    for _, mc in ipairs(machine_cache) do
        local m = mc.ent
        if m and m.valid then
            total_m = total_m + 1
            local pf = m.products_finished or 0
            if m.type == "rocket-silo" then
                local parts = m.rocket_parts or 0
                if m.is_crafting() or pf > 0 or parts > 0 then active_m = active_m + 1 end
                if pf > 0 or parts > 0 then prod_m = prod_m + 1 end
            else
                if m.is_crafting() then active_m = active_m + 1 end
                if pf > 0 then prod_m = prod_m + 1 end
            end
        end
    end
    if prod_m < total_m then
        return false
    end
    local ratio = total_m > 0 and (active_m / total_m) or 0.0
    if ratio < min_active_ratio then
        return false
    end
    local check_types = {{"assembling-machine", "furnace", "rocket-silo", "inserter", "lamp", "radar", "pump"}}
    for _, t in ipairs(check_types) do
        for _, ent in ipairs(surface.find_entities_filtered{{type = t}}) do
            if ent.electric_buffer_size and ent.electric_buffer_size > 0 and ent.energy == 0 then
                return false
            end
        end
    end
    for _, bp in ipairs(surface.find_entities_filtered{{name = "big-electric-pole"}}) do
        local conn = bp.get_wire_connector(defines.wire_connector_id.pole_copper, false)
        if not conn or #conn.connections == 0 then
            return false
        end
    end
    return true
end

script.on_event(defines.events.on_tick, function(event)
    if test_finished then return end
    local early_pass = false
    if event.tick > 0 and event.tick % 600 == 0 then
        local surface = game.surfaces[1]
        local stats = game.forces.player.get_item_production_statistics(surface)
        for iname, icount in pairs(stats.input_counts) do
            if icount > 0 and not first_output_tick[iname] then
                first_output_tick[iname] = event.tick
            end
        end
        if not warmup_done then
            local all_started = true
            for _, it in ipairs(expected_items) do
                if it == "space-science-pack" then
                    if stats.get_input_count("rocket-part") == 0 and stats.get_input_count(it) == 0 then
                        all_started = false
                        break
                    end
                elseif stats.get_input_count(it) == 0 then
                    all_started = false
                    break
                end
            end
            if all_started or event.tick >= warmup_tick then
                warmup_done = true
                warmup_tick = event.tick
                for iname, icount in pairs(stats.input_counts) do
                    warmup_input_counts[iname] = icount
                end
                for iname, icount in pairs(stats.output_counts) do
                    warmup_output_counts[iname] = icount
                end
                machine_cache = build_machine_cache(surface)
            end
        end
        if warmup_done and check_all_pass_criteria(surface, stats) then
            early_pass = true
        end
    end
    if warmup_done and not early_pass and event.tick > warmup_tick and event.tick <= {ticks} and (event.tick - warmup_tick) % sample_interval == 0 then
        if not machine_cache then
            machine_cache = build_machine_cache(game.surfaces[1])
        end
        local force = game.forces.player
        for _, mc in ipairs(machine_cache) do
            local m = mc.ent
            if m and m.valid then
                local rec = mc.rec
                local rname = mc.rname
                if not rec then
                    rec, rname = get_machine_recipe(m, force)
                    if rec then
                        mc.rec = rec
                        mc.rname = rname
                    end
                end
                local st = recipe_stats[rname]
                if not st then
                    st = {{
                        machines = 0,
                        samples = 0,
                        working = 0,
                        item_shortage = 0,
                        inserter_limited = 0,
                        belt_starved = 0,
                        fluid_shortage = 0,
                        full_output = 0,
                        output_belt_full = 0,
                        output_inserter_limited = 0,
                        other = 0,
                        starved_by = {{}},
                        inserter_lag_by = {{}}
                    }}
                    recipe_stats[rname] = st
                end
                st.samples = st.samples + 1
                local status = m.status
                if status == defines.entity_status.item_ingredient_shortage or status == defines.entity_status.no_ingredients then
                    st.item_shortage = st.item_shortage + 1
                    local inv_idx = defines.inventory.crafter_input or defines.inventory.assembling_machine_input or 2
                    local in_inv = m.get_inventory(inv_idx)
                    local belt_items = {{}}
                    for _, ii in ipairs(mc.in_ins) do
                        if ii.ins.valid and ii.ins.held_stack.valid_for_read then
                            local hname = ii.ins.held_stack.name
                            belt_items[hname] = (belt_items[hname] or 0) + ii.ins.held_stack.count
                        end
                        if ii.belt and ii.belt.valid then
                            for li = 1, 2 do
                                local tl = ii.belt.get_transport_line(li)
                                for iname, icnt in pairs(get_tl_item_counts(tl)) do
                                    belt_items[iname] = (belt_items[iname] or 0) + icnt
                                end
                            end
                        end
                    end
                    local any_belt_missing = false
                    if rec and rec.ingredients then
                        for _, ing in ipairs(rec.ingredients) do
                            if ing.type == "item" then
                                local have = in_inv and in_inv.get_item_count(ing.name) or 0
                                if have < ing.amount then
                                    if (belt_items[ing.name] or 0) == 0 then
                                        any_belt_missing = true
                                        st.starved_by[ing.name] = (st.starved_by[ing.name] or 0) + 1
                                    else
                                        st.inserter_lag_by[ing.name] = (st.inserter_lag_by[ing.name] or 0) + 1
                                    end
                                end
                            end
                        end
                    else
                        local total_b = 0
                        for _, c in pairs(belt_items) do total_b = total_b + c end
                        if total_b == 0 then any_belt_missing = true end
                    end
                    if any_belt_missing then
                        st.belt_starved = st.belt_starved + 1
                    else
                        st.inserter_limited = st.inserter_limited + 1
                    end
                elseif status == defines.entity_status.fluid_ingredient_shortage then
                    st.fluid_shortage = st.fluid_shortage + 1
                elseif status == defines.entity_status.full_output then
                    st.full_output = st.full_output + 1
                    local belt_blocked = false
                    for _, oi in ipairs(mc.out_ins) do
                        if oi.ins and oi.ins.valid then
                            if oi.ins.status == defines.entity_status.waiting_for_space_in_destination then
                                belt_blocked = true
                            elseif oi.belt and oi.belt.valid then
                                local c1 = oi.belt.get_transport_line(1).get_item_count()
                                local c2 = oi.belt.get_transport_line(2).get_item_count()
                                if math.max(c1, c2) >= 3 and oi.ins.held_stack.valid_for_read then
                                    belt_blocked = true
                                end
                            end
                        end
                    end
                    if belt_blocked then
                        st.output_belt_full = st.output_belt_full + 1
                    else
                        st.output_inserter_limited = st.output_inserter_limited + 1
                    end
                elseif m.is_crafting() or status == defines.entity_status.working or status == defines.entity_status.normal then
                    st.working = st.working + 1
                else
                    st.other = st.other + 1
                end
            end
        end
    end
    if event.tick > 0 and event.tick % 60000 == 0 then
        local surface = game.surfaces[1]
        local stats = game.forces.player.get_item_production_statistics(surface)
        local silos = surface.find_entities_filtered{{type = "rocket-silo"}}
        local s_parts = (#silos > 0 and silos[1].rocket_parts) or 0
        local exp_parts = {{}}
        for item in string.gmatch("{expected_item}", "[^,%s]+") do
            table.insert(exp_parts, string.format("%s=%d", item, stats.get_input_count(item)))
        end
        local pmsg = string.format("PROGRESS: tick=%d %s rocket-part=%d satellite=%d pu=%d lds=%d rf=%d silo_parts=%d",
            event.tick,
            table.concat(exp_parts, " "),
            stats.get_input_count("rocket-part"),
            stats.get_input_count("satellite"),
            stats.get_input_count("processing-unit"),
            stats.get_input_count("low-density-structure"),
            stats.get_input_count("rocket-fuel"),
            s_parts)
        log(pmsg)
        print(pmsg)
    end
    if early_pass or event.tick == {ticks} then
        test_finished = true
        local cur_tick = event.tick
        local surface = game.surfaces[1]
        local force = game.forces.player
        local stats = force.get_item_production_statistics(surface)
        local produced = stats.get_input_count("{expected_item}")
        local win_mins = (cur_tick - warmup_tick) / 3600.0
        local tot_mins = cur_tick / 3600.0
        out_log(string.format("FORCE_BONUSES: inserter_stack=%d bulk_inserter=%d",
            force.inserter_stack_size_bonus, force.bulk_inserter_capacity_bonus))
        for iname, icount in pairs(stats.input_counts) do
            local wcount = warmup_input_counts[iname] or 0
            local delta = icount - wcount
            local cum_rate = tot_mins > 0 and (icount / tot_mins) or 0
            local ss_rate = win_mins > 0 and (delta / win_mins) or 0
            local ftick = first_output_tick[iname] or -1
            out_log(string.format("PRODUCTION_STAT: %s = %d", iname, icount))
            out_log(string.format("STEADY_PRODUCTION: %s total=%d steady_delta=%d cum_spm=%.2f steady_spm=%.2f first_tick=%d",
                iname, icount, delta, cum_rate, ss_rate, ftick))
        end
        for iname, icount in pairs(stats.output_counts) do
            local wcount = warmup_output_counts[iname] or 0
            local delta = icount - wcount
            local cum_rate = tot_mins > 0 and (icount / tot_mins) or 0
            local ss_rate = win_mins > 0 and (delta / win_mins) or 0
            out_log(string.format("CONSUMPTION_STAT: %s = %d", iname, icount))
            out_log(string.format("STEADY_CONSUMPTION: %s total=%d steady_delta=%d cum_spm=%.2f steady_spm=%.2f",
                iname, icount, delta, cum_rate, ss_rate))
        end
        local fstats = force.get_fluid_production_statistics(surface)
        if fstats then
            for fname, fcount in pairs(fstats.input_counts) do
                out_log(string.format("FLUID_PRODUCTION_STAT: %s = %.1f (%.2f/min)", fname, fcount, tot_mins > 0 and (fcount / tot_mins) or 0))
            end
            for fname, fcount in pairs(fstats.output_counts) do
                out_log(string.format("FLUID_CONSUMPTION_STAT: %s = %.1f (%.2f/min)", fname, fcount, tot_mins > 0 and (fcount / tot_mins) or 0))
            end
        end
        if machine_cache then
            local mach_counts = {{}}
            local craft_deltas = {{}}
            for _, mc in ipairs(machine_cache) do
                local m = mc.ent
                if m and m.valid then
                    local rec, rname = get_machine_recipe(m, force)
                    mach_counts[rname] = (mach_counts[rname] or 0) + 1
                    craft_deltas[rname] = (craft_deltas[rname] or 0) + ((m.products_finished or 0) - mc.warmup_finished)
                end
            end
            for rname, st in pairs(recipe_stats) do
                local s = st.samples > 0 and st.samples or 1
                local mcnt = mach_counts[rname] or 0
                local cdelta = craft_deltas[rname] or 0
                local c_spm = win_mins > 0 and (cdelta / win_mins) or 0
                local sb_parts = {{}}
                for ing, cnt in pairs(st.starved_by) do
                    table.insert(sb_parts, string.format("%s:%d", ing, cnt))
                end
                local il_parts = {{}}
                for ing, cnt in pairs(st.inserter_lag_by) do
                    table.insert(il_parts, string.format("%s:%d", ing, cnt))
                end
                out_log(string.format(
                    "RECIPE_BOTTLENECK: recipe=%s machines=%d crafts_spm=%.2f working=%.1f%% item_shortage=%.1f%% (belt_starved=%.1f%% inserter_limited=%.1f%%) fluid_shortage=%.1f%% full_output=%.1f%% (belt_full=%.1f%% out_inserter=%.1f%%) starved_by=[%s] inserter_lag=[%s]",
                    rname, mcnt, c_spm,
                    (st.working / s) * 100.0,
                    (st.item_shortage / s) * 100.0,
                    (st.belt_starved / s) * 100.0,
                    (st.inserter_limited / s) * 100.0,
                    (st.fluid_shortage / s) * 100.0,
                    (st.full_output / s) * 100.0,
                    (st.output_belt_full / s) * 100.0,
                    (st.output_inserter_limited / s) * 100.0,
                    table.concat(sb_parts, ","),
                    table.concat(il_parts, ",")
                ))
            end
        end
        if early_pass then
            local total_m = 0
            local active_m = 0
            for _, mc in ipairs(machine_cache or {{}}) do
                local m = mc.ent
                if m and m.valid then
                    total_m = total_m + 1
                    if m.type == "rocket-silo" then
                        local pf = m.products_finished or 0
                        local parts = m.rocket_parts or 0
                        if m.is_crafting() or pf > 0 or parts > 0 then active_m = active_m + 1 end
                    else
                        if m.is_crafting() then active_m = active_m + 1 end
                    end
                end
            end
            local act_ratio = total_m > 0 and (active_m / total_m) or 0.0
            out_log(string.format(
                "FACTORY_TEST_RESULT: tick=%d item=%s produced=%d active=%d/%d ratio=%.3f productive=%d/%d unpowered=0 zero_prod=[]",
                cur_tick, "{expected_item}", produced, active_m, total_m, act_ratio, total_m, total_m
            ))
            out_log(string.format("FACTORY_TEST_EARLY_PASS: tick=%d", cur_tick))
            return
        end


        local function format_tl_contents(tl, li)
            local res = {{}}
            if tl then
                for k, item in pairs(tl.get_contents()) do
                    local iname = (type(item) == "table" and item.name) or tostring(k)
                    local icount = (type(item) == "table" and item.count) or tostring(item)
                    if li then
                        table.insert(res, string.format("%s:L%d:%s", iname, li, icount))
                    else
                        table.insert(res, string.format("%s:%s", iname, icount))
                    end
                end
            end
            return res
        end

        local asms = surface.find_entities_filtered{{type = "assembling-machine"}}
        local active_asms = 0
        local productive_asms = 0
        local zero_prod = {{}}
        for _, a in ipairs(asms) do
            if a.is_crafting() then active_asms = active_asms + 1 end
            local pf = a.products_finished or 0
            local rec = a.get_recipe()
            local sname = tostring(a.status)
            for k, v in pairs(defines.entity_status) do
                if v == a.status then sname = k break end
            end
            local fluid_info = ""
            local fc = a.get_fluid_contents()
            if fc then
                for fname, famt in pairs(fc) do
                    fluid_info = fluid_info .. string.format(" %s:%.1f", fname, famt)
                end
            end
            log(string.format("ASM_INFO: at (%.1f,%.1f) recipe=%s status=%s finished=%d crafting=%s%s",
                a.position.x, a.position.y,
                tostring(rec and rec.name),
                sname,
                pf,
                tostring(a.is_crafting()),
                fluid_info
            ))
            if pf > 0 then
                productive_asms = productive_asms + 1
            else
                table.insert(zero_prod, string.format("%s at (%.1f,%.1f)", a.name, a.position.x, a.position.y))
                log(string.format("ZERO_PROD_ASM: at (%.1f,%.1f) recipe=%s status=%s energy=%.1f",
                    a.position.x, a.position.y,
                    tostring(rec and rec.name),
                    sname,
                    a.energy
                ))
            end
            if (sname == "fluid_ingredient_shortage") or (sname == "item_ingredient_shortage") or (pf == 0) then
                if sname == "fluid_ingredient_shortage" then
                    local pipes = surface.find_entities_filtered{{area = {{{{a.position.x - 4, a.position.y - 4}}, {{a.position.x + 4, a.position.y + 4}}}}}}
                    for _, pe in ipairs(pipes) do
                        if pe.name == "pipe" or pe.name == "pipe-to-ground" or pe.name == "infinity-pipe" then
                            local f_str = "empty"
                            local pfc = pe.get_fluid_contents()
                            if pfc then
                                for pfname, pfamt in pairs(pfc) do
                                    f_str = string.format("%s:%.1f", pfname, pfamt)
                                end
                            end
                            log(string.format("  PIPE at (%.1f,%.1f) name=%s fluid=%s", pe.position.x, pe.position.y, pe.name, f_str))
                            if f_str == "empty" then
                                local visited_pipes = {{}}
                                local q = {{pe}}
                                local found_source_or_fluid = false
                                local trace_step = 0
                                visited_pipes[string.format("%.1f,%.1f", pe.position.x, pe.position.y)] = true
                                while #q > 0 and trace_step < 300 do
                                    trace_step = trace_step + 1
                                    local cur = table.remove(q, 1)
                                    local step_list = {{{{0, 1}}, {{0, -1}}, {{1, 0}}, {{-1, 0}}}}
                                    if cur.name == "pipe-to-ground" then
                                        local udx, udy = 0, 0
                                        if cur.direction == 0 then udy = 1
                                        elseif cur.direction == 8 then udy = -1
                                        elseif cur.direction == 4 then udx = -1
                                        elseif cur.direction == 12 then udx = 1
                                        end
                                        for dist = 1, 10 do
                                            table.insert(step_list, {{udx * dist, udy * dist}})
                                        end
                                    end
                                    for _, d in ipairs(step_list) do
                                        local npos = {{cur.position.x + d[1], cur.position.y + d[2]}}
                                        local nkey = string.format("%.1f,%.1f", npos[1], npos[2])
                                        if not visited_pipes[nkey] then
                                            local nents = surface.find_entities_filtered{{position = npos}}
                                            for _, ne in ipairs(nents) do
                                                if ne.name == "pipe" or ne.name == "pipe-to-ground" or ne.name == "pump" or ne.name == "infinity-pipe" then
                                                    visited_pipes[nkey] = true
                                                    local nfc = ne.get_fluid_contents()
                                                    local has_fluid = false
                                                    local nf_str = "empty"
                                                    if nfc then
                                                        for nfn, nfa in pairs(nfc) do
                                                            if nfa > 0 then has_fluid = true end
                                                            nf_str = string.format("%s:%.1f", nfn, nfa)
                                                        end
                                                    end
                                                    log(string.format("    TRACE step %d: %s at (%.1f,%.1f) dir=%s fluid=%s energy=%.1f",
                                                        trace_step, ne.name, ne.position.x, ne.position.y, tostring(ne.direction), nf_str, ne.energy or 0))
                                                    if has_fluid or ne.name == "infinity-pipe" then
                                                        found_source_or_fluid = true
                                                    else
                                                        table.insert(q, ne)
                                                    end
                                                end
                                            end
                                        end
                                    end
                                    if found_source_or_fluid then break end
                                end
                            end
                        end
                    end
                end
                local inserters = surface.find_entities_filtered{{type = "inserter"}}
                for _, ins in ipairs(inserters) do
                    if math.abs(ins.drop_position.x - a.position.x) <= 2.0 and math.abs(ins.drop_position.y - a.position.y) <= 2.0 then
                        local held = ins.held_stack.valid_for_read and ins.held_stack.name or "empty"
                        local ptx = math.floor(ins.pickup_position.x) + 0.5
                        local pty = math.floor(ins.pickup_position.y) + 0.5
                        local belt_ents = surface.find_entities_filtered{{position = {{ptx, pty}}}}
                        local b_info = "no_belt"
                        local c1 = 0
                        local c2 = 0
                        local item_names = {{}}
                        for _, be in ipairs(belt_ents) do
                            if be.type == "transport-belt" then
                                c1 = be.get_transport_line(1).get_item_count()
                                c2 = be.get_transport_line(2).get_item_count()
                                for li = 1, 2 do
                                    for _, s in ipairs(format_tl_contents(be.get_transport_line(li), li)) do
                                        table.insert(item_names, s)
                                    end
                                end
                                b_info = string.format("belt dir=%s items=%d+%d", tostring(be.direction), c1, c2)
                            elseif be.type == "underground-belt" then
                                c1 = be.get_transport_line(1).get_item_count()
                                c2 = be.get_transport_line(2).get_item_count()
                                for li = 1, 2 do
                                    for _, s in ipairs(format_tl_contents(be.get_transport_line(li), li)) do
                                        table.insert(item_names, s)
                                    end
                                end
                                b_info = string.format("ug_belt dir=%s type=%s items=%d+%d", tostring(be.direction), be.belt_to_ground_type, c1, c2)
                            end
                        end
                        if #item_names > 0 then
                            b_info = b_info .. " [" .. table.concat(item_names, ",") .. "]"
                        end
                        log(string.format("  INSERTER at (%.1f,%.1f) dir=%s held=%s pickup=(%.1f,%.1f) %s",
                            ins.position.x, ins.position.y, tostring(ins.direction), held, ptx, pty, b_info
                        ))
                        if (c1 + c2 == 0) or (b_info == "no_belt") or (pf == 0) then
                            local cur_tx = ptx
                            local cur_ty = pty
                            local tr_steps = 0
                            local visited = {{}}
                            while tr_steps < 60 do
                                tr_steps = tr_steps + 1
                                local cur_b = nil
                                for _, e in ipairs(surface.find_entities_filtered{{position = {{cur_tx, cur_ty}}}}) do
                                    if e.type == "transport-belt" or e.type == "underground-belt" or e.type == "splitter" then
                                        cur_b = e
                                        break
                                    end
                                end
                                if not cur_b then
                                    log(string.format("    UPSTREAM_GAP: after %d steps, no belt at (%.1f,%.1f)", tr_steps, cur_tx, cur_ty))
                                    break
                                end
                                local vkey = string.format("%.1f,%.1f", cur_tx, cur_ty)
                                if visited[vkey] then
                                    log(string.format("    UPSTREAM_CYCLE: after %d steps, cycle at %s (%s)", tr_steps, vkey, cur_b.name))
                                    break
                                end
                                visited[vkey] = true

                                local c_tot = 0
                                local found_items = {{}}
                                for li = 1, (cur_b.type == "splitter" and 8 or 2) do
                                    local tl = cur_b.get_transport_line(li)
                                    if tl then
                                        c_tot = c_tot + tl.get_item_count()
                                        for _, s in ipairs(format_tl_contents(tl)) do
                                            table.insert(found_items, s)
                                        end
                                    end
                                end
                                log(string.format("    UPSTREAM_STEP %d: at (%.1f,%.1f) %s dir=%s items=[%s]",
                                    tr_steps, cur_tx, cur_ty, cur_b.name, tostring(cur_b.direction), table.concat(found_items, ",")))
                                if (pf > 0) and (c_tot > 0) then
                                    break
                                end
                                local bdir = cur_b.direction
                                local dx, dy = 0, 0
                                if bdir == 0 then dy = 1
                                elseif bdir == 4 then dx = -1
                                elseif bdir == 8 then dy = -1
                                elseif bdir == 12 then dx = 1
                                end
                                if cur_b.type == "underground-belt" and cur_b.belt_to_ground_type == "output" then
                                    local found_in = false
                                    for ustep = 1, 11 do
                                        local unx = cur_tx + dx * ustep
                                        local uny = cur_ty + dy * ustep
                                        for _, ue in ipairs(surface.find_entities_filtered{{position = {{unx, uny}}}}) do
                                            if ue.type == "underground-belt" and ue.belt_to_ground_type == "input" and ue.direction == bdir then
                                                cur_tx = unx
                                                cur_ty = uny
                                                found_in = true
                                                break
                                            end
                                        end
                                        if found_in then break end
                                    end
                                    if not found_in then
                                        log(string.format("    UPSTREAM_UG_UNPAIRED: output at (%.1f,%.1f) dir=%d", cur_tx, cur_ty, bdir))
                                        break
                                    end
                                elseif cur_b.type == "splitter" then
                                    local in_cands = {{}}
                                    if bdir == 0 then
                                        in_cands = {{{{cur_b.position.x - 0.5, cur_b.position.y + 1}}, {{cur_b.position.x + 0.5, cur_b.position.y + 1}}}}
                                    elseif bdir == 8 then
                                        in_cands = {{{{cur_b.position.x - 0.5, cur_b.position.y - 1}}, {{cur_b.position.x + 0.5, cur_b.position.y - 1}}}}
                                    elseif bdir == 4 then
                                        in_cands = {{{{cur_b.position.x - 1, cur_b.position.y - 0.5}}, {{cur_b.position.x - 1, cur_b.position.y + 0.5}}}}
                                    elseif bdir == 12 then
                                        in_cands = {{{{cur_b.position.x + 1, cur_b.position.y - 0.5}}, {{cur_b.position.x + 1, cur_b.position.y + 0.5}}}}
                                    end
                                    local best_in = nil
                                    for _, ic in ipairs(in_cands) do
                                        for _, ie in ipairs(surface.find_entities_filtered{{position = ic}}) do
                                            if (ie.type == "transport-belt" or ie.type == "underground-belt" or ie.type == "splitter") and (ie.direction == bdir or ie.type == "splitter") then
                                                local icnt = 0
                                                for li = 1, (ie.type == "splitter" and 8 or 2) do
                                                    local tl = ie.get_transport_line(li)
                                                    if tl then icnt = icnt + tl.get_item_count() end
                                                end
                                                if icnt > 0 then
                                                    best_in = ic
                                                    break
                                                elseif not best_in then
                                                    best_in = ic
                                                end
                                            end
                                        end
                                        if best_in and best_in == ic then break end
                                    end
                                    if best_in then
                                        cur_tx = best_in[1]
                                        cur_ty = best_in[2]
                                    else
                                        cur_tx = cur_tx + dx
                                        cur_ty = cur_ty + dy
                                    end
                                else
                                    local straight_x = cur_tx + dx
                                    local straight_y = cur_ty + dy
                                    local next_b = nil
                                    for _, e in ipairs(surface.find_entities_filtered{{position = {{straight_x, straight_y}}}}) do
                                        if (e.type == "transport-belt" or e.type == "underground-belt" or e.type == "splitter") and (e.direction == bdir or e.type == "splitter") then
                                            next_b = e
                                            break
                                        end
                                    end
                                    if next_b then
                                        cur_tx = straight_x
                                        cur_ty = straight_y
                                    else
                                        local turn_found = false
                                        local candidates = {{
                                            {{tdx = 0, tdy = -1, exp_dir = 8}}, -- from North, pointing South
                                            {{tdx = 0, tdy = 1, exp_dir = 0}},  -- from South, pointing North
                                            {{tdx = -1, tdy = 0, exp_dir = 4}}, -- from West, pointing East
                                            {{tdx = 1, tdy = 0, exp_dir = 12}}, -- from East, pointing West
                                        }}
                                        -- Do not check the downstream direction
                                        local ds_dx, ds_dy = -dx, -dy
                                        for _, td in ipairs(candidates) do
                                            if not (td.tdx == ds_dx and td.tdy == ds_dy) then
                                                local tx = cur_tx + td.tdx
                                                local ty = cur_ty + td.tdy
                                                for _, te in ipairs(surface.find_entities_filtered{{position = {{tx, ty}}}}) do
                                                    if (te.type == "transport-belt" or te.type == "underground-belt" or te.type == "splitter") and te.direction == td.exp_dir then
                                                        cur_tx = tx
                                                        cur_ty = ty
                                                        turn_found = true
                                                        break
                                                    end
                                                end
                                                if turn_found then break end
                                            end
                                        end
                                        if not turn_found then
                                            cur_tx = straight_x
                                            cur_ty = straight_y
                                        end
                                    end
                                end
                            end
                            if tr_steps >= 2000 then
                                log(string.format("    UPSTREAM_LIMIT: reached 2000 steps without items at (%.1f,%.1f)", cur_tx, cur_ty))
                            end
                        end
                    end
                end
                local splitters = surface.find_entities_filtered{{area = {{{{a.position.x - 4, a.position.y - 4}}, {{a.position.x + 4, a.position.y + 4}}}}, type = "splitter"}}
                for _, sp in ipairs(splitters) do
                    local lines_info = {{}}
                    for li = 1, 8 do
                        local tl = sp.get_transport_line(li)
                        if tl and tl.get_item_count() > 0 then
                            table.insert(lines_info, string.format("L%d=%d", li, tl.get_item_count()))
                        end
                    end
                    log(string.format("SPLITTER at (%.1f,%.1f) dir=%s contents=[%s]",
                        sp.position.x, sp.position.y, tostring(sp.direction), table.concat(lines_info, ",")
                    ))
                end
                local belts = surface.find_entities_filtered{{area = {{{{a.position.x - 4, a.position.y - 4}}, {{a.position.x + 4, a.position.y + 4}}}}, type = "transport-belt"}}
                local belts_with_items = {{}}
                for _, b in ipairs(belts) do
                    local c1 = b.get_transport_line(1).get_item_count()
                    local c2 = b.get_transport_line(2).get_item_count()
                    if c1 + c2 > 0 then
                        table.insert(belts_with_items, string.format("(%.1f,%.1f:d=%d:%d+%d)", b.position.x, b.position.y, b.direction, c1, c2))
                    end
                end
                if #belts_with_items > 0 then
                    log(string.format("NEARBY_BELTS_WITH_ITEMS (%d): %s", #belts_with_items, table.concat(belts_with_items, " ")))
                end
            end
        end

        local furnaces = surface.find_entities_filtered{{type = "furnace"}}
        local active_furnaces = 0
        local productive_furnaces = 0
        for _, f in ipairs(furnaces) do
            if f.is_crafting() then active_furnaces = active_furnaces + 1 end
            local pf = f.products_finished or 0
            if pf > 0 then
                productive_furnaces = productive_furnaces + 1
            else
                table.insert(zero_prod, string.format("%s at (%.1f,%.1f)", f.name, f.position.x, f.position.y))
                log(string.format("ZERO_PROD_FURNACE: at (%.1f,%.1f) energy=%.1f", f.position.x, f.position.y, f.energy))
                local inv_in = f.get_inventory(1)
                local inv_out = f.get_inventory(2)
                local in_str = (inv_in and not inv_in.is_empty()) and string.format("%s x%d", inv_in[1].name, inv_in[1].count) or "empty"
                local out_str = (inv_out and not inv_out.is_empty()) and string.format("%s x%d", inv_out[1].name, inv_out[1].count) or "empty"
                log(string.format("  FURNACE_INV: in=%s out=%s status=%s", in_str, out_str, tostring(f.status)))
                local f_inserters = surface.find_entities_filtered{{type = "inserter"}}
                for _, ins in ipairs(f_inserters) do
                    if math.abs(ins.drop_position.x - f.position.x) <= 2.0 and math.abs(ins.drop_position.y - f.position.y) <= 2.0 then
                        local held = ins.held_stack.valid_for_read and ins.held_stack.name or "empty"
                        local ptx = math.floor(ins.pickup_position.x) + 0.5
                        local pty = math.floor(ins.pickup_position.y) + 0.5
                        local belt_ents = surface.find_entities_filtered{{position = {{ptx, pty}}}}
                        local b_info = "no_belt"
                        local f_item_names = {{}}
                        for _, be in ipairs(belt_ents) do
                            if be.type == "transport-belt" then
                                local c1 = be.get_transport_line(1).get_item_count()
                                local c2 = be.get_transport_line(2).get_item_count()
                                for li = 1, 2 do
                                    for _, s in ipairs(format_tl_contents(be.get_transport_line(li), li)) do
                                        table.insert(f_item_names, s)
                                    end
                                end
                                b_info = string.format("belt dir=%s items=%d+%d", tostring(be.direction), c1, c2)
                            end
                        end
                        if #f_item_names > 0 then
                            b_info = b_info .. " [" .. table.concat(f_item_names, ",") .. "]"
                        end
                        log(string.format("  FURNACE_INSERTER at (%.1f,%.1f) dir=%s held=%s pickup=(%.1f,%.1f) %s",
                            ins.position.x, ins.position.y, tostring(ins.direction), held, ptx, pty, b_info
                        ))
                    end
                end
            end
        end

        local silos = surface.find_entities_filtered{{type = "rocket-silo"}}
        local active_silos = 0
        local productive_silos = 0
        for _, s in ipairs(silos) do
            local pf = s.products_finished or 0
            local parts = s.rocket_parts or 0
            log(string.format("SILO_INFO: at (%.1f,%.1f) finished=%d rocket_parts=%d crafting=%s",
                s.position.x, s.position.y, pf, parts, tostring(s.is_crafting())))
            if s.is_crafting() or pf > 0 or parts > 0 then active_silos = active_silos + 1 end
            if pf > 0 or parts > 0 then
                productive_silos = productive_silos + 1
            else
                table.insert(zero_prod, string.format("%s at (%.1f,%.1f)", s.name, s.position.x, s.position.y))
            end
        end

        local total_machines = #asms + #furnaces + #silos
        local active_machines = active_asms + active_furnaces + active_silos
        local productive_machines = productive_asms + productive_furnaces + productive_silos
        local active_ratio = total_machines > 0 and (active_machines / total_machines) or 0.0

        local check_types = {{"assembling-machine", "furnace", "rocket-silo", "inserter", "lamp", "radar", "pump"}}
        local unpowered = 0
        for _, t in ipairs(check_types) do
            for _, ent in ipairs(surface.find_entities_filtered{{type = t}}) do
                if ent.electric_buffer_size and ent.electric_buffer_size > 0 and ent.energy == 0 then
                    unpowered = unpowered + 1
                    log(string.format("UNPOWERED_ENT: %s at (%.1f, %.1f)", ent.name, ent.position.x, ent.position.y))
                end
            end
        end

        local inf_pipes = surface.find_entities_filtered{{name = "infinity-pipe"}}
        for _, ip in ipairs(inf_pipes) do
            local fc = ip.get_fluid_contents()
            local fc_str = "empty"
            if fc then
                for k, v in pairs(fc) do fc_str = string.format("%s:%.1f", k, v) end
            end
            local filt = ip.get_infinity_pipe_filter()
            local filt_str = filt and string.format("%s:%.1f:%s", filt.name, filt.percentage or 1.0, filt.mode or "none") or "no_filter"
            log(string.format("INFINITY_PIPE: at (%.1f,%.1f) filter=%s fluid=%s", ip.position.x, ip.position.y, filt_str, fc_str))
            for _, d in ipairs({{{{0, 1}}, {{0, -1}}, {{1, 0}}, {{-1, 0}}}}) do
                local npos = {{ip.position.x + d[1], ip.position.y + d[2]}}
                local nents = surface.find_entities_filtered{{position = npos}}
                for _, ne in ipairs(nents) do
                    local nfc = ne.get_fluid_contents()
                    local nfc_str = "empty"
                    if nfc then
                        for k, v in pairs(nfc) do nfc_str = string.format("%s:%.1f", k, v) end
                    end
                    log(string.format("  ADJ_TO_INFINITY: %s at (%.1f,%.1f) fluid=%s", ne.name, ne.position.x, ne.position.y, nfc_str))
                end
            end
        end

        local water_ip = surface.find_entities_filtered{{position = {{-274.5, 178.5}}}}[1]
        local crude_ip = surface.find_entities_filtered{{position = {{-274.5, 18.5}}}}[1]
        if water_ip and crude_ip then
            local q = {{water_ip}}
            local parent = {{}}
            local vis = {{}}
            local key_start = string.format("%.1f,%.1f", water_ip.position.x, water_ip.position.y)
            local key_target = string.format("%.1f,%.1f", crude_ip.position.x, crude_ip.position.y)
            vis[key_start] = true
            local found_target = false
            local vis_count = 1
            while #q > 0 and not found_target do
                local cur = table.remove(q, 1)
                local cur_k = string.format("%.1f,%.1f", cur.position.x, cur.position.y)
                if cur_k == key_target then
                    found_target = true
                    break
                end
                local candidates = {{}}
                for _, d in ipairs({{{{0, 1}}, {{0, -1}}, {{1, 0}}, {{-1, 0}}}}) do
                    local npos = {{cur.position.x + d[1], cur.position.y + d[2]}}
                    for _, ne in ipairs(surface.find_entities_filtered{{position = npos}}) do
                        if ne.name == "pipe" or ne.name == "pump" or ne.name == "infinity-pipe" or ne.name == "pipe-to-ground" or ne.type == "assembling-machine" then
                            table.insert(candidates, ne)
                        end
                    end
                end
                if cur.name == "pipe-to-ground" then
                    local ug_dir = cur.direction
                    local dx, dy = 0, 0
                    if ug_dir == defines.direction.north then dx, dy = 0, 1
                    elseif ug_dir == defines.direction.south then dx, dy = 0, -1
                    elseif ug_dir == defines.direction.east then dx, dy = -1, 0
                    elseif ug_dir == defines.direction.west then dx, dy = 1, 0
                    end
                    for s = 1, 10 do
                        local upos = {{cur.position.x + dx * s, cur.position.y + dy * s}}
                        for _, ue in ipairs(surface.find_entities_filtered{{position = upos}}) do
                            if ue.name == "pipe-to-ground" and (ue.direction + 8) % 16 == ug_dir then
                                table.insert(candidates, ue)
                                break
                            end
                        end
                    end
                end
                for _, cand in ipairs(candidates) do
                    local ck = string.format("%.1f,%.1f", cand.position.x, cand.position.y)
                    if not vis[ck] then
                        vis[ck] = true
                        vis_count = vis_count + 1
                        parent[ck] = cur_k
                        table.insert(q, cand)
                    end
                end
            end
            log(string.format("FLUID_PATH_SEARCH: found_target=%s, visited=%d", tostring(found_target), vis_count))
            if found_target then
                local path = {{}}
                local step_k = key_target
                while step_k do
                    table.insert(path, step_k)
                    step_k = parent[step_k]
                end
                log(string.format("FLUID_PATH_BETWEEN_WATER_AND_CRUDE (%d steps): %s", #path, table.concat(path, " -> ")))
            end
        end

        local big_poles = surface.find_entities_filtered{{name = "big-electric-pole"}}
        for _, bp in ipairs(big_poles) do
            local conn = bp.get_wire_connector(defines.wire_connector_id.pole_copper, false)
            if not conn or #conn.connections == 0 then
                unpowered = unpowered + 1
                log(string.format("DISCONNECTED_BIG_POLE: at (%.1f, %.1f)", bp.position.x, bp.position.y))
            end
        end


        local item_totals = {{}}
        for _, e in ipairs(surface.find_entities_filtered{{type = {{"transport-belt", "underground-belt", "splitter"}}}}) do
            for li = 1, (e.type == "splitter" and 8 or 2) do
                local tl = e.get_transport_line(li)
                if tl then
                    local fc = format_tl_contents(tl)
                    for _, entry in ipairs(fc) do
                        local iname = entry:match("^([^:]+)")
                        local icnt = tonumber(entry:match(":(%d+)$")) or 0
                        if iname and icnt > 0 then
                            item_totals[iname] = (item_totals[iname] or 0) + icnt
                        end
                    end
                end
            end
        end
        for iname, icnt in pairs(item_totals) do
            log(string.format("BELT_INVENTORY: %s = %d", iname, icnt))
        end

        local zero_prod_str = table.concat(zero_prod, "; ")
        out_log(string.format(
            "FACTORY_TEST_RESULT: tick=%d item=%s produced=%d active=%d/%d ratio=%.3f productive=%d/%d unpowered=%d zero_prod=[%s]",
            cur_tick, "{expected_item}", produced, active_machines, total_machines, active_ratio, productive_machines, total_machines, unpowered, zero_prod_str
        ))
    end
end)
"""
    (mod_dir / "control.lua").write_text(control_lua, encoding="utf-8")

    cmd = [
        str(game_bin),
        "-c",
        str(config_file),
        "--mod-directory",
        str(mods_dir),
        "--benchmark",
        str(save_zip),
        "--benchmark-ticks",
        str(ticks + 5),
        "--benchmark-sanitize",
    ]

    stdout_lines: list[str] = []
    early_pass = False
    completed_tick = ticks
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as proc:
        if proc.stdout is not None:
            for raw_line in proc.stdout:
                stdout_lines.append(raw_line)
                if "PROGRESS:" in raw_line:
                    print(f"factorilog: {raw_line.strip()}", flush=True)
                if "FACTORY_TEST_EARLY_PASS:" in raw_line:
                    m_ep = re.search(r"tick=(\d+)", raw_line)
                    if m_ep:
                        completed_tick = int(m_ep.group(1))
                    early_pass = True
                    proc.terminate()
                    break
        returncode = proc.wait()

    if returncode != 0 and not early_pass:
        log_file = data_dir / "factorio-current.log"
        log_text = (
            log_file.read_text(encoding="utf-8") if log_file.is_file() else "No log file found."
        )
        raise RuntimeError(
            f"Factorio headless benchmark failed with code {returncode}:\n"
            f"OUTPUT:\n{''.join(stdout_lines)}\nLOG:\n{log_text}"
        )

    log_file = data_dir / "factorio-current.log"
    if not early_pass and not log_file.is_file():
        raise FileNotFoundError(f"Simulation log not written at {log_file}")

    log_text = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
    source_lines = stdout_lines if early_pass else log_text.splitlines()
    production_stats: dict[str, int] = {}
    result_line = ""
    for line in source_lines:
        m_pstat = re.search(r"PRODUCTION_STAT:\s*(\S+)\s*=\s*(\d+)", line)
        if m_pstat:
            production_stats[m_pstat.group(1)] = int(m_pstat.group(2))
        if (
            "ASM_INFO:" in line
            or "SILO_INFO:" in line
            or "SILO_INSERTER" in line
            or "LANDING_PAD_INFO:" in line
            or "ZERO_PROD_ASM:" in line
            or "ZERO_PROD_FURNACE:" in line
            or "FURNACE_INV:" in line
            or "FURNACE_INSERTER" in line
            or "INSERTER at" in line
            or "SPLITTER at" in line
            or "PIPE at" in line
            or "TRACE step" in line
            or "UPSTREAM_" in line
            or "FORCE_BONUSES:" in line
            or "PRODUCTION_STAT:" in line
            or "STEADY_PRODUCTION:" in line
            or "CONSUMPTION_STAT:" in line
            or "STEADY_CONSUMPTION:" in line
            or "FLUID_PRODUCTION_STAT:" in line
            or "FLUID_CONSUMPTION_STAT:" in line
            or "RECIPE_BOTTLENECK:" in line
            or "BELT_INVENTORY:" in line
            or "NEARBY_BELTS_WITH_ITEMS" in line
            or "UNPOWERED_ENT:" in line
            or "DISCONNECTED_BIG_POLE:" in line
            or "INFINITY_PIPE:" in line
            or "ADJ_TO_INFINITY:" in line
            or "FLUID_PATH_" in line
        ):
            print(f"factorilog: {line.strip()}")
        if "FACTORY_TEST_RESULT:" in line:
            result_line = line

    if not result_line:
        raise RuntimeError(
            f"No FACTORY_TEST_RESULT found in simulation log.\n"
            f"STDOUT:\n{''.join(stdout_lines)}\nLOG:\n{log_text}"
        )

    m_tick = re.search(r"tick=(\d+)", result_line)
    if m_tick:
        completed_tick = int(m_tick.group(1))
    m_prod = re.search(r"produced=(\d+)", result_line)
    m_active = re.search(r"active=(\d+)/(\d+)", result_line)
    m_ratio = re.search(r"ratio=([\d.]+)", result_line)
    m_productive = re.search(r"productive=(\d+)/(\d+)", result_line)
    m_unpowered = re.search(r"unpowered=(\d+)", result_line)
    m_zero_prod = re.search(r"zero_prod=\[(.*?)\]", result_line)

    return {
        "completed_tick": completed_tick,
        "produced": int(m_prod.group(1)) if m_prod else 0,
        "production_stats": production_stats,
        "active_machines": int(m_active.group(1)) if m_active else 0,
        "total_machines": int(m_active.group(2)) if m_active else 0,
        "active_ratio": float(m_ratio.group(1)) if m_ratio else 0.0,
        "productive_machines": int(m_productive.group(1)) if m_productive else 0,
        "unpowered": int(m_unpowered.group(1)) if m_unpowered else 0,
        "zero_prod": m_zero_prod.group(1) if m_zero_prod else "",
    }


def main() -> None:
    """Entry point for factory regression test."""
    parser = argparse.ArgumentParser(description="Factorilog automated factory test runner.")
    parser.add_argument(
        "--save", type=Path, required=True, help="Path to playable Factorio save .zip"
    )
    parser.add_argument(
        "--expected-item",
        type=str,
        default="automation-science-pack",
        help="Expected produced item name (or comma-separated list of item names)",
    )
    parser.add_argument(
        "--min-produced", type=int, default=20, help="Minimum produced items required"
    )
    parser.add_argument("--ticks", type=int, default=7200, help="Simulation duration in game ticks")
    parser.add_argument(
        "--min-active-ratio", type=float, default=0.70, help="Minimum machine activity ratio"
    )
    parser.add_argument(
        "--def-file", type=Path, default=None, help="Optional routed DEF file for area validation"
    )
    parser.add_argument("--max-area", type=float, default=None, help="Maximum allowed tile area")
    parser.add_argument("--sta-rpt", type=Path, default=None, help="Optional STA report file")
    parser.add_argument("--min-slack", type=float, default=0.0, help="Minimum allowed worst slack")
    parser.add_argument(
        "--drc-rpt", type=Path, default=None, help="Optional routing DRC report file"
    )
    parser.add_argument(
        "--report-out", type=Path, default=None, help="Optional path for JSON test report"
    )
    parser.add_argument(
        "--factorio-bin", type=Path, default=None, help="Optional override for Factorio executable"
    )

    args = parser.parse_args()
    expected_items = [it.strip() for it in args.expected_item.split(",") if it.strip()]
    primary_expected_item = expected_items[-1] if expected_items else args.expected_item

    print(f"factorilog: Starting physical test for {', '.join(expected_items)}")

    # 1. Routing DRC check
    if args.drc_rpt:
        violations = check_drc_report(args.drc_rpt)
        if violations:
            sys.stderr.write(f"DRC check failed with {len(violations)} violation(s):\n")
            for v in violations:
                sys.stderr.write(f"  {v}\n")
            sys.exit(1)
        print("factorilog: DRC validation passed (0 violations)")

    # 2. Timing slack check
    slack = None
    if args.sta_rpt:
        slack = check_timing_report(args.sta_rpt, min_slack=args.min_slack)
        if slack is not None:
            print(
                f"factorilog: Timing validation passed (worst slack = {slack:.4f} >= {args.min_slack:.4f})"
            )

    # 3. Area check
    width, height, area = 0.0, 0.0, 0.0
    if args.def_file and args.def_file.is_file():
        width, height, area = check_def_area(args.def_file, max_area=args.max_area)
        print(f"factorilog: Area validation passed ({width:.0f}x{height:.0f} = {area:.1f} tiles)")

    # 4. Headless simulation
    try:
        game_bin = find_factorio_binary(args.factorio_bin)
    except FileNotFoundError as err:
        sys.stderr.write(f"Warning: {err}. Skipping simulation test.\n")
        sys.exit(0)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="factorilog-sim-") as tmp:
        metrics = run_factory_simulation(
            save_zip=args.save,
            expected_item=primary_expected_item,
            ticks=args.ticks,
            game_bin=game_bin,
            work_dir=Path(tmp),
            expected_items=expected_items,
            min_produced=args.min_produced,
            min_active_ratio=args.min_active_ratio,
        )

    completed_tick = metrics.get("completed_tick", args.ticks)
    produced = metrics["produced"]
    production_stats = metrics.get("production_stats", {})
    active_ratio = metrics["active_ratio"]
    active_cnt = metrics["active_machines"]
    total_cnt = metrics["total_machines"]
    unpowered = metrics["unpowered"]

    productive_cnt = metrics["productive_machines"]
    zero_prod = metrics["zero_prod"]

    prod_summary = ", ".join(
        f"{production_stats.get(it, produced if it == primary_expected_item else 0)} {it}"
        for it in expected_items
    )
    print(
        f"factorilog: Simulation completed at tick {completed_tick} (max {args.ticks}): "
        f"{prod_summary} produced (required >= {args.min_produced} each), "
        f"active machines: {active_cnt}/{total_cnt} ({active_ratio * 100:.1f}%), "
        f"productive machines: {productive_cnt}/{total_cnt} ({productive_cnt / total_cnt * 100:.1f}%), "
        f"unpowered machines: {unpowered}"
    )
    if zero_prod:
        print(f"factorilog: Zero-production machines: {zero_prod}")

    failures = []
    for it in expected_items:
        it_prod = production_stats.get(it, produced if it == primary_expected_item else 0)
        if it_prod < args.min_produced:
            failures.append(
                f"Production below target: {it_prod} {it} < minimum {args.min_produced}"
            )
    if productive_cnt < total_cnt:
        failures.append(
            f"Machine productivity below 100%: {productive_cnt}/{total_cnt} machines produced items. "
            f"Zero-production machines: {zero_prod}"
        )
    if active_ratio < args.min_active_ratio:
        failures.append(
            f"Machine utilization below target: {active_ratio * 100:.1f}% < minimum {args.min_active_ratio * 100:.1f}%"
        )
    if unpowered > 0:
        failures.append(f"Unpowered machines detected: {unpowered}")

    if args.report_out:
        report_data = {
            "item": args.expected_item,
            "ticks": args.ticks,
            "produced": produced,
            "min_produced": args.min_produced,
            "active_machines": active_cnt,
            "total_machines": total_cnt,
            "active_ratio": active_ratio,
            "unpowered": unpowered,
            "width": width,
            "height": height,
            "area": area,
            "slack": slack,
            "passed": len(failures) == 0,
        }
        args.report_out.write_text(json.dumps(report_data, indent=2) + "\n", encoding="utf-8")

    if failures:
        sys.stderr.write("factorilog: Test assertions failed:\n")
        for f in failures:
            sys.stderr.write(f"  - {f}\n")
        sys.exit(1)

    print(f"factorilog: All verification assertions passed for {args.expected_item}.")


if __name__ == "__main__":
    main()
