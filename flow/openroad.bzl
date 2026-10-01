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

    args = ctx.actions.args()
    args.add(ctx.file.dump.path)
    args.add("--spc")
    args.add(str(ctx.attr.spc))

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

    flags_str = " ".join(extra_flags)

    cmd = (
        "set -euo pipefail\n" +
        "OUT_DIR='%s'\n" % out_dir +
        "mkdir -p \"$OUT_DIR\"\n" +
        "DUMP_ABS=\"$(pwd)/%s\"\n" % ctx.file.dump.path +
        "NETBUILD_ABS=\"$(pwd)/%s\"\n" % script.path +
        "cd \"$OUT_DIR\"\n" +
        "\"$NETBUILD_ABS\" \"$DUMP_ABS\" --spc %s %s\n"
    ) % (ctx.attr.spc, flags_str)

    ctx.actions.run_shell(
        inputs = [ctx.file.dump],
        outputs = outputs,
        command = cmd,
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
        "spc": attr.string(default = "60.0", doc = "Science packs per minute rate"),
        "telemetry_taps": attr.string(default = "none", doc = "Telemetry tap scope: none, io, merges, io,merges, all"),
        "enable_display_readouts": attr.bool(default = True, doc = "Enable display panel readouts"),
        "enable_circuit_readouts": attr.bool(default = False, doc = "Enable arithmetic combinator readouts"),
        "cells_v": attr.output(mandatory = True),
        "netlist_v": attr.output(mandatory = True),
        "netlist_json": attr.output(mandatory = True),
        "tech_lef": attr.output(mandatory = True),
        "tech_lib": attr.output(mandatory = True),
        "_netbuild": attr.label(
            default = Label("//foundry:netbuild"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Synthesizes Verilog netlist, LEF, and Liberty from Factorio prototype dump.",
)


def _factorilog_blueprint_impl(ctx):
    out_bp = ctx.outputs.blueprint
    def_file = ctx.file.def_file
    netlist_json = ctx.file.netlist_json
    label = ctx.attr.label_text

    ctx.actions.run(
        inputs = [def_file, netlist_json],
        outputs = [out_bp],
        executable = ctx.executable._def2bp,
        arguments = [
            def_file.path,
            netlist_json.path,
            "-o",
            out_bp.path,
            "--label",
            label,
        ],
        mnemonic = "FactorilogBlueprint",
    )

    return [DefaultInfo(files = depset([out_bp]))]

factorilog_blueprint = rule(
    implementation = _factorilog_blueprint_impl,
    attrs = {
        "def_file": attr.label(allow_single_file = [".def"], mandatory = True, doc = "Placed and routed DEF file"),
        "netlist_json": attr.label(allow_single_file = [".json"], mandatory = True, doc = "Netlist sidecar metadata"),
        "label_text": attr.string(default = "factory", doc = "Blueprint label"),
        "blueprint": attr.output(mandatory = True, doc = "Output blueprint file"),
        "_def2bp": attr.label(
            default = Label("//foundry:def2bp"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Converts placed DEF geometry and netlist sidecar into a blueprint file.",
)
