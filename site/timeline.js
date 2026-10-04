// The orders table's history as a row of columns, one per version, lined up
// over the range input that picks which version a query reads. The input is
// the control (keyboard, screen readers); the columns are a picture of what
// each step did to the table, and clicking one moves the input there.

const COMPACT = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const FULL = new Intl.NumberFormat("en-US");
const THUMB = 16; // px; must match the range thumb width in style.css
const MAX_LABELLED = 8; // beyond this, operation names would collide
const VALUE_WIDTH = 34; // px a column needs for its row count above it
const LABEL_WIDTH = 26; // px a "v12" label needs below the track

// Where version i sits along the input, matching where the browser draws the
// thumb's centre: the thumb never overhangs the track, so the ends are inset.
const position = (i, n) => (n <= 1 ? "50%" : `calc(${THUMB / 2}px + (100% - ${THUMB}px) * ${i / (n - 1)})`);

export function renderTimeline({ chart, labels, input }, commits, selected, onPick) {
  const n = commits.length;
  const most = Math.max(1, ...commits.map((c) => c.rows));
  // Each Append adds a column, so thin out what is printed as space runs out.
  // The selected version always keeps its value and label.
  const spacing = n <= 1 ? Infinity : (chart.clientWidth - THUMB) / (n - 1);
  const barWidth = Math.max(4, Math.min(18, spacing - 4));
  const labelEvery = Math.max(1, Math.ceil(LABEL_WIDTH / spacing));

  input.max = String(n - 1);
  input.value = String(selected);
  input.setAttribute("aria-valuetext", describe(commits[selected], n));

  chart.replaceChildren(
    ...commits.map((commit, i) => {
      const column = document.createElement("button");
      column.type = "button";
      column.tabIndex = -1; // the range input is the keyboard path
      column.className = "column" + (i === selected ? " selected" : "");
      column.style.left = position(i, n);
      const bar = document.createElement("span");
      bar.className = "column-bar";
      bar.style.height = `${(100 * commit.rows) / most}%`;
      bar.style.width = `${barWidth}px`;
      const value = document.createElement("span");
      value.className = "column-value";
      value.textContent = COMPACT.format(commit.rows);
      if (spacing < VALUE_WIDTH && i !== selected) value.style.visibility = "hidden";
      column.append(value, bar);
      column.addEventListener("click", () => onPick(i));
      return column;
    }),
  );

  labels.replaceChildren(
    ...commits.map((commit, i) => {
      const label = document.createElement("span");
      label.className = "timeline-label" + (i === selected ? " selected" : "");
      label.style.left = position(i, n);
      label.textContent = `v${commit.version}`;
      // Keep every labelEvery-th label, plus the last and the selected one,
      // and drop any regular label that would crowd those two.
      const kept = i === selected || i === n - 1;
      const crowds = [selected, n - 1].some((j) => j !== i && Math.abs(i - j) < labelEvery);
      if (!kept && (i % labelEvery !== 0 || crowds)) label.style.visibility = "hidden";
      if (n <= MAX_LABELLED) {
        const op = document.createElement("small");
        op.textContent = commit.operation.toLowerCase();
        label.append(op);
      }
      return label;
    }),
  );
}

export function describe(commit, n) {
  const latest = commit.version === n - 1 ? ", latest" : "";
  return `v${commit.version}${latest}: ${commit.operation}, ${FULL.format(commit.rows)} rows`;
}
