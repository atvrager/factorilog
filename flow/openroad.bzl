"""Bazel rules for incremental OpenROAD physical design flow."""

def _openroad_stage_impl(ctx):
    # Collect all inputs
    inputs = list(ctx.files.srcs) + [ctx.file.script]
    outputs = ctx.outputs.outs

    # Prepare command running OpenROAD in Podman container
    work_dir = outputs[0].dirname
    tcl_file = ctx.file.script.path

    # Build copy commands to stage inputs in the action work directory
    cmd_parts = [
        "set -euo pipefail",
        "mkdir -p '%s'" % work_dir,
    ]

    for src in ctx.files.srcs:
        target = "%s/%s" % (work_dir, src.basename)
        if src.path != target:
            cmd_parts.append("cp -f '%s' '%s'" % (src.path, target))

    script_target = "%s/%s" % (work_dir, ctx.file.script.basename)
    if tcl_file != script_target:
        cmd_parts.append("cp -f '%s' '%s'" % (tcl_file, script_target))

    # Podman command execution
    container_cmd = (
        "TMPDIR=/tmp podman run --rm --network none " +
        "--security-opt label=disable " +
        "-v \"$(pwd)/%s\":/work " % work_dir +
        "-w /work " +
        "docker.io/openroad/orfs:latest " +
        "bash -lc \"source /OpenROAD-flow-scripts/env.sh >/dev/null && openroad -no_init -exit /work/%s\"" % ctx.file.script.basename
    )
    cmd_parts.append(container_cmd)

    full_cmd = "\n".join(cmd_parts)

    ctx.actions.run_shell(
        inputs = inputs,
        outputs = outputs,
        command = full_cmd,
        mnemonic = "OpenROAD",
        execution_requirements = {
            "no-sandbox": "1",
            "local": "1",
        },
    )

    return [DefaultInfo(files = depset(outputs))]

openroad_stage = rule(
    implementation = _openroad_stage_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True, doc = "Input files needed for stage"),
        "script": attr.label(allow_single_file = [".tcl"], mandatory = True, doc = "OpenROAD TCL script"),
        "outs": attr.output_list(mandatory = True, doc = "Output files created by stage"),
    },
    doc = "Executes one incremental OpenROAD physical design stage in Podman.",
)


