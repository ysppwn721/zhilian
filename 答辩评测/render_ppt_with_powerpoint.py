"""Render a PPTX with the locally installed Microsoft PowerPoint.

This is an optional Windows visual-QA tool. It opens the source read-only,
exports slide PNGs and a PDF, and never writes back to the input deck.
PowerPoint must already be installed and activated on the machine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def render(source: Path, output: Path) -> dict:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise RuntimeError('缺少 pywin32；请在当前 Python 环境安装 pywin32') from exc
    source = source.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pdf = output / (source.stem + '.pdf')
    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx('PowerPoint.Application')
    presentation = None
    try:
        try:
            app.DisplayAlerts = 0
        except Exception:
            pass
        presentation = app.Presentations.Open(str(source), ReadOnly=True, Untitled=True, WithWindow=False)
        # PowerPoint writes one image per slide into output.
        presentation.Export(str(output), 'PNG', 1600, 900)
        # 32 is ppSaveAsPDF; using SaveAs is more compatible than the optional
        # ExportAsFixedFormat signature across Office versions.
        presentation.SaveAs(str(pdf), 32)
        result = {
            'source': str(source),
            'slides': int(presentation.Slides.Count),
            'png_count': len({p.name.casefold() for p in output.iterdir() if p.suffix.casefold() == '.png'}),
            'pdf': str(pdf),
            'pdf_bytes': pdf.stat().st_size if pdf.exists() else 0,
            'status': 'passed' if pdf.exists() else 'failed',
            'renderer': 'Microsoft PowerPoint COM',
        }
    finally:
        if presentation is not None:
            presentation.Close()
        app.Quit()
        pythoncom.CoUninitialize()
    (output / 'render_manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(json.dumps(render(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
