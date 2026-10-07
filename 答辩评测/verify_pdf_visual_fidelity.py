"""Render-level PDF fidelity gate for an independently exported revision."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zhilian.pdf_revised_export import compare_pdf_visual_fidelity


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--revised", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--diff-dir", type=Path)
    parser.add_argument("--dpi", type=int, default=96)
    parser.add_argument("--pixel-tolerance", type=int, default=0)
    args = parser.parse_args()
    report = compare_pdf_visual_fidelity(
        args.original, args.revised, diff_dir=args.diff_dir,
        dpi=args.dpi, pixel_tolerance=args.pixel_tolerance,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "changed_page_count": report["changed_page_count"],
        "page_count": report["page_count"],
        "report": str(args.out.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