def _factorilog_synth_impl(ctx):
    out_dir = ctx.outputs.netlist_v.dirname
    outputs = [
        ctx.outputs.cells_v,
        ctx.outputs.netlist_v,
        ctx.outputs.netlist_json,
        ctx.outputs.tech_lef,
        ctx.outputs.tech_lib,
    ]

    script = ctx.executable._netbuild

    extra_flags = []
    if ctx.attr.telemetry_taps:
        extra_flags.append("--telemetry-taps '%s'" % ctx.attr.telemetry_taps)
    if ctx.attr.enable_display_readouts:
        extra_flags.append("--enable-display-readouts")
    else:
        extra_flags.append("--no-enable-display-readouts")
    if ctx.attr.enable_circuit_readouts:
        extra_flags.append("--enable-circuit-readouts")
    else:
        extra_flags.append("--no-enable-circuit-readouts")
    if ctx.attr.enable_compound_cells:
        extra_flags.append("--enable-compound-cells")
    else:
        extra_flags.append("--no-enable-compound-cells")
    if ctx.attr.synthesize_splitters:
        extra_flags.append("--synthesize-splitters")
    else:
        extra_flags.append("--no-synthesize-splitters")

    flags_str = " ".join(extra_flags)
    target_str = ctx.attr.demand if ctx.attr.demand else ("--spc %s" % ctx.attr.spc)

    inputs = [ctx.file.dump]
    ctrl_export = ""
    ctrl_flag = ""
    if ctx.file.controller_verilog:
        ctrl_export = "CTRL_ABS=\"$(pwd)/%s\"" % ctx.file.controller_verilog.path
        ctrl_flag = "--controller-verilog \"$CTRL_ABS\""
        inputs.append(ctx.file.controller_verilog)

    cmd_parts = [
        "set -euo pipefail",
        "OUT_DIR=\"$(pwd)/%s\"" % out_dir,
        "WORK_DIR=\"$OUT_DIR/%s_synth_work\"" % ctx.attr.name,
        "mkdir -p \"$WORK_DIR\"",
        "DUMP_ABS=\"$(pwd)/%s\"" % ctx.file.dump.path,
        "NETBUILD_ABS=\"$(pwd)/%s\"" % script.path,
    ]
    if ctrl_export:
        cmd_parts.append(ctrl_export)
    cmd_parts.extend([
        "cd \"$WORK_DIR\"",
        "START_NS=$(date +%s%N)",
        "\"$NETBUILD_ABS\" \"$DUMP_ABS\" %s %s %s" % (target_str, flags_str, ctrl_flag),
        "END_NS=$(date +%s%N)",
        "ELAPSED_NS=$((END_NS - START_NS))",
        "ELAPSED_SEC=$(awk \"BEGIN {printf \\\"%.3f\\\", $ELAPSED_NS / 1000000000}\")",
        "cp -f \"$WORK_DIR/cells.v\" \"$OUT_DIR/%s\"" % ctx.outputs.cells_v.basename,
        "cp -f \"$WORK_DIR/netlist.v\" \"$OUT_DIR/%s\"" % ctx.outputs.netlist_v.basename,
        "cp -f \"$WORK_DIR/netlist.json\" \"$OUT_DIR/%s\"" % ctx.outputs.netlist_json.basename,
        "cp -f \"$WORK_DIR/tech.lef\" \"$OUT_DIR/%s\"" % ctx.outputs.tech_lef.basename,
        "cp -f \"$WORK_DIR/tech.lib\" \"$OUT_DIR/%s\"" % ctx.outputs.tech_lib.basename,
    ])

    if ctx.outputs.ranks_tcl:
        outputs.append(ctx.outputs.ranks_tcl)
        cmd_parts.append("if [ -f \"$WORK_DIR/ranks.tcl\" ]; then cp -f \"$WORK_DIR/ranks.tcl\" \"$OUT_DIR/%s\"; fi" % ctx.outputs.ranks_tcl.basename)

    cmd_parts.append("rm -rf \"$WORK_DIR\"")

    if ctx.outputs.time_json:
        outputs.append(ctx.outputs.time_json)
        cmd_parts.append("echo \"{\\\"stage\\\": \\\"synth\\\", \\\"elapsed_seconds\\\": $ELAPSED_SEC}\" > \"$OUT_DIR/%s\"" % ctx.outputs.time_json.basename)

    cmd_parts.append("echo \"factorilog [%s]: Synth completed in ${ELAPSED_SEC}s\"" % ctx.attr.name)

    ctx.actions.run_shell(
        inputs = inputs,
        outputs = outputs,
        command = "\n".join(cmd_parts),
        mnemonic = "FactorilogSynth",
        tools = [script],
        execution_requirements = {
            "no-sandbox": "1",
            "local": "1",
        },
    )

    return [DefaultInfo(files = depset(outputs))]

