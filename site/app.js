const BP_CACHE = {};
const COPY_STATUS = document.getElementById("copy-status");

document.querySelectorAll("[data-bp-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = btn.getAttribute("data-bp-copy");
    COPY_STATUS.textContent = "Loading " + target + "...";
    try {
      if (!BP_CACHE[target]) {
        const resp = await fetch("data/blueprints/" + target + ".blueprint");
        BP_CACHE[target] = (await resp.text()).trim();
      }
      await navigator.clipboard.writeText(BP_CACHE[target]);
      COPY_STATUS.textContent =
        "Copied " + target + " blueprint string (" + BP_CACHE[target].length.toLocaleString() + " chars)";
    } catch (err) {
      COPY_STATUS.textContent = "Clipboard failed (serve over HTTP/HTTPS): " + err;
    }
  });
});

const modal = document.getElementById("zoom-modal");
const zoomImg = document.getElementById("zoom-img");
const zoomCap = document.getElementById("zoom-caption");
document.getElementById("zoom-close").addEventListener("click", () => modal.close());
modal.addEventListener("click", (ev) => {
  if (ev.target === modal) modal.close();
});
document.querySelectorAll(".figure-card img, .svg-card img").forEach((img) => {
  img.addEventListener("click", () => {
    zoomImg.src = img.src;
    zoomCap.textContent = img.alt || img.src;
    modal.showModal();
  });
});

// Handle benchmark table sorting and search
const benchSearch = document.getElementById("bench-search");
const benchTbody = document.getElementById("bench-tbody");
let sortKey = "sciences_count";
let sortAsc = true;

benchSearch.addEventListener("input", () => {
  const q = benchSearch.value.trim().toLowerCase();
  benchTbody.querySelectorAll("tr").forEach((tr) => {
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none";
  });
});

document.querySelectorAll("#bench-table th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.getAttribute("data-sort");
    if (sortKey === key) sortAsc = !sortAsc;
    else {
      sortKey = key;
      sortAsc = true;
    }
    const rows = Array.from(benchTbody.querySelectorAll("tr"));
    const colIdx = th.cellIndex;
    rows.sort((a, b) => {
      const va = a.cells[colIdx].getAttribute("data-val") || a.cells[colIdx].textContent;
      const vb = b.cells[colIdx].getAttribute("data-val") || b.cells[colIdx].textContent;
      const na = parseFloat(va);
      const nb = parseFloat(vb);
      const cmp = !Number.isNaN(na) && !Number.isNaN(nb) ? na - nb : va.localeCompare(vb);
      return sortAsc ? cmp : -cmp;
    });
    rows.forEach((r) => benchTbody.appendChild(r));
  });
});

