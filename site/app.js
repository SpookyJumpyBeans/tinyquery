// Loads Pyodide, unpacks tinyquery + tinydelta + demo.py from packages.zip,
// builds the demo tables, and runs queries through demo.run().

import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";

const MAX_ROWS_SHOWN = 100;

const el = {
  sql: document.getElementById("sql"),
  run: document.getElementById("run"),
  status: document.getElementById("status"),
  presets: document.getElementById("presets"),
  plan: document.getElementById("plan"),
  results: document.getElementById("results"),
  history: document.getElementById("orders-history"),
};

let demo = null;

function setStatus(text, isError = false) {
  el.status.textContent = text;
  el.status.classList.toggle("error", isError);
}

async function boot() {
  const started = performance.now();
  setStatus("Loading Python…");
  const pyodide = await loadPyodide();

  setStatus("Loading tinyquery and tinydelta…");
  const response = await fetch("packages.zip");
  if (!response.ok) throw new Error(`packages.zip: HTTP ${response.status}`);
  pyodide.unpackArchive(await response.arrayBuffer(), "zip", { extractDir: "/lib" });
  pyodide.runPython("import sys; sys.path.insert(0, '/lib')");
  demo = pyodide.pyimport("demo");

  setStatus("Building tables…");
  renderHistory(JSON.parse(demo.setup("/data")));
  renderPresets(JSON.parse(demo.presets()));

  el.sql.disabled = false;
  el.run.disabled = false;
  setStatus(`Ready in ${((performance.now() - started) / 1000).toFixed(1)}s`);
  runQuery();
}

function renderHistory(commits) {
  const latest = commits[commits.length - 1];
  el.history.textContent =
    `${commits.length} versions (v0–v${latest.version}), ${latest.rows.toLocaleString()} rows at latest`;
}

function renderPresets(presets) {
  el.presets.replaceChildren(
    ...presets.map((preset, i) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = preset.name;
      button.setAttribute("aria-pressed", String(i === 0));
      button.addEventListener("click", () => {
        for (const other of el.presets.children) other.setAttribute("aria-pressed", "false");
        button.setAttribute("aria-pressed", "true");
        el.sql.value = preset.sql;
        runQuery();
      });
      return button;
    }),
  );
  el.sql.value = presets[0].sql;
}

let runCount = 0;
let running = false;

// Python runs on the main thread, so the page cannot repaint until a query
// finishes. Wait for a frame first, or "Running…" would never be seen.
const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve)));

async function runQuery() {
  if (!demo || running) return;
  running = true;
  el.run.disabled = true;
  el.run.textContent = "Running…";
  setStatus("Running…");
  await nextFrame();

  try {
    const started = performance.now();
    const result = JSON.parse(demo.run(el.sql.value));
    const elapsed = performance.now() - started;
    runCount += 1;

    if (result.error) {
      setStatus(`Run ${runCount}: query failed`, true);
      const message = document.createElement("p");
      message.className = "error-text";
      message.textContent = result.error;
      el.results.replaceChildren(message);
      el.plan.textContent = "";
    } else {
      const n = result.rows.length;
      setStatus(
        `Run ${runCount}: ${n.toLocaleString()} row${n === 1 ? "" : "s"} in ` +
          `${elapsed.toFixed(0)}ms, orders v${result.version}`,
      );
      el.plan.textContent = result.text;
      renderRows(result.columns, result.rows);
    }
    flash(el.plan, el.results);
  } finally {
    running = false;
    el.run.disabled = false;
    el.run.textContent = "Run";
  }
}

// Re-running a query often produces the same rows and nearly the same plan,
// so mark the panels as refreshed or the click looks like it did nothing.
function flash(...panels) {
  for (const panel of panels) {
    panel.classList.remove("refreshed");
    void panel.offsetWidth; // restart the animation
    panel.classList.add("refreshed");
  }
}

function renderRows(columns, rows) {
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  columns.forEach((name, i) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = name;
    if (rows.length && typeof rows[0][i] === "number") th.className = "num";
    head.append(th);
  });
  const body = table.createTBody();
  for (const row of rows.slice(0, MAX_ROWS_SHOWN)) {
    const tr = body.insertRow();
    for (const value of row) {
      const td = tr.insertCell();
      td.textContent = value === null ? "NULL" : formatValue(value);
      if (typeof value === "number") td.className = "num";
    }
  }
  const parts = [table];
  if (rows.length > MAX_ROWS_SHOWN) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = `Showing ${MAX_ROWS_SHOWN} of ${rows.length.toLocaleString()} rows.`;
    parts.push(note);
  }
  el.results.replaceChildren(...parts);
}

function formatValue(value) {
  if (typeof value === "number" && !Number.isInteger(value)) return value.toFixed(2);
  return String(value);
}

el.run.addEventListener("click", runQuery);
el.sql.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    runQuery();
  }
});

boot().catch((error) => {
  console.error(error);
  setStatus(`Failed to start: ${error.message}`, true);
});
