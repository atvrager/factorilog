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

    control_lua = f"""
script.on_event(defines.events.on_tick, function(event)
    if event.tick == {ticks} then
        local surface = game.surfaces[1]
        local stats = game.forces.player.get_item_production_statistics(surface)
        local produced = stats.get_input_count("{expected_item}")
        for iname, icount in pairs(stats.input_counts) do
            log(string.format("PRODUCTION_STAT: %s = %d", iname, icount))
        end
        for iname, icount in pairs(stats.output_counts) do
            log(string.format("CONSUMPTION_STAT: %s = %d", iname, icount))
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
                                while #q > 0 and trace_step < 100 do
                                    trace_step = trace_step + 1
                                    local cur = table.remove(q, 1)
                                    for _, d in ipairs({{{{0, 1}}, {{0, -1}}, {{1, 0}}, {{-1, 0}}}}) do
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
                                                    if has_fluid or ne.name == "pump" or ne.name == "infinity-pipe" then
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
                        if (c1 + c2 == 0) or (b_info == "no_belt") then
                            local cur_tx = ptx
                            local cur_ty = pty
                            local tr_steps = 0
                            local visited = {{}}
                            while tr_steps < 2000 do
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
                                if c_tot > 0 then
                                    log(string.format("    UPSTREAM_ITEMS: found %d items [%s] after %d steps at (%.1f,%.1f) %s",
                                        c_tot, table.concat(found_items, ","), tr_steps, cur_tx, cur_ty, cur_b.name))
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

        local total_machines = #asms + #furnaces
        local active_machines = active_asms + active_furnaces
        local productive_machines = productive_asms + productive_furnaces
        local active_ratio = total_machines > 0 and (active_machines / total_machines) or 0.0

        local check_types = {{"assembling-machine", "furnace", "inserter", "lamp", "radar"}}
        local unpowered = 0
        for _, t in ipairs(check_types) do
            for _, ent in ipairs(surface.find_entities_filtered{{type = t}}) do
                if ent.electric_buffer_size and ent.electric_buffer_size > 0 and ent.energy == 0 then
                    unpowered = unpowered + 1
                    log(string.format("UNPOWERED_ENT: %s at (%.1f, %.1f)", ent.name, ent.position.x, ent.position.y))
                end
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
        log(string.format(
            "FACTORY_TEST_RESULT: item=%s produced=%d active=%d/%d ratio=%.3f productive=%d/%d unpowered=%d zero_prod=[%s]",
            "{expected_item}", produced, active_machines, total_machines, active_ratio, productive_machines, total_machines, unpowered, zero_prod_str
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

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        log_file = data_dir / "factorio-current.log"
        log_text = (
            log_file.read_text(encoding="utf-8") if log_file.is_file() else "No log file found."
        )
        raise RuntimeError(
            f"Factorio headless benchmark failed with code {res.returncode}:\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nLOG:\n{log_text}"
        )

    log_file = data_dir / "factorio-current.log"
    if not log_file.is_file():
        raise FileNotFoundError(f"Simulation log not written at {log_file}")

    log_text = log_file.read_text(encoding="utf-8")
    for line in log_text.splitlines():
        if (
            "ASM_INFO:" in line
            or "ZERO_PROD_ASM:" in line
            or "ZERO_PROD_FURNACE:" in line
            or "FURNACE_INV:" in line
            or "FURNACE_INSERTER" in line
            or "INSERTER at" in line
            or "SPLITTER at" in line
            or "PIPE at" in line
            or "TRACE step" in line
            or "UPSTREAM_" in line
            or "PRODUCTION_STAT:" in line
            or "CONSUMPTION_STAT:" in line
            or "BELT_INVENTORY:" in line
            or "NEARBY_BELTS_WITH_ITEMS" in line
            or "UNPOWERED_ENT:" in line
            or "DISCONNECTED_BIG_POLE:" in line
        ):
            print(f"factorilog: {line.strip()}")
        if "FACTORY_TEST_RESULT:" in line:
            result_line = line

    if not result_line:
        raise RuntimeError(f"No FACTORY_TEST_RESULT found in simulation log.\nLOG:\n{log_text}")

    m_prod = re.search(r"produced=(\d+)", result_line)
    m_active = re.search(r"active=(\d+)/(\d+)", result_line)
    m_ratio = re.search(r"ratio=([\d.]+)", result_line)
    m_productive = re.search(r"productive=(\d+)/(\d+)", result_line)
    m_unpowered = re.search(r"unpowered=(\d+)", result_line)
    m_zero_prod = re.search(r"zero_prod=\[(.*?)\]", result_line)

    return {
        "produced": int(m_prod.group(1)) if m_prod else 0,
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
        help="Expected produced item name",
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

    print(f"factorilog: Starting physical test for {args.expected_item}")

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
            expected_item=args.expected_item,
            ticks=args.ticks,
            game_bin=game_bin,
            work_dir=Path(tmp),
        )

    produced = metrics["produced"]
    active_ratio = metrics["active_ratio"]
    active_cnt = metrics["active_machines"]
    total_cnt = metrics["total_machines"]
    unpowered = metrics["unpowered"]

    productive_cnt = metrics["productive_machines"]
    zero_prod = metrics["zero_prod"]

    print(
        f"factorilog: Simulation completed at tick {args.ticks}: "
        f"{produced} {args.expected_item} produced (required >= {args.min_produced}), "
        f"active machines: {active_cnt}/{total_cnt} ({active_ratio * 100:.1f}%), "
        f"productive machines: {productive_cnt}/{total_cnt} ({productive_cnt / total_cnt * 100:.1f}%), "
        f"unpowered machines: {unpowered}"
    )
    if zero_prod:
        print(f"factorilog: Zero-production machines: {zero_prod}")

    failures = []
    if produced < args.min_produced:
        failures.append(
            f"Production below target: {produced} {args.expected_item} < minimum {args.min_produced}"
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
