from pathlib import Path
import json, time, sys
from pdf2docx import Converter

source = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2]).resolve()
out.mkdir(parents=True, exist_ok=True)
start = time.perf_counter()
docx = out / 'pdf2docx_20pages.docx'
cv = Converter(str(source))
try:
    cv.convert(str(docx), start=0, end=20, multi_processing=False)
finally:
    cv.close()
report = {'source': str(source), 'pages_requested': 20, 'docx': str(docx), 'elapsed_seconds': round(time.perf_counter()-start, 3)}
(out/'pdf2docx_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