// Initialize interactive canvas die viewer
(function initDieInspector() {
  const wrap = document.getElementById("canvas-wrap");
  const canvas = document.getElementById("die-canvas");
  const ctx = canvas.getContext("2d");
  const hud = document.getElementById("die-hud");
  const tip = document.getElementById("die-tooltip");
  let die = null;
  let spriteImg = null;
  let spriteReady = false;
  const cam = { x: 600, y: 600, scale: 0.5 };
  let dragging = false;
  let lastPt = { x: 0, y: 0 };
  let hoveredIdx = -1;
  let maxRank = 1;
  let rafPending = false;

  const machinePalette = [
    "#38bdf8",
    "#f59e0b",
    "#34d399",
    "#a78bfa",
    "#f472b6",
    "#2dd4bf",
    "#fb7185",
    "#818cf8",
  ];

  function rankColor(rank) {
    const t = Math.max(0, Math.min(1, rank / Math.max(maxRank, 1)));
    const r = Math.round(0x38 + (0xf4 - 0x38) * t);
    const g = Math.round(0xbd + (0x72 - 0xbd) * t);
    const b = Math.round(0xf8 + (0xb6 - 0xf8) * t);
    return "rgb(" + r + "," + g + "," + b + ")";
  }

  function resetView(cx, cy, spanTiles) {
    const rect = wrap.getBoundingClientRect();
    const minSide = Math.min(rect.width, rect.height);
    cam.x = cx;
    cam.y = cy;
    cam.scale = (minSide * 0.92) / spanTiles;
    scheduleDraw();
  }

  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = wrap.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    scheduleDraw();
  }

  function scheduleDraw() {
    if (!rafPending) {
      rafPending = true;
      requestAnimationFrame(() => {
        rafPending = false;
        draw();
      });
    }
  }

  function screenToWorld(sx, sy, rect) {
    return [
      cam.x + (sx - rect.width * 0.5) / cam.scale,
      cam.y + (sy - rect.height * 0.5) / cam.scale,
    ];
  }

  function draw() {
    const rect = wrap.getBoundingClientRect();
    ctx.fillStyle = "#070a0f";
    ctx.fillRect(0, 0, rect.width, rect.height);
    if (!die) return;

    ctx.save();
    ctx.translate(rect.width * 0.5, rect.height * 0.5);
    ctx.scale(cam.scale, cam.scale);
    ctx.translate(-cam.x, -cam.y);

    const dw = die.die.width;
    const dh = die.die.height;
    ctx.fillStyle = "#0b0e14";
    ctx.fillRect(0, 0, dw, dh);

    const sprAlpha = Number(document.getElementById("sprite-opacity").value) / 100.0;
    if (spriteReady && sprAlpha > 0) {
      const sr = die.die.sprite_rect || [0, 0, dw, dh];
      ctx.save();
      ctx.globalAlpha = sprAlpha;
      ctx.drawImage(spriteImg, sr[0], sr[1], sr[2], sr[3]);
      ctx.restore();
    }

    if (document.getElementById("layer-streets").checked) {
      ctx.fillStyle = "rgba(51, 65, 85, 0.35)";
      for (const b of die.blockages) {
        ctx.fillRect(b[0], b[1], b[2] - b[0], b[3] - b[1]);
      }
      ctx.strokeStyle = "#1e293b";
      ctx.lineWidth = 1.0 / cam.scale;
      for (let gx = 0; gx <= dw; gx += die.die.pitch) {
        ctx.beginPath();
        ctx.moveTo(gx, 0);
        ctx.lineTo(gx, dh);
        ctx.stroke();
      }
      for (let gy = 0; gy <= dh; gy += die.die.pitch) {
        ctx.beginPath();
        ctx.moveTo(0, gy);
        ctx.lineTo(dw, gy);
        ctx.stroke();
      }
    }

    const lw = Math.max(0.55, 1.4 / cam.scale);
    if (document.getElementById("layer-pipes").checked) {
      ctx.strokeStyle = "rgba(167, 139, 250, 0.78)";
      ctx.lineWidth = lw;
      ctx.beginPath();
      for (const p of die.pipes) {
        ctx.moveTo(p[0], p[1]);
        ctx.lineTo(p[2], p[3]);
      }
      ctx.stroke();
    }

    if (document.getElementById("layer-belts").checked) {
      ctx.strokeStyle = "rgba(245, 158, 11, 0.78)";
      ctx.lineWidth = lw;
      ctx.beginPath();
      for (const s of die.belts) {
        ctx.moveTo(s[0], s[1]);
        ctx.lineTo(s[2], s[3]);
      }
      ctx.stroke();
    }

    if (document.getElementById("layer-ug").checked) {
      ctx.strokeStyle = "rgba(34, 211, 238, 0.85)";
      ctx.lineWidth = lw;
      ctx.beginPath();
      for (const u of die.underground) {
        ctx.moveTo(u[0], u[1]);
        ctx.lineTo(u[2], u[3]);
      }
      ctx.stroke();
    }

    if (document.getElementById("layer-copper").checked) {
      ctx.strokeStyle = "rgba(251, 146, 60, 0.55)";
      ctx.lineWidth = lw * 0.85;
      ctx.beginPath();
      for (const w of die.copper_wires) {
        ctx.moveTo(w[0], w[1]);
        ctx.lineTo(w[2], w[3]);
      }
      ctx.stroke();
    }

    if (document.getElementById("layer-macros").checked) {
      const mode = document.getElementById("color-mode").value;
      const q = document.getElementById("filter-recipe").value.trim().toLowerCase();
      for (let i = 0; i < die.macros.length; i++) {
        const m = die.macros[i];
        const recipe = die.recipes[m[5]];
        const machine = die.machines[m[4]];
        const matched =
          !q ||
          recipe.toLowerCase().includes(q) ||
          machine.toLowerCase().includes(q) ||
          m[9].toLowerCase().includes(q);
        ctx.fillStyle = mode === "rank" ? rankColor(m[8]) : machinePalette[m[4] % machinePalette.length];
        ctx.globalAlpha = q ? (matched ? 0.95 : 0.12) : i === hoveredIdx ? 1.0 : 0.75;
        ctx.fillRect(m[0], m[1], m[2], m[3]);
        if (i === hoveredIdx || (q && matched)) {
          ctx.strokeStyle = "#ffffff";
          ctx.lineWidth = 1.6 / cam.scale;
          ctx.strokeRect(m[0], m[1], m[2], m[3]);
        }
      }
      ctx.globalAlpha = 1.0;
    }

    ctx.strokeStyle = "#334155";
    ctx.lineWidth = 2.0 / cam.scale;
    ctx.strokeRect(0, 0, dw, dh);
    ctx.restore();

    hud.textContent =
      "Zoom: " +
      cam.scale.toFixed(2) +
      " px/tile | Center: (" +
      cam.x.toFixed(0) +
      ", " +
      cam.y.toFixed(0) +
      ") | Scroll to zoom, drag to pan";
  }

  wrap.addEventListener("mousedown", (ev) => {
    dragging = true;
    lastPt = { x: ev.clientX, y: ev.clientY };
  });
  window.addEventListener("mouseup", () => {
    dragging = false;
  });
  wrap.addEventListener("mousemove", (ev) => {
    const rect = wrap.getBoundingClientRect();
    if (dragging) {
      cam.x -= (ev.clientX - lastPt.x) / cam.scale;
      cam.y -= (ev.clientY - lastPt.y) / cam.scale;
      lastPt = { x: ev.clientX, y: ev.clientY };
      tip.style.display = "none";
      scheduleDraw();
      return;
    }
    if (!die) return;
    const sx = ev.clientX - rect.left;
    const sy = ev.clientY - rect.top;
    const [wx, wy] = screenToWorld(sx, sy, rect);
    let found = -1;
    for (let i = 0; i < die.macros.length; i++) {
      const m = die.macros[i];
      if (wx >= m[0] && wx <= m[0] + m[2] && wy >= m[1] && wy <= m[1] + m[3]) {
        found = i;
        break;
      }
    }
    if (found !== hoveredIdx) {
      hoveredIdx = found;
      scheduleDraw();
    }
    if (found >= 0) {
      const m = die.macros[found];
      tip.innerHTML =
        "<strong style='color:var(--cyan)'>" +
        m[9] +
        "</strong><br>" +
        "machine: " +
        die.machines[m[4]] +
        "<br>" +
        "recipe: " +
        die.recipes[m[5]] +
        "<br>" +
        "rate: " +
        m[6] +
        " / " +
        m[7] +
        " per min<br>" +
        "rank: " +
        m[8] +
        " &middot; tile (" +
        m[0] +
        ", " +
        m[1] +
        ") [" +
        m[2] +
        "x" +
        m[3] +
        "]";
      tip.style.left = Math.min(rect.width - 280, sx + 14) + "px";
      tip.style.top = Math.min(rect.height - 110, sy + 14) + "px";
      tip.style.display = "block";
    } else {
      tip.style.display = "none";
    }
  });
  wrap.addEventListener(
    "wheel",
    (ev) => {
      ev.preventDefault();
      const rect = wrap.getBoundingClientRect();
      const sx = ev.clientX - rect.left;
      const sy = ev.clientY - rect.top;
      const [wx0, wy0] = screenToWorld(sx, sy, rect);
      const factor = ev.deltaY < 0 ? 1.18 : 1.0 / 1.18;
      cam.scale = Math.max(0.25, Math.min(36.0, cam.scale * factor));
      cam.x = wx0 - (sx - rect.width * 0.5) / cam.scale;
      cam.y = wy0 - (sy - rect.height * 0.5) / cam.scale;
      scheduleDraw();
    },
    { passive: false }
  );

  [
    "layer-streets",
    "layer-macros",
    "layer-belts",
    "layer-ug",
    "layer-pipes",
    "layer-copper",
    "sprite-opacity",
    "color-mode",
    "filter-recipe",
  ].forEach((id) => {
    document.getElementById(id).addEventListener("input", scheduleDraw);
  });
  document.getElementById("cam-full").addEventListener("click", () => resetView(600, 600, 1200));
  document.getElementById("cam-west").addEventListener("click", () => resetView(110, 600, 380));
  document.getElementById("cam-east").addEventListener("click", () => resetView(1060, 600, 380));

  window.addEventListener("resize", resizeCanvas);
  resizeCanvas();

  spriteImg = new Image();
  spriteImg.onload = () => {
    spriteReady = true;
    scheduleDraw();
  };
  spriteImg.src = "assets/science_chassis.webp";

  fetch("data/die_science_chassis.json")
    .then((r) => r.json())
    .then((data) => {
      die = data;
      maxRank = die.macros.reduce((acc, m) => Math.max(acc, m[8]), 1);
      resetView(die.die.width * 0.5, die.die.height * 0.5, die.die.width);
    })
    .catch(() => {
      hud.textContent =
        "Interactive JSON layer disabled over file protocol; serve site directory with HTTP";
    });
})();
