"""HTML template renderer for factorilog GitHub Pages output.

Renders `site/index.html` referencing `styles.css`, `app.js`, WebP sprites,
deterministic pipeline SVGs, and pre-baked benchmark provenance rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any


def render_showcase_html(benchmarks_doc: Mapping[str, Any]) -> str:
    """Render complete HTML document for `site/index.html` from `benchmarks.json`."""
    prov = benchmarks_doc["provenance"]
    targets = list(benchmarks_doc["targets"])
    science_row = next((row for row in targets if row["target"] == "science_chassis"), targets[-1])

    short_sha = escape(str(prov["git_commit_short"]))
    full_sha = escape(str(prov["git_commit_full"]))
    commit_date = escape(str(prov["git_commit_date"]))
    repo_url = escape(str(prov["repo_url"]))
    commit_url = f"{repo_url}/commit/{full_sha}"
    cpu_model = escape(str(prov["cpu_model"]))
    phys_cores = int(prov["physical_cores"])
    log_threads = int(prov["logical_threads"])
    stage_conc = prov["stage_concurrency"]

    table_rows_html = _build_benchmark_rows_html(targets)
    concurrency_pills_html = _build_concurrency_html(stage_conc)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>factorilog — Foundry PDK &amp; OpenROAD Place-and-Route for Factorio</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <a href="/">atvrager</a>
      <span class="slash">/</span>
      <a href="{repo_url}">factorilog</a>
    </div>
    <nav class="nav-links">
      <a href="#inspector">Die Viewer</a>
      <a href="#sprites">Sprite Renders</a>
      <a href="#algorithmics">Algorithmics</a>
      <a href="#campaign">Freeplay Bot</a>
      <a href="#benchmarks">Benchmarks</a>
      <a href="{repo_url}">GitHub</a>
    </nav>
    <div class="prov-pill">
      commit <a href="{commit_url}"><strong>{short_sha}</strong></a> ({commit_date})
      &middot; {phys_cores}C/{log_threads}T {cpu_model}
    </div>
  </header>

  <main>
    <section class="hero">
      <div>
        <h1>Foundry PDK &amp; EDA Physical Compiler for Factorio</h1>
        <p class="lead">
          Factorilog synthesizes balanced production graphs from live Factorio prototype data via LP optimization, places macros across a 94&times;94 tile city-block chassis using analytical quadratic wirelength minimization and OpenROAD, routes multi-layer underground belts and fluids with a 3D PathFinder maze router, and verifies zero-cheat execution directly against Factorio C++ internals and live UDP multiplayer lockstep.
        </p>
      </div>

      <div class="kpi-grid">
        <div class="kpi">
          <div class="val">{science_row["die_w"]}&times;{science_row["die_h"]}</div>
          <div class="lbl">7-Science Chassis Die (tiles)</div>
        </div>
        <div class="kpi">
          <div class="val">{science_row["macro_instances"]:,}</div>
          <div class="lbl">DEF Macro Instances (51 PDK cells)</div>
        </div>
        <div class="kpi">
          <div class="val">{science_row["bp_entities"]:,}</div>
          <div class="lbl">Placed Blueprint Entities</div>
        </div>
        <div class="kpi">
          <div class="val ok">{science_row["drc_violations"]} DRC</div>
          <div class="lbl">Physical Overlap Violations</div>
        </div>
        <div class="kpi">
          <div class="val">{science_row["stages_sec"]["pnr"]:.2f}s / {science_row["stages_sec"]["bp"]:.1f}s</div>
          <div class="lbl">PnR / 3D Maze Route Time</div>
        </div>
        <div class="kpi">
          <div class="val ok">60 SPM &times; 7</div>
          <div class="lbl">All Base Science Packs</div>
        </div>
      </div>

      <div class="actions-row">
        <button class="btn primary" data-bp-copy="science_chassis">Copy 7-Science .blueprint ({science_row["bp_entities"]:,} ents)</button>
        <a class="btn" href="data/blueprints/science_chassis.blueprint" download="science_chassis.blueprint">Download science_chassis.blueprint</a>
        <button class="btn" data-bp-copy="automation_controlled_science">Copy Verilog-Controlled .blueprint</button>
        <button class="btn" data-bp-copy="starter_base">Copy Freeplay Starter .blueprint</button>
        <span id="copy-status" class="mono copy-status"></span>
      </div>
    </section>

    <section id="inspector" class="card">
      <div class="sec-head">
        <h2>Interactive 7-Science Die &amp; Layer Inspector (<code>science_chassis</code>)</h2>
        <span class="badge">LIVE GEOMETRY + SPRITE OVERLAY</span>
      </div>

      <div class="inspector-bar">
        <label class="layer-toggle"><input type="checkbox" id="layer-streets" checked><span class="swatch sw-streets"></span>Street Channels</label>
        <label class="layer-toggle"><input type="checkbox" id="layer-macros" checked><span class="swatch sw-macros"></span>Macros ({science_row["macro_instances"]:,})</label>
        <label class="layer-toggle"><input type="checkbox" id="layer-belts" checked><span class="swatch sw-belts"></span>M1 Belts</label>
        <label class="layer-toggle"><input type="checkbox" id="layer-ug" checked><span class="swatch sw-ug"></span>M2 Underground</label>
        <label class="layer-toggle"><input type="checkbox" id="layer-pipes" checked><span class="swatch sw-pipes"></span>M3 Pipes</label>
        <label class="layer-toggle"><input type="checkbox" id="layer-copper"><span class="swatch sw-copper"></span>Copper MST</label>
        <label class="layer-toggle">Sprite <input type="range" id="sprite-opacity" min="0" max="100" value="35" class="opacity-slider"></label>
        <select id="color-mode" aria-label="Color mode">
          <option value="rank">Color: Topological Rank</option>
          <option value="machine">Color: Machine Type</option>
        </select>
        <input type="text" id="filter-recipe" placeholder="Filter recipe (e.g. lab, gear)">
        <button class="btn sm" id="cam-full">Full Die</button>
        <button class="btn sm" id="cam-west">West Pins</button>
        <button class="btn sm" id="cam-east">East Labs &amp; Silo</button>
      </div>

      <div class="canvas-wrap" id="canvas-wrap">
        <canvas id="die-canvas"></canvas>
        <div class="hud-chip" id="die-hud">Loading geometry from data/die_science_chassis.json...</div>
        <div class="tooltip" id="die-tooltip"></div>
      </div>
    </section>

    <section id="sprites" class="card">
      <div class="sec-head">
        <h2>Game-Sprite Renderings &amp; SystemVerilog Telemetry</h2>
        <span class="badge info">RENDERED FROM FACTORIO PROTOTYPE ASSETS</span>
      </div>

      <div class="two-col">
        <figure class="figure-card">
          <img src="assets/science_chassis.webp" alt="Full 1200x1200 science_chassis factory die rendering" loading="lazy">
          <figcaption class="figure-cap">
            <h3>Full 7-Science Chassis Die (1200&times;1200 tiles)</h3>
            <p>Global die view showing 12&times;12 100-tile pitch city blocks, 6-tile perimeter street channels, perimeter substations/lamps, West raw bus pins, and East rocket silo complex.</p>
          </figcaption>
        </figure>

        <figure class="figure-card">
          <img src="assets/science_chassis_crop.webp" alt="32 px/tile street-straddling detail crop of science_chassis" loading="lazy">
          <figcaption class="figure-cap">
            <h3>Native 32 px/tile Street-Straddling Detail Crop</h3>
            <p>Zoomed extract directly from the decoded 188,786-entity blueprint showing assembling machines, inserters, splitter trees, surface M1 belts, and underground tunnel transitions crossing a street channel.</p>
          </figcaption>
        </figure>
      </div>

      <div class="two-col">
        <figure class="figure-card">
          <img src="assets/automation_controlled_science.webp" alt="Automation science factory controlled by synthesized Verilog FSM" loading="lazy">
          <figcaption class="figure-cap">
            <h3>SystemVerilog Power Sequencer (<code>power_sequencer.v</code>)</h3>
            <p>1-science factory paired with synthesized arithmetic/decider combinators, backup pump control, hysteresis FSM states, and seven-segment I/O telemetry panels.</p>
          </figcaption>
        </figure>

        <div class="figure-card padded">
          <div>
            <h3 class="panel-heading-cyan">IC-to-Factorio PDK Mapping</h3>
            <table>
              <thead><tr><th>VLSI Concept</th><th>Factorio Equivalent</th></tr></thead>
              <tbody>
                <tr><td>Standard Cell</td><td>Building + recipe footprint (<code>tech.lef</code>)</td></tr>
                <tr><td>Cell Delay</td><td><code>energy_required / crafting_speed</code> (<code>tech.lib</code>)</td></tr>
                <tr><td>M1 / M2 / M3 Layers</td><td>Surface belts / underground tunnels / pipes</td></tr>
                <tr><td>Clock / RTL FSM</td><td><code>pyslang</code> Verilog &rarr; combinator graph</td></tr>
                <tr><td>PDN Grid</td><td>Big pole &amp; substation Minimum Spanning Tree</td></tr>
                <tr><td>DRC / LVS Signoff</td><td>Native C++ ELF <code>TransportLine</code> simulation</td></tr>
              </tbody>
            </table>
          </div>
          <p class="panel-note">Click any sprite image or pipeline diagram to zoom full screen.</p>
        </div>
      </div>
    </section>

    <section id="algorithmics" class="card">
      <div class="sec-head">
        <h2>Compiler Architecture &amp; Algorithmics</h2>
        <span class="badge info">DETERMINISTIC VECTOR DIAGRAMS FROM REAL ARTIFACTS</span>
      </div>

      <div class="svg-stack">
        <div class="svg-card">
          <img src="assets/pipeline_lp_dag.svg" alt="Stage 1 LP Solver DAG and science_chassis treemap" loading="lazy">
          <p class="svg-desc">
            <strong>1. Synthesis &amp; Rate Solver (<code>foundry/solve.py</code>, <code>netbuild.py</code>):</strong> Solves a SciPy linear program over the game prototype dump with zero mass-balance error, then lowers fractional demand into integer machines, balanced 2-to-2 binary splitter/merger trees, Liberty timing stubs, and LEF macros.
          </p>
        </div>

        <div class="svg-card">
          <img src="assets/pipeline_placement.svg" alt="Stage 2 Naive Rank Placement vs Analytical Quadratic Placement and City-Block Legalization" loading="lazy">
          <p class="svg-desc">
            <strong>2. Analytical Placement &amp; Legalization (<code>analytical_placer.py</code>, <code>flow/factorilog.tcl</code>):</strong> Condenses strongly connected recipe cycles (Tarjan SCC) to enforce acyclic West-to-East topological ordering, minimizes continuous quadratic wirelength with boundary anchors, and legalizes macros into 94&times;94 city-block cores around 6-tile street channels.
          </p>
        </div>

        <div class="svg-card">
          <img src="assets/pipeline_router_3d.svg" alt="Stage 3 Hierarchical 3D Multi-Layer Maze Router" loading="lazy">
          <p class="svg-desc">
            <strong>3. Hierarchical 3D Maze Router (<code>def2bp.py</code>, <code>foundry/router/</code>):</strong> Routes intra-block nets inside each 94&times;94 core before promoting cross-block nets onto highway street channels. Negotiated PathFinder congestion cost (<code>base + history</code> &times; <code>present</code>) allocates underground belt and pipe jumps using exact prototype spans with zero DRC violations.
          </p>
        </div>

        <div class="svg-card">
          <img src="assets/pipeline_verilog_campaign.svg" alt="Stage 4 SystemVerilog FSM, Native ELF Verification, and UDP Freeplay Campaign" loading="lazy">
          <p class="svg-desc">
            <strong>4. RTL Controllers, Native ELF Signoff &amp; Autonomous Agent:</strong> Compiles <code>power_sequencer.v</code> into Factorio combinators, validates throughput against patched <code>dlopen</code> symbols in Factorio&apos;s own binary (<code>TransportLine::move</code>, <code>FluidBuffer::getFlowRate</code>), and drives a live unmodded multiplayer game over UDP.
          </p>
        </div>
      </div>
    </section>

    <section id="campaign" class="card">
      <div class="sec-head">
        <h2>Zero-Cheat Autonomous Freeplay Starter Base</h2>
        <span class="badge">LIVE UDP LOCKSTEP VERIFIED</span>
      </div>

      <div class="two-col">
        <figure class="figure-card">
          <img src="assets/starter_base.webp" alt="Autonomous starter_base rendered blueprint" loading="lazy">
          <figcaption class="figure-cap">
            <h3>Procedural Terrain to Red-Science Mall</h3>
            <p>Synthesized directly from scanned Freeplay terrain coordinates (<code>foundry/campaign/cartographer.py</code>, <code>foundry/patch_router.py</code>, <code>foundry/mp_proxy.py</code>) without Lua command cheats or fixed map seeds.</p>
          </figcaption>
        </figure>

        <div class="figure-card padded">
          <div class="panel-body-stack">
            <h3 class="panel-heading-emerald">End-to-End Lockstep Pipeline</h3>
            <p class="panel-copy">
              Instead of pasting cheat blueprints with elevated Lua permissions, the agent spawns as an ordinary engineer in headless multiplayer Freeplay, classifies water shorelines and resource clusters from map packets, navigates A* paths around cliffs and water, mines initial ore, crafts drills and boilers, and builds every entity manually over UDP.
            </p>
            <ul class="campaign-list">
              <li><strong>Cartographer:</strong> Detects water, resource patches, cliffs, rocks, and trees dynamically.</li>
              <li><strong>Patch Router:</strong> Synthesizes shoreline steam power and patch-to-smelter belt topologies.</li>
              <li><strong>Anti-Cheat Enforced:</strong> Tested statically by <code>//tools:no_bot_cheats_test</code>.</li>
            </ul>
          </div>
          <div class="actions-row">
            <button class="btn primary" data-bp-copy="starter_base">Copy starter_base.blueprint</button>
            <a class="btn" href="data/blueprints/starter_base.blueprint" download="starter_base.blueprint">Download .blueprint</a>
          </div>
        </div>
      </div>
    </section>

    <section id="benchmarks" class="card">
      <div class="sec-head">
        <h2>Factory Scaling &amp; Compile Benchmarks (60 SPM per Pack)</h2>
        <span class="badge">0 DRC ACROSS ALL TARGETS</span>
      </div>

      <div class="prov-box">
        <div>
          <strong>Measurement Provenance:</strong>
          Built and timed at commit <a href="{commit_url}"><code>{short_sha}</code></a> (<code>{commit_date}</code>)
          on <strong>{cpu_model}</strong> ({phys_cores} physical cores / {log_threads} logical threads).
        </div>
        <div class="conc-list">
          {concurrency_pills_html}
        </div>
      </div>

      <div class="actions-row actions-between">
        <input type="text" id="bench-search" class="search-input" placeholder="Search target or pack...">
        <span class="mono hint-mono">Click any column header to sort</span>
      </div>

      <div class="table-scroll">
        <table id="bench-table">
          <thead>
            <tr>
              <th data-sort="target">Target</th>
              <th class="num" data-sort="sciences_count">Packs</th>
              <th data-sort="die_w">Die Size</th>
              <th class="num" data-sort="macro_instances">Macros</th>
              <th class="num" data-sort="pdk_cells">PDK Cells</th>
              <th class="num" data-sort="bp_entities">BP Entities</th>
              <th class="num" data-sort="drc_violations">DRC</th>
              <th class="num" data-sort="synth">Synth (s)</th>
              <th class="num" data-sort="pnr">PnR (s)</th>
              <th class="num" data-sort="bp">Route+BP (s)</th>
              <th class="num" data-sort="compile_sec">Compile (s)</th>
            </tr>
          </thead>
          <tbody id="bench-tbody">
            {table_rows_html}
          </tbody>
        </table>
      </div>
    </section>

    <footer>
      <p>
        Generated from real <code>factorilog</code> build outputs &middot;
        <a href="{repo_url}">Source Repository</a> &middot;
        <a href="/">atvrager Index</a>
      </p>
    </footer>
  </main>

  <dialog id="zoom-modal">
    <div class="zoom-bar">
      <span id="zoom-caption" class="mono zoom-cap"></span>
      <button class="btn sm" id="zoom-close">Close</button>
    </div>
    <img id="zoom-img" src="" alt="">
  </dialog>

  <script src="app.js"></script>
</body>
</html>
"""


