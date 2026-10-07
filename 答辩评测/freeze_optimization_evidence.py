"""Freeze data fingerprints; verify before/after optimization without touching tests."""
import argparse
import hashlib
import json
import platform
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '答辩评测/optimization_20261001'
DATA = [
    '答辩评测/eval_dataset.jsonl', '答辩评测/real_human_eval_dataset.jsonl',
    '答辩评测/semantic_rewrite_eval_v2.jsonl',
    '答辩评测/semantic_rewrite_eval_v2_api_predictions.jsonl',
    '答辩评测/external_programmatic_holdout_all.jsonl',
    '长文Word测试/配套数据_初始.xlsx',
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    manifest = OUT / 'frozen_inputs.json'
    if args.verify:
        frozen = json.loads(manifest.read_text(encoding='utf-8'))
        changed = [name for name, info in frozen['files'].items()
                   if not (ROOT / name).is_file() or sha(ROOT / name) != info['sha256']]
        if changed:
            raise SystemExit('FROZEN INPUT CHANGED: ' + ', '.join(changed))
        print(f"PASS: {len(frozen['files'])} frozen inputs unchanged")
        return
    if manifest.exists():
        raise SystemExit('Manifest already exists; use --verify rather than overwrite')
    OUT.mkdir(parents=True, exist_ok=True)
    names = DATA + [p.relative_to(ROOT).as_posix() for p in
                    sorted((ROOT / '答辩评测/cn_reports').glob('*.pdf'))]
    files = {name: {'sha256': sha(ROOT / name), 'bytes': (ROOT / name).stat().st_size}
             for name in names}
    result = {'created_local': datetime.now().isoformat(timespec='seconds'),
              'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                                 text=True).strip(),
              'platform': platform.platform(), 'files': files,
              'note': 'Working tree already contains pre-existing edits; git HEAD is not the code snapshot.'}
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    # Reconstruct the exact pre-turn extraction pattern from the current file.
    # This source includes prior working-tree edits (quote vocabulary and aliases).
    source = (ROOT / 'zhilian/engine.py').read_text(encoding='utf-8')
    first = source.index("        ('growth',", source.index('def extract_claims'))
    last = source.index("        ('ranking',", first)
    old = "        ('growth', r'(?:较上期|环比|较上年|比上年|同比)(增长|下降|持平|增加|减少|上升)(?:(' + NUM + r')%)?'),\n"
    baseline = source[:first] + old + source[last:]
    baseline = baseline.replace('EXTRACTION_VERSION = 6', 'EXTRACTION_VERSION = 5')
    (OUT / 'baseline_engine.py').write_text(baseline, encoding='utf-8')
    (OUT / 'baseline_source_note.txt').write_text(
        'Pre-turn working-tree pattern reconstructed; existing quote vocabulary/aliases retained.\n'
        'The newer implicit-comparison guard is unreachable for this older growth pattern.\n'
        + 'baseline_sha256=' + sha(OUT / 'baseline_engine.py') + '\n', encoding='utf-8')
    print(f'Frozen {len(files)} data/PDF files; baseline extractor preserved separately')


if __name__ == '__main__':
    main()
