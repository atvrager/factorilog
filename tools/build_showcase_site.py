"""CLI builder for the factorilog static showcase site and committed visuals.

Generates compressed WebP renders, native cropped sprite details, deterministic
vector SVGs, compact die geometry JSON, benchmarks JSON, and `site/index.html`.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import os
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PIL import Image

from foundry.blueprint import decode, encode
from foundry.circuit_synth import synthesize_power_sequencer
from foundry.def_parser import parse_def
from foundry.entity_helpers import DIRECTION_NORTH, entity_size
from foundry.render_bp import render_blueprint
from tools.showcase_data import (
    build_die_payload,
    collect_cpu_provenance,
    collect_git_provenance,
    collect_target_benchmarks,
    load_prototype_catalog,
    select_street_straddle_window,
)
from tools.showcase_svg import (
    build_lp_dag_treemap_svg,
    build_placement_comparison_svg,
    build_router_3d_svg,
    build_verilog_campaign_svg,
)
from tools.showcase_template import render_showcase_html

CROP_WIN_W = 56
CROP_WIN_H = 36
CROP_PAD_TILES = 6.0
RENDER_MARGIN_TILES = 3.0
RENDER_BASE_TILE_PX = 32
RENDER_MIN_TILE_PX = 8
RENDER_MAX_CANVAS_DIM = 8192
MAX_RENDER_PIXELS = 150_000_000
WEBP_MAX_DIM = 4096
WEBP_QUALITY_DIE = 82
WEBP_QUALITY_DETAIL = 88
WEBP_METHOD = 4
TAR_FILE_MODE = 0o644

Image.MAX_IMAGE_PIXELS = MAX_RENDER_PIXELS

BLUEPRINT_EXPORTS: tuple[str, ...] = (
    "science_chassis",
    "automation_controlled_science",
    "automation_logistic_chassis",
)


def _convert_png_to_webp(
    src_png: Path,
    dst_webp: Path,
    *,
    quality: int,
    max_dim: int = WEBP_MAX_DIM,
) -> None:
    """Convert a rendered RGBA PNG file into an optimized WebP image."""
    if not src_png.is_file():
        raise FileNotFoundError(f"Missing rendered PNG: {src_png}")
    dst_webp.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src_png) as img:
        work: Image.Image = img
        if max(img.width, img.height) > max_dim:
            work = img.copy()
            work.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        work.save(dst_webp, format="WEBP", quality=quality, method=WEBP_METHOD)


def _sub_blueprint_crop_to_webp(
    bp_doc: Mapping[str, Any],
    cat_map: Mapping[str, str],
    dst_webp: Path,
) -> None:
    """Extract a street-straddling 56x36 tile sub-blueprint and render at native 32 px/tile."""
    bp_payload = bp_doc.get("blueprint", bp_doc)
    wx0, wy0, wx1, wy1 = select_street_straddle_window(
        bp_payload,
        cat_map,
        CROP_WIN_W,
        CROP_WIN_H,
        prefer_pipes=False,
    )
    pad_x0, pad_y0 = wx0 - CROP_PAD_TILES, wy0 - CROP_PAD_TILES
    pad_x1, pad_y1 = wx1 + CROP_PAD_TILES, wy1 + CROP_PAD_TILES

    sub_entities: list[dict[str, Any]] = []
    kept_ids: set[int] = set()
    for ent in bp_payload.get("entities", ()):
        if not isinstance(ent, Mapping):
            continue
        pos = ent.get("position", {})
        if not isinstance(pos, Mapping):
            continue
        ex, ey = float(pos.get("x", 0.0)), float(pos.get("y", 0.0))
        if pad_x0 <= ex <= pad_x1 and pad_y0 <= ey <= pad_y1:
            sub_entities.append(dict(ent))
            if "entity_number" in ent:
                kept_ids.add(int(ent["entity_number"]))

    sub_tiles: list[dict[str, Any]] = []
    for tile in bp_payload.get("tiles", ()):
        if not isinstance(tile, Mapping):
            continue
        pos = tile.get("position", {})
        if not isinstance(pos, Mapping):
            continue
        tx, ty = float(pos.get("x", 0.0)), float(pos.get("y", 0.0))
        if pad_x0 <= tx <= pad_x1 and pad_y0 <= ty <= pad_y1:
            sub_tiles.append(dict(tile))

    sub_wires: list[list[int]] = [
        [int(w[0]), int(w[1]), int(w[2]), int(w[3])]
        for w in bp_payload.get("wires", ())
        if isinstance(w, list) and len(w) >= 4 and int(w[0]) in kept_ids and int(w[2]) in kept_ids
    ]

    sub_bp_str = encode(
        sub_entities,
        "science_chassis_crop",
        wires=sub_wires or None,
        tiles=sub_tiles or None,
    )

    vp_min_x, vp_min_y, _vp_w, _vp_h, tile_px = _predict_render_viewport(sub_entities, sub_tiles)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_png = Path(tmpdir) / "crop.png"
        render_blueprint(sub_bp_str, tmp_png)
        with Image.open(tmp_png) as img:
            left = max(0, round((wx0 - vp_min_x) * tile_px))
            top = max(0, round((wy0 - vp_min_y) * tile_px))
            right = min(img.width, round((wx1 - vp_min_x) * tile_px))
            bottom = min(img.height, round((wy1 - vp_min_y) * tile_px))
            cropped = img.crop((left, top, right, bottom))
            dst_webp.parent.mkdir(parents=True, exist_ok=True)
            cropped.save(dst_webp, format="WEBP", quality=WEBP_QUALITY_DETAIL, method=WEBP_METHOD)


def _predict_render_viewport(
    entities: list[dict[str, Any]],
    tiles: list[dict[str, Any]],
) -> tuple[float, float, float, float, int]:
    """Mirror `render_bp._compute_viewport` bounds and pixel scale for exact cropping."""
    min_x = math.inf
    min_y = math.inf
    max_x = -math.inf
    max_y = -math.inf

    for ent in entities:
        pos = ent["position"]
        ex, ey = float(pos["x"]), float(pos["y"])
        w, h = entity_size(str(ent["name"]), int(ent.get("direction", DIRECTION_NORTH)))
        min_x = min(min_x, ex - w * 0.5)
        min_y = min(min_y, ey - h * 0.5)
        max_x = max(max_x, ex + w * 0.5)
        max_y = max(max_y, ey + h * 0.5)

    for tile in tiles:
        pos = tile["position"]
        tx, ty = float(pos["x"]), float(pos["y"])
        min_x = min(min_x, tx)
        min_y = min(min_y, ty)
        max_x = max(max_x, tx + 1.0)
        max_y = max(max_y, ty + 1.0)

    min_x = math.floor(min_x - RENDER_MARGIN_TILES)
    min_y = math.floor(min_y - RENDER_MARGIN_TILES)
    max_x = math.ceil(max_x + RENDER_MARGIN_TILES)
    max_y = math.ceil(max_y + RENDER_MARGIN_TILES)
    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)
    max_span = max(span_x, span_y, 1.0)
    tile_px = RENDER_BASE_TILE_PX
    if max_span * tile_px > RENDER_MAX_CANVAS_DIM:
        tile_px = max(RENDER_MIN_TILE_PX, int(RENDER_MAX_CANVAS_DIM // max_span))
    return float(min_x), float(min_y), float(span_x), float(span_y), tile_px


def build_showcase_site(workspace: Path) -> None:
    """Build all showcase visuals, data exports, blueprints, and `site/index.html`."""
    flow_bin = workspace / "bazel-bin" / "flow"
    starter_dir = workspace / "bazel-testlogs" / "test" / "patch_router_test" / "test.outputs"
    dump_path = workspace / "test" / "data" / "dump-fixture.json"
    site_dir = workspace / "site"
    assets_dir = site_dir / "assets"
    data_dir = site_dir / "data"
    bp_dir = data_dir / "blueprints"

    assets_dir.mkdir(parents=True, exist_ok=True)
    bp_dir.mkdir(parents=True, exist_ok=True)

    _prototypes, cat_map, ug_spans = load_prototype_catalog(dump_path)

    # 1. Convert raster sprite PNG renders to compressed WebP
    _convert_png_to_webp(
        flow_bin / "science_chassis.png",
        assets_dir / "science_chassis.webp",
        quality=WEBP_QUALITY_DIE,
    )
    _convert_png_to_webp(
        flow_bin / "automation_controlled_science.png",
        assets_dir / "automation_controlled_science.webp",
        quality=WEBP_QUALITY_DETAIL,
    )
    _convert_png_to_webp(
        starter_dir / "starter_base.png",
        assets_dir / "starter_base.webp",
        quality=WEBP_QUALITY_DETAIL,
    )

    # 2. Decode science_chassis blueprint and extract native 32 px/tile street crop + die JSON
    science_bp_str = (flow_bin / "science_chassis.blueprint").read_text(encoding="utf-8").strip()
    science_bp_doc = decode(science_bp_str)
    _sub_blueprint_crop_to_webp(science_bp_doc, cat_map, assets_dir / "science_chassis_crop.webp")

    science_def = parse_def((flow_bin / "science_chassis.def").read_text(encoding="utf-8"))
    science_netlist = json.loads(
        (flow_bin / "science_chassis_netlist.json").read_text(encoding="utf-8")
    )

    science_bp_payload = science_bp_doc.get("blueprint", science_bp_doc)
    die_payload = build_die_payload(
        science_def,
        science_netlist,
        science_bp_payload,
        cat_map,
    )
    bp_ents = [dict(e) for e in science_bp_payload.get("entities", ()) if isinstance(e, Mapping)]
    bp_tiles = [dict(t) for t in science_bp_payload.get("tiles", ()) if isinstance(t, Mapping)]
    sp_x, sp_y, sp_w, sp_h, _ = _predict_render_viewport(bp_ents, bp_tiles)
    die_payload["die"]["sprite_rect"] = [sp_x, sp_y, sp_w, sp_h]
    (data_dir / "die_science_chassis.json").write_text(
        json.dumps(die_payload, separators=(",", ":")),
        encoding="utf-8",
    )

    # 3. Generate deterministic pipeline vector SVGs from compiled flow artifacts
    refresh_pipeline_svgs(
        workspace,
        cat_map=cat_map,
        ug_spans=ug_spans,
        science_netlist=science_netlist,
    )

    # 4. Copy downloadable blueprint exchange strings
    for bp_target in BLUEPRINT_EXPORTS:
        shutil.copyfile(flow_bin / f"{bp_target}.blueprint", bp_dir / f"{bp_target}.blueprint")
    shutil.copyfile(starter_dir / "starter_base.blueprint", bp_dir / "starter_base.blueprint")

    # 5. Export benchmarks JSON with git + host CPU/thread provenance and render site/index.html
    provenance = {**collect_git_provenance(workspace), **collect_cpu_provenance()}
    targets = collect_target_benchmarks(flow_bin, decode)
    benchmarks_doc = {
        "provenance": provenance,
        "targets": targets,
    }
    (data_dir / "benchmarks.json").write_text(
        json.dumps(benchmarks_doc, indent=2) + "\n",
        encoding="utf-8",
    )
    (site_dir / "index.html").write_text(
        render_showcase_html(benchmarks_doc),
        encoding="utf-8",
    )


def _add_bytes_to_tar(tf: tarfile.TarFile, arcname: str, payload: bytes) -> None:
    """Write an in-memory payload into a deterministic tar archive entry."""
    info = tarfile.TarInfo(name=arcname)
    info.size = len(payload)
    info.mtime = 0
    info.mode = TAR_FILE_MODE
    tf.addfile(info, io.BytesIO(payload))


def refresh_pipeline_svgs(
    workspace: Path,
    *,
    cat_map: Mapping[str, str] | None = None,
    ug_spans: Mapping[str, int] | None = None,
    science_netlist: Mapping[str, Any] | None = None,
) -> None:
    """Regenerate all four pipeline SVGs without changing WebPs or benchmark provenance."""
    flow_bin = workspace / "bazel-bin" / "flow"
    dump_path = workspace / "test" / "data" / "dump-fixture.json"
    assets_dir = workspace / "site" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    if cat_map is None or ug_spans is None:
        _prototypes, loaded_cats, loaded_spans = load_prototype_catalog(dump_path)
        cat_map = cat_map or loaded_cats
        ug_spans = ug_spans or loaded_spans

    if science_netlist is None:
        science_netlist = json.loads(
            (flow_bin / "science_chassis_netlist.json").read_text(encoding="utf-8")
        )

    logistic_netlist = json.loads(
        (flow_bin / "logistic_science_netlist.json").read_text(encoding="utf-8")
    )
    (assets_dir / "pipeline_lp_dag.svg").write_text(
        build_lp_dag_treemap_svg(logistic_netlist, science_netlist),
        encoding="utf-8",
    )

    chem_def = parse_def(
        (flow_bin / "automation_logistic_military_chemical_chassis.def").read_text(encoding="utf-8")
    )
    chem_netlist = json.loads(
        (flow_bin / "automation_logistic_military_chemical_chassis_netlist.json").read_text(
            encoding="utf-8"
        )
    )
    (assets_dir / "pipeline_placement.svg").write_text(
        build_placement_comparison_svg(chem_def, chem_netlist),
        encoding="utf-8",
    )

    router_target = "automation_logistic_military_chemical_chassis"
    if not (flow_bin / f"{router_target}.blueprint").is_file():
        router_target = "automation_logistic_chassis"
    router_def = parse_def((flow_bin / f"{router_target}.def").read_text(encoding="utf-8"))
    router_bp_doc = decode(
        (flow_bin / f"{router_target}.blueprint").read_text(encoding="utf-8").strip()
    )
    netlist_spans = {
        str(k): int(v) for k, v in science_netlist.get("underground_spans", ug_spans).items()
    }
    (assets_dir / "pipeline_router_3d.svg").write_text(
        build_router_3d_svg(
            router_def,
            router_bp_doc.get("blueprint", router_bp_doc),
            cat_map,
            netlist_spans,
        ),
        encoding="utf-8",
    )

    verilog_path = workspace / "flow" / "controllers" / "power_sequencer.v"
    (assets_dir / "pipeline_verilog_campaign.svg").write_text(
        _synthesize_fresh_verilog_svg(verilog_path),
        encoding="utf-8",
    )


def _synthesize_fresh_verilog_svg(verilog_path: Path) -> str:
    """Synthesize the power sequencer controller SVG from Verilog source."""
    verilog_src = verilog_path.read_text(encoding="utf-8")
    synth_res = synthesize_power_sequencer()
    return build_verilog_campaign_svg(synth_res, verilog_src)


def refresh_hermetic_site(workspace: Path, *, stamp_git: bool = False) -> None:
    """Regenerate `index.html` and the Verilog SVG without full factory builds."""
    site_dir = workspace / "site"
    data_dir = site_dir / "data"
    benchmarks_path = data_dir / "benchmarks.json"
    benchmarks_doc = json.loads(benchmarks_path.read_text(encoding="utf-8"))
    if stamp_git:
        benchmarks_doc["provenance"] = {
            **collect_git_provenance(workspace),
            **collect_cpu_provenance(),
        }
        benchmarks_path.write_text(
            json.dumps(benchmarks_doc, indent=2) + "\n",
            encoding="utf-8",
        )

    verilog_path = workspace / "flow" / "controllers" / "power_sequencer.v"
    (site_dir / "assets" / "pipeline_verilog_campaign.svg").write_text(
        _synthesize_fresh_verilog_svg(verilog_path),
        encoding="utf-8",
    )
    (site_dir / "index.html").write_text(
        render_showcase_html(benchmarks_doc),
        encoding="utf-8",
    )


def build_pages_bundle_tar(site_dir: Path, verilog_path: Path, out_tar: Path) -> None:
    """Package `site/` into a deterministic `.tar.gz` with fresh generated pages."""
    benchmarks_path = site_dir / "data" / "benchmarks.json"
    benchmarks_doc = json.loads(benchmarks_path.read_text(encoding="utf-8"))
    entries: dict[str, bytes] = {}
    for src_path in sorted(site_dir.rglob("*")):
        if not src_path.is_file():
            continue
        if src_path.name == "BUILD.bazel" or src_path.name.endswith(".tar.gz"):
            continue
        rel_name = src_path.relative_to(site_dir).as_posix()
        entries[rel_name] = src_path.read_bytes()

    entries["assets/pipeline_verilog_campaign.svg"] = _synthesize_fresh_verilog_svg(
        verilog_path
    ).encode("utf-8")
    entries["index.html"] = render_showcase_html(benchmarks_doc).encode("utf-8")

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        for arcname in sorted(entries):
            _add_bytes_to_tar(tf, arcname, entries[arcname])

    out_tar.parent.mkdir(parents=True, exist_ok=True)
    out_tar.write_bytes(gzip.compress(tar_buf.getvalue(), mtime=0))


def _resolve_workspace(explicit: str | None) -> Path:
    """Resolve factorilog repository root directory."""
    if explicit:
        return Path(explicit).resolve()
    env_ws = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env_ws:
        return Path(env_ws).resolve()
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point for building showcase visuals and GitHub Pages output."""
    parser = argparse.ArgumentParser(description="Build factorilog showcase site and visuals.")
    parser.add_argument(
        "--workspace",
        help="Path to the factorilog repository root (defaults to BUILD_WORKSPACE_DIRECTORY).",
    )
    parser.add_argument(
        "--package-tar",
        type=Path,
        help="Write a deterministic .tar.gz bundle of the showcase site.",
    )
    parser.add_argument(
        "--site-dir",
        type=Path,
        help="Input site directory used with --package-tar.",
    )
    parser.add_argument(
        "--verilog",
        type=Path,
        help="Path to power_sequencer.v used with --package-tar.",
    )
    parser.add_argument(
        "--refresh-hermetic",
        action="store_true",
        help="Regenerate index.html and the Verilog SVG without full builds.",
    )
    parser.add_argument(
        "--refresh-svgs",
        action="store_true",
        help="Regenerate pipeline SVGs without changing WebPs or benchmarks.json.",
    )
    parser.add_argument(
        "--stamp-git",
        action="store_true",
        help="Refresh commit and CPU provenance during --refresh-hermetic.",
    )
    args = parser.parse_args(argv)
    if args.package_tar is not None:
        site_dir = args.site_dir
        verilog_path = args.verilog
        if site_dir is None or verilog_path is None:
            workspace = _resolve_workspace(args.workspace)
            site_dir = site_dir or (workspace / "site")
            verilog_path = verilog_path or (
                workspace / "flow" / "controllers" / "power_sequencer.v"
            )
        build_pages_bundle_tar(site_dir, verilog_path, args.package_tar)
        print(f"factorilog: pages bundle written to {args.package_tar}")
        return 0

    workspace = _resolve_workspace(args.workspace)
    if args.refresh_svgs:
        refresh_pipeline_svgs(workspace)
        print(f"factorilog: pipeline SVGs refreshed in {workspace / 'site' / 'assets'}")
        return 0
    if args.refresh_hermetic:
        refresh_hermetic_site(workspace, stamp_git=args.stamp_git)
        print(f"factorilog: hermetic site refreshed in {workspace / 'site'}")
        return 0

    build_showcase_site(workspace)
    print(f"factorilog: showcase site written to {workspace / 'site'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