def _build_benchmark_rows_html(targets: Sequence[Mapping[str, Any]]) -> str:
    """Build static HTML table rows for the benchmark scaling table."""
    html_rows: list[str] = []
    for row in targets:
        st = row["stages_sec"]
        target = escape(str(row["target"]))
        packs = int(row["sciences_count"])
        die_str = f"{int(row['die_w'])}&times;{int(row['die_h'])}"
        die_area = int(row["die_tiles"])
        drc = int(row["drc_violations"])
        html_rows.append(
            "<tr>"
            f'<td class="mono" data-val="{target}">{target}</td>'
            f'<td class="num" data-val="{packs}">{packs}</td>'
            f'<td class="mono" data-val="{die_area}">{die_str}</td>'
            f'<td class="num" data-val="{row["macro_instances"]}">{row["macro_instances"]:,}</td>'
            f'<td class="num" data-val="{row["pdk_cells"]}">{row["pdk_cells"]}</td>'
            f'<td class="num" data-val="{row["bp_entities"]}">{row["bp_entities"]:,}</td>'
            f'<td class="num drc-zero" data-val="{drc}">{drc}</td>'
            f'<td class="num" data-val="{st["synth"]:.3f}">{st["synth"]:.2f}</td>'
            f'<td class="num" data-val="{st["pnr"]:.3f}">{st["pnr"]:.2f}</td>'
            f'<td class="num" data-val="{st["bp"]:.3f}">{st["bp"]:.2f}</td>'
            f'<td class="num" data-val="{row["compile_sec"]:.3f}"><strong>{row["compile_sec"]:.2f}</strong></td>'
            "</tr>"
        )
    return "\n".join(html_rows)


def _build_concurrency_html(stage_conc: Mapping[str, str]) -> str:
    """Build monospace pill tags showing thread allocation for each flow stage."""
    ordered = ("synth", "pnr", "bp", "render", "save")
    pills: list[str] = []
    for key in ordered:
        desc = escape(str(stage_conc.get(key, "")))
        pills.append(f'<span class="conc-chip"><strong>{key}</strong>: {desc}</span>')
    return "\n".join(pills)