factorilog_synth = rule(
    implementation = _factorilog_synth_impl,
    attrs = {
        "dump": attr.label(allow_single_file = [".json"], mandatory = True, doc = "Dump fixture JSON file"),
        "demand": attr.string(default = "", doc = "Item and rate demand string (e.g. 'automation-science-pack 60.0')"),
        "spc": attr.string(default = "60.0", doc = "Science packs per minute rate (used if demand is not set)"),
        "telemetry_taps": attr.string(default = "none", doc = "Telemetry tap scope: none, io, merges, io,merges, all"),
        "enable_display_readouts": attr.bool(default = True, doc = "Enable display panel readouts"),
        "enable_circuit_readouts": attr.bool(default = False, doc = "Enable arithmetic combinator readouts"),
        "enable_compound_cells": attr.bool(default = False, doc = "Enable compound macro-cell clustering"),
        "synthesize_splitters": attr.bool(default = True, doc = "Synthesize binary trees of splitters for multi-consumer nets"),
        "controller_verilog": attr.label(allow_single_file = [".v"], doc = "Optional Verilog controller file"),
        "cells_v": attr.output(mandatory = True),
        "netlist_v": attr.output(mandatory = True),
        "netlist_json": attr.output(mandatory = True),
        "tech_lef": attr.output(mandatory = True),
        "tech_lib": attr.output(mandatory = True),
        "ranks_tcl": attr.output(doc = "Optional output TCL script containing instance ranks"),
        "time_json": attr.output(doc = "Optional output synthesis timing JSON file"),
        "_netbuild": attr.label(
            default = Label("//foundry:netbuild"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Synthesizes Verilog netlist, LEF, and Liberty from Factorio prototype dump.",
)


def _factorilog_pnr_impl(ctx):
    out_def = ctx.outputs.def_file
    out_drc = ctx.outputs.route_drc
    out_timing = ctx.outputs.timing_rpt
    out_time = ctx.outputs.time_json
    out_log = ctx.outputs.log_file

    inputs = [ctx.file.netlist_v, ctx.file.tech_lef, ctx.file.tech_lib, ctx.file.script]
    if ctx.file.ranks_tcl:
        inputs.append(ctx.file.ranks_tcl)
    outputs = [out_def, out_time]
    if out_drc:
        outputs.append(out_drc)
    if out_timing:
        outputs.append(out_timing)
    if out_log:
        outputs.append(out_log)

    work_dir = "%s/%s_pnr_work" % (out_def.dirname, ctx.attr.name)

    cmd_parts = [
        "set -euo pipefail",
        "WORK_DIR='%s'" % work_dir,
        "mkdir -p \"$WORK_DIR\"",
        "cp -f '%s' \"$WORK_DIR/netlist.v\"" % ctx.file.netlist_v.path,
        "cp -f '%s' \"$WORK_DIR/tech.lef\"" % ctx.file.tech_lef.path,
        "cp -f '%s' \"$WORK_DIR/tech.lib\"" % ctx.file.tech_lib.path,
        "cp -f '%s' \"$WORK_DIR/factorilog.tcl\"" % ctx.file.script.path,
    ]
    if ctx.file.ranks_tcl:
        cmd_parts.append("cp -f '%s' \"$WORK_DIR/ranks.tcl\"" % ctx.file.ranks_tcl.path)
    cmd_parts.extend([
        "cat << 'EOF' > \"$WORK_DIR/run_pnr.tcl\"",
        "set BLOCK_FILL %s" % ctx.attr.block_fill,
        "set CELL_PAD_SITES %d" % ctx.attr.cell_pad_sites,
        "set SPLIT_PAD_SITES %d" % ctx.attr.split_pad_sites,
        "set CORE_MARGIN %d" % ctx.attr.core_margin,
        "set density %s" % ctx.attr.density,
        "set ROUTABILITY_DRIVEN %s" % ("1" if ctx.attr.routability_driven else "0"),
        "set LAYER_ADJUSTMENT \"%s\"" % ctx.attr.layer_adjustment,
        "set USE_ROW_PLACEMENT %s" % ("1" if ctx.attr.use_row_placement else "0"),
        "set CHANNEL_H %d" % ctx.attr.channel_h,
        "set USE_CHASSIS %s" % ("1" if ctx.attr.use_chassis else "0"),
        "set CHASSIS_GRID_W %d" % ctx.attr.chassis_grid_w,
        "set CHASSIS_GRID_H %d" % ctx.attr.chassis_grid_h,
        "set CHASSIS_PITCH %d" % ctx.attr.chassis_pitch,
        "set CHASSIS_CORE_W %d" % ctx.attr.chassis_core_w,
        "set CHASSIS_CORE_H %d" % ctx.attr.chassis_core_h,
        "set CHASSIS_CHANNEL_W %d" % ctx.attr.chassis_channel_w,
        "set DO_UNTANGLE %s" % ("1" if ctx.attr.do_untangle else "0"),
        "set SKIP_ROUTER %s" % ("1" if ctx.attr.skip_router else "0"),
        "set DROUTE_END_ITER 50",
        "source [file join [file dirname [info script]] factorilog.tcl]",
        "EOF",
        "START_NS=$(date +%s%N)",
        "TMPDIR=/tmp podman run --rm --network none " +
        "--security-opt label=disable " +
        "-v \"$(pwd)/$WORK_DIR\":/work " +
        "-w /work " +
        "docker.io/openroad/orfs:latest " +
        "bash -lc \"source /OpenROAD-flow-scripts/env.sh >/dev/null && openroad -no_init -exit /work/run_pnr.tcl\" > \"$WORK_DIR/pnr.log\" 2>&1 || (cat \"$WORK_DIR/pnr.log\" >&2 && exit 1)",
        "END_NS=$(date +%s%N)",
        "ELAPSED_NS=$((END_NS - START_NS))",
        "ELAPSED_SEC=$(awk \"BEGIN {printf \\\"%.3f\\\", $ELAPSED_NS / 1000000000}\")",
        "cp -f \"$WORK_DIR/factory.def\" '%s'" % out_def.path,
    ])

    if out_drc:
        cmd_parts.append(
            "if [ -f \"$WORK_DIR/route_drc.rpt\" ]; then cp -f \"$WORK_DIR/route_drc.rpt\" '%s'; else touch '%s'; fi" %
            (out_drc.path, out_drc.path),
        )
    if out_timing:
        cmd_parts.append(
            "if [ -f \"$WORK_DIR/timing.rpt\" ]; then cp -f \"$WORK_DIR/timing.rpt\" '%s'; else touch '%s'; fi" %
            (out_timing.path, out_timing.path),
        )
    if out_log:
        cmd_parts.append(
            "if [ -f \"$WORK_DIR/pnr.log\" ]; then cp -f \"$WORK_DIR/pnr.log\" '%s'; else touch '%s'; fi" %
            (out_log.path, out_log.path),
        )

    cmd_parts.append("echo \"{\\\"stage\\\": \\\"pnr\\\", \\\"elapsed_seconds\\\": $ELAPSED_SEC}\" > '%s'" % out_time.path)
    cmd_parts.append("echo \"factorilog [%s]: PnR completed in ${ELAPSED_SEC}s\"" % ctx.attr.name)
    cmd_parts.append("rm -rf \"$WORK_DIR\"")

    full_cmd = "\n".join(cmd_parts)

    ctx.actions.run_shell(
        inputs = inputs,
        outputs = outputs,
        command = full_cmd,
        mnemonic = "FactorilogPnR",
        execution_requirements = {
            "no-sandbox": "1",
            "local": "1",
        },
    )

    return [DefaultInfo(files = depset(outputs))]

factorilog_pnr = rule(
    implementation = _factorilog_pnr_impl,
    attrs = {
        "netlist_v": attr.label(allow_single_file = [".v"], mandatory = True, doc = "Structural Verilog netlist"),
        "tech_lef": attr.label(allow_single_file = [".lef"], mandatory = True, doc = "Technology LEF file"),
        "tech_lib": attr.label(allow_single_file = [".lib"], mandatory = True, doc = "Technology Liberty file"),
        "script": attr.label(
            default = Label("//flow:factorilog.tcl"),
            allow_single_file = [".tcl"],
            doc = "OpenROAD flow script template",
        ),
        "block_fill": attr.string(default = "0.08", doc = "Target cell fill fraction"),
        "cell_pad_sites": attr.int(default = 4, doc = "Cell padding sites"),
        "density": attr.string(default = "0.18", doc = "Target placement density"),
        "core_margin": attr.int(default = 4, doc = "Core margin in sites"),
        "split_pad_sites": attr.int(default = 5, doc = "Splitter and merger padding sites"),
        "routability_driven": attr.bool(default = True, doc = "Enable routability-driven placement"),
        "layer_adjustment": attr.string(default = "", doc = "Global routing layer adjustment factor"),
        "use_row_placement": attr.bool(default = False, doc = "Enable structured linear row placement"),
        "channel_h": attr.int(default = 12, doc = "Channel height in sites"),
        "use_chassis": attr.bool(default = False, doc = "Enable chassis floorplan with street channels and blockages"),
        "chassis_grid_w": attr.int(default = 0, doc = "Chassis grid width in city blocks (0 for auto)"),
        "chassis_grid_h": attr.int(default = 0, doc = "Chassis grid height in city blocks (0 for auto)"),
        "chassis_pitch": attr.int(default = 100, doc = "Chassis block pitch in tiles"),
        "chassis_core_w": attr.int(default = 94, doc = "Chassis core width in tiles"),
        "chassis_core_h": attr.int(default = 94, doc = "Chassis core height in tiles"),
        "chassis_channel_w": attr.int(default = 6, doc = "Chassis street channel width in tiles"),
        "do_untangle": attr.bool(default = True, doc = "Enable pre-route pin untangling in OpenROAD TCL"),
        "skip_router": attr.bool(default = True, doc = "Skip OpenROAD global/detailed routing; route with native maze router"),
        "ranks_tcl": attr.label(allow_single_file = [".tcl"], doc = "Optional TCL script containing instance ranks"),
        "def_file": attr.output(mandatory = True, doc = "Routed DEF output file"),
        "route_drc": attr.output(doc = "DRC report output file"),
        "timing_rpt": attr.output(doc = "Timing STA report output file"),
        "time_json": attr.output(mandatory = True, doc = "Timing JSON output file"),
        "log_file": attr.output(doc = "PnR log output file"),
    },
    doc = "Executes OpenROAD placement and routing for a factory design.",
)


def _factorilog_blueprint_impl(ctx):
    out_bp = ctx.outputs.blueprint
    out_pins = ctx.outputs.pins_json
    out_time = ctx.outputs.time_json
    def_file = ctx.file.def_file
    netlist_json = ctx.file.netlist_json
    label = ctx.attr.label_text

    args = [
        def_file.path,
        netlist_json.path,
        "-o",
        out_bp.path,
        "--label",
        label,
    ]
    outputs = [out_bp]
    if out_pins:
        args.extend(["--pins-out", out_pins.path])
        outputs.append(out_pins)
    if out_time:
        args.extend(["--time-out", out_time.path])
        outputs.append(out_time)
    if not ctx.attr.strict_drc:
        args.append("--no-strict-drc")
    if ctx.attr.use_maze_router:
        args.append("--use-maze-router")

    ctx.actions.run(
        inputs = [def_file, netlist_json],
        outputs = outputs,
        executable = ctx.executable._def2bp,
        tools = [ctx.executable._def2bp],
        arguments = args,
        mnemonic = "FactorilogBlueprint",
    )

    return [DefaultInfo(files = depset(outputs))]

factorilog_blueprint = rule(
    implementation = _factorilog_blueprint_impl,
    attrs = {
        "def_file": attr.label(allow_single_file = [".def"], mandatory = True, doc = "Placed and routed DEF file"),
        "netlist_json": attr.label(allow_single_file = [".json"], mandatory = True, doc = "Netlist sidecar metadata"),
        "label_text": attr.string(default = "factory", doc = "Blueprint label"),
        "blueprint": attr.output(mandatory = True, doc = "Output blueprint file"),
        "pins_json": attr.output(doc = "Optional output pins JSON metadata file"),
        "time_json": attr.output(doc = "Optional output blueprint timing JSON file"),
        "strict_drc": attr.bool(default = True, doc = "Enforce strict zero physical DRC violations"),
        "use_maze_router": attr.bool(default = True, doc = "Route nets using 3D multi-layer DualLaneMazeRouter"),
        "_def2bp": attr.label(
            default = Label("//foundry:def2bp"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Converts placed DEF geometry and netlist sidecar into a blueprint file.",
)


def _factorilog_render_impl(ctx):
    blueprint = ctx.file.blueprint
    png = ctx.outputs.png
    time_json = ctx.outputs.time_json
    script = ctx.executable._render_bp

    args = [
        blueprint.path,
        "-o",
        png.path,
    ]
    outputs = [png]
    if time_json:
        args.extend(["--time-out", time_json.path])
        outputs.append(time_json)

    ctx.actions.run(
        inputs = [blueprint],
        outputs = outputs,
        executable = script,
        tools = [script],
        arguments = args,
        mnemonic = "FactorilogRender",
        use_default_shell_env = True,
        execution_requirements = {
            "no-sandbox": "1",
            "local": "1",
        },
    )

    return [DefaultInfo(files = depset(outputs))]

factorilog_render = rule(
    implementation = _factorilog_render_impl,
    attrs = {
        "blueprint": attr.label(allow_single_file = [".blueprint", ".txt"], mandatory = True, doc = "Blueprint exchange string file"),
        "png": attr.output(mandatory = True, doc = "Output rendered PNG image"),
        "time_json": attr.output(doc = "Optional output render timing JSON file"),
        "_render_bp": attr.label(
            default = Label("//foundry:render_bp"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Renders Factorio blueprint string to a PNG image.",
)


def _factorilog_save_impl(ctx):
    blueprint = ctx.file.blueprint
    save_zip = ctx.outputs.save_zip
    time_json = ctx.outputs.time_json
    pins_json = ctx.file.pins_json
    netlist_json = ctx.file.netlist_json
    def_file = ctx.file.def_file
    script = ctx.executable._bp2save

    inputs = [blueprint]
    outputs = [save_zip]
    args = [blueprint.path, "--out", save_zip.path]

    if pins_json:
        inputs.append(pins_json)
        args.extend(["--pins", pins_json.path])
    if netlist_json:
        inputs.append(netlist_json)
        args.extend(["--netlist", netlist_json.path])
    if def_file:
        inputs.append(def_file)
        args.extend(["--def", def_file.path])
    if time_json:
        outputs.append(time_json)
        args.extend(["--time-out", time_json.path])

    ctx.actions.run(
        inputs = inputs,
        outputs = outputs,
        executable = script,
        tools = [script],
        arguments = args,
        mnemonic = "FactorilogSave",
        use_default_shell_env = True,
        execution_requirements = {
            "no-sandbox": "1",
            "local": "1",
        },
    )

    return [DefaultInfo(files = depset(outputs))]

factorilog_save = rule(
    implementation = _factorilog_save_impl,
    attrs = {
        "blueprint": attr.label(allow_single_file = [".blueprint", ".txt"], mandatory = True, doc = "Blueprint exchange string file"),
        "def_file": attr.label(allow_single_file = [".def"], mandatory = False, doc = "DEF file"),
        "netlist_json": attr.label(allow_single_file = [".json"], mandatory = False, doc = "Netlist JSON file"),
        "pins_json": attr.label(allow_single_file = [".json"], mandatory = False, doc = "Pins JSON file"),
        "save_zip": attr.output(mandatory = True, doc = "Output playable Factorio save file (.zip)"),
        "time_json": attr.output(doc = "Optional output save generation timing JSON file"),
        "_bp2save": attr.label(
            default = Label("//foundry:bp2save"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Converts a Factorio blueprint exchange string into a playable Factorio save file.",
)



def _factorilog_timing_summary_impl(ctx):
    summary_out = ctx.outputs.summary_json
    factory_name = ctx.attr.factory_name
    time_files = ctx.files.time_files

    cmd = """set -euo pipefail
python3 - '%s' '%s' %s << 'PYEOF'
import json, sys
from pathlib import Path

target = sys.argv[1]
summary_file = Path(sys.argv[2])
file_paths = sys.argv[3:]

stages = {}
total = 0.0

for fp in file_paths:
    p = Path(fp)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            stage = data.get('stage', p.stem)
            sec = float(data.get('elapsed_seconds', 0.0))
            stages[stage] = round(sec, 3)
            total += sec
        except Exception as err:
            sys.stderr.write(f'Warning: failed to read {fp}: {err}\\n')

summary = {
    'target': target,
    'stages': stages,
    'total_seconds': round(total, 3),
}
summary_file.parent.mkdir(parents=True, exist_ok=True)
summary_file.write_text(json.dumps(summary, indent=2) + '\\n', encoding='utf-8')
stage_strs = [f'{k}={v:.2f}s' for k, v in stages.items()]
print(f'factorilog [{target}]: ' + ' '.join(stage_strs) + f' total={total:.2f}s')
PYEOF
""" % (factory_name, summary_out.path, " ".join(["'%s'" % f.path for f in time_files]))

    ctx.actions.run_shell(
        inputs = time_files,
        outputs = [summary_out],
        command = cmd,
        mnemonic = "FactorilogTimingSummary",
        execution_requirements = {
            "no-sandbox": "1",
            "local": "1",
        },
    )

    return [DefaultInfo(files = depset([summary_out]))]

factorilog_timing_summary = rule(
    implementation = _factorilog_timing_summary_impl,
    attrs = {
        "factory_name": attr.string(mandatory = True, doc = "Name of factory design"),
        "time_files": attr.label_list(allow_files = [".json"], mandatory = True, doc = "Input timing JSON files"),
        "summary_json": attr.output(mandatory = True, doc = "Aggregated timing summary JSON output"),
    },
    doc = "Aggregates execution time measurements across flow stages.",
)


def _factorilog_factory_test_impl(ctx):
    runner = ctx.executable._test_runner
    save_zip = ctx.file.save_zip
    expected_item = ctx.attr.expected_item
    min_produced = ctx.attr.min_produced
    ticks = ctx.attr.ticks
    min_active_ratio = ctx.attr.min_active_ratio
    max_area = ctx.attr.max_area
    min_slack = ctx.attr.min_slack
    def_file = ctx.file.def_file
    sta_rpt = ctx.file.sta_rpt
    drc_rpt = ctx.file.drc_rpt

    inputs = [save_zip]
    extra_flags = []
    if def_file:
        inputs.append(def_file)
        extra_flags.append('--def-file "${PREFIX}%s"' % def_file.short_path)
    if max_area and max_area != "0.0":
        extra_flags.append("--max-area %s" % max_area)
    if sta_rpt:
        inputs.append(sta_rpt)
        extra_flags.append('--sta-rpt "${PREFIX}%s"' % sta_rpt.short_path)
        extra_flags.append("--min-slack %s" % min_slack)
    if drc_rpt:
        inputs.append(drc_rpt)
        extra_flags.append('--drc-rpt "${PREFIX}%s"' % drc_rpt.short_path)

    extra_str = " ".join(extra_flags)

    script = """#!/bin/bash
set -euo pipefail
RUNFILES="${PYTHON_RUNFILES:-${TEST_SRCDIR:-}}"
PREFIX=""
if [ -n "$RUNFILES" ] && [ -d "$RUNFILES/_main" ]; then
    PREFIX="$RUNFILES/_main/"
elif [ -d "${0}.runfiles/_main" ]; then
    PREFIX="${0}.runfiles/_main/"
fi

RUNNER="${PREFIX}%s"
SAVE="${PREFIX}%s"

exec "$RUNNER" --save "$SAVE" --expected-item "%s" --min-produced %d --ticks %d --min-active-ratio %s %s "$@"
""" % (
        runner.short_path,
        save_zip.short_path,
        expected_item,
        min_produced,
        ticks,
        min_active_ratio,
        extra_str,
    )

    ctx.actions.write(
        output = ctx.outputs.executable,
        content = script,
        is_executable = True,
    )

    runfiles = ctx.runfiles(
        files = inputs,
        transitive_files = ctx.attr._test_runner[DefaultInfo].default_runfiles.files,
    )
    return [DefaultInfo(executable = ctx.outputs.executable, runfiles = runfiles)]

factorilog_factory_test = rule(
    implementation = _factorilog_factory_test_impl,
    test = True,
    attrs = {
        "save_zip": attr.label(allow_single_file = [".zip"], mandatory = True, doc = "Playable Factorio save file (.zip)"),
        "expected_item": attr.string(default = "automation-science-pack", doc = "Expected science pack or item name"),
        "min_produced": attr.int(default = 20, doc = "Minimum produced item count"),
        "ticks": attr.int(default = 7200, doc = "Simulation ticks"),
        "min_active_ratio": attr.string(default = "0.70", doc = "Minimum machine activity ratio"),
        "def_file": attr.label(allow_single_file = [".def"], doc = "Routed DEF file"),
        "max_area": attr.string(default = "0.0", doc = "Maximum allowed tile area"),
        "sta_rpt": attr.label(allow_single_file = [".rpt"], doc = "STA timing report"),
        "min_slack": attr.string(default = "0.0", doc = "Minimum allowed timing slack"),
        "drc_rpt": attr.label(allow_single_file = [".rpt"], doc = "Routing DRC report"),
        "_test_runner": attr.label(
            default = Label("//flow:test_factory"),
            executable = True,
            cfg = "target",
        ),
    },
    doc = "Runs automated regression tests on physical factory throughput and metrics.",
)


def factorilog_science_factory(
        name,
        demand,
        dump = "//test:data/dump-fixture.json",
        block_fill = "0.08",
        cell_pad_sites = 4,
        density = "0.18",
        core_margin = 4,
        split_pad_sites = 5,
        routability_driven = True,
        layer_adjustment = "",
        use_row_placement = False,
        channel_h = 12,
        use_chassis = False,
        chassis_grid_w = 0,
        chassis_grid_h = 0,
        chassis_pitch = 100,
        chassis_core_w = 94,
        chassis_core_h = 94,
        chassis_channel_w = 6,
        do_untangle = True,
        enable_display_readouts = False,
        enable_circuit_readouts = False,
        enable_compound_cells = False,
        synthesize_splitters = True,
        telemetry_taps = "none",
        controller_verilog = None,
        strict_drc = True,
        skip_router = True,
        use_maze_router = True,
        test_expected_item = None,
        test_min_produced = 20,
        test_ticks = 7200,
        test_min_active_ratio = "0.70",
        tags = None):
    """Builds full physical design flow for a science pack factory."""
    merged_tags = list(tags) if tags else []

    synth_name = name + "_synth"
    pnr_name = name + "_pnr"
    bp_name = name + "_bp"
    render_name = name + "_render"
    save_name = name + "_save"
    timing_name = name + "_timing"

    factorilog_synth(
        name = synth_name,
        cells_v = name + "_cells.v",
        netlist_v = name + "_netlist.v",
        netlist_json = name + "_netlist.json",
        tech_lef = name + "_tech.lef",
        tech_lib = name + "_tech.lib",
        ranks_tcl = name + "_ranks.tcl",
        time_json = name + "_synth_time.json",
        dump = dump,
        demand = demand,
        enable_display_readouts = enable_display_readouts,
        enable_circuit_readouts = enable_circuit_readouts,
        enable_compound_cells = enable_compound_cells,
        synthesize_splitters = synthesize_splitters,
        telemetry_taps = telemetry_taps,
        controller_verilog = controller_verilog,
        tags = merged_tags,
    )

    factorilog_pnr(
        name = pnr_name,
        netlist_v = ":" + name + "_netlist.v",
        tech_lef = ":" + name + "_tech.lef",
        tech_lib = ":" + name + "_tech.lib",
        ranks_tcl = ":" + name + "_ranks.tcl",
        def_file = name + ".def",
        route_drc = name + "_route_drc.rpt",
        timing_rpt = name + "_sta.rpt",
        log_file = name + "_pnr.log",
        time_json = name + "_pnr_time.json",
        block_fill = block_fill,
        cell_pad_sites = cell_pad_sites,
        split_pad_sites = split_pad_sites,
        density = density,
        core_margin = core_margin,
        routability_driven = routability_driven,
        layer_adjustment = layer_adjustment,
        use_row_placement = use_row_placement,
        channel_h = channel_h,
        use_chassis = use_chassis,
        chassis_grid_w = chassis_grid_w,
        chassis_grid_h = chassis_grid_h,
        chassis_pitch = chassis_pitch,
        chassis_core_w = chassis_core_w,
        chassis_core_h = chassis_core_h,
        chassis_channel_w = chassis_channel_w,
        do_untangle = do_untangle,
        skip_router = skip_router,
        tags = merged_tags,
    )

    factorilog_blueprint(
        name = bp_name,
        def_file = ":" + name + ".def",
        netlist_json = ":" + name + "_netlist.json",
        blueprint = name + ".blueprint",
        pins_json = name + "_pins.json",
        time_json = name + "_bp_time.json",
        label_text = name,
        strict_drc = strict_drc,
        use_maze_router = use_maze_router,
        tags = merged_tags,
    )

    factorilog_render(
        name = render_name,
        blueprint = ":" + name + ".blueprint",
        png = name + ".png",
        time_json = name + "_render_time.json",
        tags = merged_tags,
    )

    factorilog_save(
        name = save_name,
        blueprint = ":" + name + ".blueprint",
        def_file = ":" + name + ".def",
        netlist_json = ":" + name + "_netlist.json",
        pins_json = ":" + name + "_pins.json",
        save_zip = name + ".zip",
        time_json = name + "_save_time.json",
        tags = merged_tags,
    )

    factorilog_timing_summary(
        name = timing_name,
        factory_name = name,
        time_files = [
            ":" + name + "_synth_time.json",
            ":" + name + "_pnr_time.json",
            ":" + name + "_bp_time.json",
            ":" + name + "_render_time.json",
            ":" + name + "_save_time.json",
        ],
        summary_json = name + "_timing.json",
        tags = merged_tags,
    )

    native.filegroup(
        name = name,
        srcs = [
            ":" + name + ".blueprint",
            ":" + name + ".png",
            ":" + name + ".zip",
            ":" + name + "_timing.json",
        ],
        tags = merged_tags,
    )

    test_name = name + "_test"
    demand_tokens = demand.split(" ")
    primary_item = test_expected_item if test_expected_item else (demand_tokens[0] if demand_tokens else "automation-science-pack")
    test_tags = list(merged_tags)
    for t in ("local", "no-sandbox", "manual"):
        if t not in test_tags:
            test_tags.append(t)

    factorilog_factory_test(
        name = test_name,
        save_zip = ":" + name + ".zip",
        expected_item = primary_item,
        min_produced = test_min_produced,
        ticks = test_ticks,
        min_active_ratio = test_min_active_ratio,
        def_file = ":" + name + ".def",
        drc_rpt = ":" + name + "_route_drc.rpt",
        sta_rpt = ":" + name + "_sta.rpt",
        tags = test_tags,
    )
