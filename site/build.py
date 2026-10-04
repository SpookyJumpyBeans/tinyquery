"""Assemble the static demo into _site/.

Copies the page files and bundles what Pyodide needs into packages.zip:
the tinyquery package from this repo, tinydelta from wherever Python imports
it (so install the delta extra first), and demo.py. The page unpacks the zip
into Pyodide's filesystem and imports from it like any installed package.

Run:
    pip install -e ".[delta]"
    python site/build.py
    python -m http.server -d _site
"""

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
PAGE_FILES = ("index.html", "style.css", "app.js")


def build(out: Path) -> Path:
    import tinydelta

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in PAGE_FILES:
        shutil.copy2(SITE / name, out / name)

    bundle = out / "packages.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for package in (ROOT / "tinyquery", Path(tinydelta.__file__).parent):
            for source in sorted(package.rglob("*.py")):
                archive.write(source, source.relative_to(package.parent).as_posix())
        archive.write(SITE / "demo.py", "demo.py")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    bundle = build(args.out)
    print(f"built {args.out} ({bundle.stat().st_size:,} byte bundle)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
