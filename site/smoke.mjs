// Load the built site's bundle into Pyodide under Node and drive it the way
// app.js does. The pytest suite runs demo.py natively, where os.link exists,
// so this is the only check that the browser path works: the os.link
// stand-in, the bundle layout, and the code under Pyodide's Python.
//
// Run after site/build.py, with pyodide installed at the version app.js loads:
//     npm install --no-save pyodide@314.0.7
//     node site/smoke.mjs _site

import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { loadPyodide } from "pyodide";

const site = process.argv[2] ?? "_site";
const pyodide = await loadPyodide();
// unpackArchive rejects a Node Buffer; the page hands it an ArrayBuffer.
const bundle = new Uint8Array(await readFile(join(site, "packages.zip")));
pyodide.unpackArchive(bundle, "zip", { extractDir: "/lib" });
pyodide.runPython("import sys; sys.path.insert(0, '/lib')");

const hasLink = pyodide.runPython("import os; hasattr(os, 'link')");
const demo = pyodide.pyimport("demo");
const failures = [];
const check = (ok, message) => {
  if (!ok) failures.push(message);
};

const history = JSON.parse(demo.setup("/data"));
check(history.length === 6, `expected 6 versions, got ${history.length}`);

for (const preset of JSON.parse(demo.presets())) {
  for (const [reorder, pushdown] of [[true, true], [false, true], [true, false]]) {
    for (const version of [1, history.length - 1]) {
      const result = JSON.parse(demo.run(preset.sql, version, reorder, pushdown));
      const where = `${preset.name} at v${version}, reorder=${reorder}, pushdown=${pushdown}`;
      check(!result.error, `${where}: ${result.error}`);
      check(result.version === version, `${where}: read v${result.version}`);
      check(result.plan?.operator, `${where}: plan has no operator`);
    }
  }
}

const appended = JSON.parse(demo.append_orders(200));
check(appended.length === history.length + 1, "append_orders did not add a version");
check(appended.at(-1).rows === history.at(-1).rows + 200, "append_orders added the wrong row count");

console.log(`Python ${pyodide.runPython("import sys; sys.version.split()[0]")} in Pyodide ${pyodide.version}`);
console.log(`os.link ${hasLink ? "present" : "missing, so the demo's stand-in was used"}`);
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(`ok: ${appended.length} versions, every preset ran under every setting`);
