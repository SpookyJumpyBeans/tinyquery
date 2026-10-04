// Draws an EXPLAIN ANALYZE plan as a tree. Each operator gets a pair of bars,
// estimated rows over actual rows, on one log scale shared by the whole plan,
// so a 25x misestimate is as long a gap at the top of the tree as at the leaves.

const NUMBER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

export function renderPlanTree(container, tooltip, plan, flagFactor) {
  const rows = flatten(plan);
  const largest = Math.max(10, ...rows.flatMap(({ node }) => [node.estimated_rows, node.actual_rows]));
  const top = 10 ** Math.ceil(Math.log10(largest));
  // log(v + 1) so that 0 rows has a zero-length bar and 1 row a visible stub.
  const x = (v) => (v <= 0 ? 0 : (100 * Math.log10(v + 1)) / Math.log10(top + 1));
  const ticks = [];
  for (let t = 1; t <= top; t *= 10) ticks.push(t);

  const table = document.createElement("table");
  table.className = "plan-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th scope="col">Operator</th>
        <th scope="col" class="rows-head">
          <span class="visually-hidden">Rows, estimated and actual, log scale</span>
          <div class="plot axis" aria-hidden="true">
            ${ticks.map((t) => `<span class="tick" style="left:${x(t)}%">${compact(t)}</span>`).join("")}
          </div>
        </th>
        <th scope="col">Estimate</th>
        <th scope="col" class="num">Self time</th>
      </tr>
    </thead>`;

  const body = table.createTBody();
  for (const { node, prefix } of rows) {
    const tr = body.insertRow();
    tr.tabIndex = 0;

    const op = tr.insertCell();
    op.className = "op";
    const detail = node.label.startsWith(node.operator)
      ? node.label.slice(node.operator.length).trim()
      : node.label;
    op.append(
      span("guide", prefix),
      span("op-name", node.operator),
      detail ? span("op-detail", ` ${detail}`) : "",
    );

    const bars = tr.insertCell();
    bars.className = "bars";
    bars.innerHTML = `
      <div class="plot">
        ${ticks.map((t) => `<span class="grid" style="left:${x(t)}%"></span>`).join("")}
        ${barLine("est", node.estimated_rows, x)}
        ${barLine("act", node.actual_rows, x)}
      </div>`;

    const error = tr.insertCell();
    error.className = "error";
    error.append(describeError(node, flagFactor));

    const time = tr.insertCell();
    time.className = "num";
    time.textContent = `${node.self_ms.toFixed(2)} ms`;

    const show = (event) => showTooltip(tooltip, tr, node, event);
    tr.addEventListener("mousemove", show);
    tr.addEventListener("focus", show);
    tr.addEventListener("mouseleave", () => hideTooltip(tooltip));
    tr.addEventListener("blur", () => hideTooltip(tooltip));
  }
  container.replaceChildren(table);
}

function flatten(node, out = [], lead = "", last = true, depth = 0) {
  out.push({ node, prefix: depth === 0 ? "" : lead + (last ? "└ " : "├ ") });
  const childLead = depth === 0 ? "" : lead + (last ? "  " : "│ ");
  node.children.forEach((child, i) =>
    flatten(child, out, childLead, i === node.children.length - 1, depth + 1),
  );
  return out;
}

function barLine(kind, value, x) {
  const label = kind === "est" ? "estimated" : "actual";
  return `
    <div class="bar-line ${kind}">
      <span class="bar" style="width:${x(value)}%"></span>
      <span class="tip" style="left:${x(value)}%"><span class="visually-hidden">${label} </span>${NUMBER.format(value)}</span>
    </div>`;
}

// How far off the estimate was, as a factor >= 1, matching PlanStats.misestimate.
function misestimate(node) {
  const est = Math.max(node.estimated_rows, 1);
  const act = Math.max(node.actual_rows, 1);
  return Math.max(est / act, act / est);
}

function describeError(node, flagFactor) {
  const factor = misestimate(node);
  if (factor < 1.05) return span("error-exact", "exact");
  const direction = node.estimated_rows > node.actual_rows ? "high" : "low";
  const text = `${factor < 10 ? factor.toFixed(1) : NUMBER.format(factor)}× ${direction}`;
  if (factor < flagFactor) return span("error-minor", text);
  const flag = document.createElement("span");
  flag.className = "flag";
  flag.append(span("flag-icon", "⚠"), ` ${text}`);
  flag.title = `Estimate off by ${flagFactor}x or more`;
  return flag;
}

function showTooltip(tooltip, row, node, event) {
  const factor = misestimate(node);
  tooltip.innerHTML = "";
  const title = document.createElement("div");
  title.className = "tt-title";
  title.textContent = node.label;
  const list = document.createElement("dl");
  for (const [term, value] of [
    ["Estimated rows", NUMBER.format(node.estimated_rows)],
    ["Actual rows", NUMBER.format(node.actual_rows)],
    ["Off by", factor < 1.05 ? "exact" : `${factor.toFixed(1)}×`],
    ["Total time", `${node.total_ms.toFixed(2)} ms`],
    ["Self time", `${node.self_ms.toFixed(2)} ms`],
  ]) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }
  tooltip.append(title, list);
  tooltip.hidden = false;

  // Measured after unhiding: a hidden element has no offsetParent.
  const box = tooltip.offsetParent.getBoundingClientRect();
  const rowBox = row.getBoundingClientRect();
  const pointerX = event.type === "mousemove" ? event.clientX : rowBox.left + 24;
  const left = Math.min(pointerX - box.left + 12, box.width - tooltip.offsetWidth - 4);
  tooltip.style.left = `${Math.max(4, left)}px`;
  tooltip.style.top = `${rowBox.bottom - box.top + 6}px`;
}

function hideTooltip(tooltip) {
  tooltip.hidden = true;
}

function span(className, text) {
  const el = document.createElement("span");
  el.className = className;
  el.textContent = text;
  return el;
}

function compact(n) {
  return n >= 1e6 ? `${n / 1e6}M` : n >= 1e3 ? `${n / 1e3}k` : String(n);
}
