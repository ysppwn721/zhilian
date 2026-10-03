"""拉取并核验公开训练数据集（只下载、不训练、不改默认模型）。

设计原则（沿用仓库既有 policy）：
  - 原始数据放在 output/ 下（已被 .gitignore 覆盖），不进交付包；
  - 每个数据集记录 URL、许可、SHA256、行数、字段，写入 manifest；
  - 中文句对数据集（CLUE 系列）用于通用中文语义预热；
  - 财报类数据集用于领域适配；
  - 全部为"可获取的公开资料"，是否最终用于训练由队内决定。

用法：
    python packaging/fetch_public_training_data.py            # 全部
    python packaging/fetch_public_training_data.py --only cmnli ocnli
    python packaging/fetch_public_training_data.py --list
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'output' / 'public_training_data'
UA = {'User-Agent': 'zhilian-research/1.0 (academic; contact via repo)'}

# name -> (许可, 语言, 用途, [可下载 URL...])
# URL 全部是 HuggingFace dataset 的 resolve 直链，无需 git-lfs 客户端。
CATALOG = {
    # ---- 通用中文语义（预训练阶段）----
    'cmnli': ('apache-2.0', 'zh', '中文句对推理：训练"两句话语义是否一致"的基础能力',
              ['https://huggingface.co/datasets/suolyer/cmnli/resolve/main/train.json',
               'https://huggingface.co/datasets/suolyer/cmnli/resolve/main/test.json']),
    'ocnli': ('apache-2.0', 'zh', '中文原生推理（非翻译），句式更接近真实中文',
              ['https://huggingface.co/datasets/suolyer/ocnli/resolve/main/train.json',
               'https://huggingface.co/datasets/suolyer/ocnli/resolve/main/test.json']),
    'afqmc': ('apache-2.0', 'zh', '中文句对相似度（蚂蚁金融语义相似度）',
              ['https://huggingface.co/datasets/suolyer/afqmc/resolve/main/train.json',
               'https://huggingface.co/datasets/suolyer/afqmc/resolve/main/test.json']),
    'lcqmc': ('apache-2.0', 'zh', '中文句对匹配（哈工大 LCQMC）',
              ['https://huggingface.co/datasets/Tongjilibo/LCQMC/resolve/main/LCQMC.train.data',
               'https://huggingface.co/datasets/Tongjilibo/LCQMC/resolve/main/LCQMC.valid.data']),
    # ---- 财报 / 表格领域 ----
    'tableeval': ('apache-2.0', 'zh+en', '中文问题 × 上市公司财报表 × 人工答案（跨语言表格问答）',
                  ['https://huggingface.co/datasets/wenge-research/TableEval/resolve/main/TableEval-test.jsonl']),
    'cfqa_gold': ('cc-by-4.0', 'zh', '中文上市公司财报问答（人工金标准 500 条）',
                  ['https://huggingface.co/datasets/ZackZhu00/CFQA_Chinese_Finance_Question_Answering/resolve/main/gold_standard_500.json']),
    'cfqa_advanced': ('cc-by-4.0', 'zh', '中文财报问答进阶 500 条',
                      ['https://huggingface.co/datasets/ZackZhu00/CFQA_Chinese_Finance_Question_Answering/resolve/main/test_advanced_500.json']),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, retries: int = 3) -> int:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=180) as resp, dest.open('wb') as fh:
                total = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
            return total
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries:
                raise
            print(f'      重试 {attempt}/{retries}：{exc}')
            time.sleep(2 * attempt)
    return 0


def peek(path: Path):
    """轻量核验：行数 + 首行字段。不依赖 pandas。

    注意：多个 CLUE 数据集的 `train.json` 实际是 JSONL（每行一个对象），
    扩展名不可信。因此先按 JSONL 试，失败再按整份 JSON 读。
    """
    try:
        # 先按 JSONL 逐行解析
        n = 0
        first = None
        with path.open(encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = None
                    n = 0
                    break
                n += 1
                if first is None:
                    first = obj
        if n and first is not None:
            return n, (sorted(first.keys()) if isinstance(first, dict) else type(first).__name__)
        # 退回整份 JSON
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, list):
            obj = data[0] if data else {}
            return len(data), (sorted(obj.keys()) if isinstance(obj, dict) else type(obj).__name__)
        if isinstance(data, dict):
            return 1, sorted(data.keys())[:12]
        return 1, type(data).__name__
    except Exception as exc:
        # 最后尝试按制表符分隔文本（LCQMC 的 .data）
        try:
            n = sum(1 for _ in path.open(encoding='utf-8', errors='replace'))
            return n, 'tsv(text)'
        except Exception:
            return None, f'读取失败: {exc}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', nargs='*', help='只拉指定数据集')
    ap.add_argument('--list', action='store_true', help='只列出清单，不下载')
    args = ap.parse_args()

    names = args.only or list(CATALOG)
    unknown = [n for n in names if n not in CATALOG]
    if unknown:
        print(f'✗ 未知数据集：{unknown}', file=sys.stderr)
        return 2

    print(f'{"数据集":16} {"许可":12} {"语言":7} 用途')
    print('-' * 100)
    for n in names:
        lic, lang, use, _ = CATALOG[n]
        print(f'{n:16} {lic:12} {lang:7} {use}')
    if args.list:
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {}

    for n in names:
        lic, lang, use, urls = CATALOG[n]
        print(f'\n=== {n} （{lic} / {lang}）===')
        entries = []
        for url in urls:
            dest = OUT / n / url.rsplit('/', 1)[-1]
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dest.exists() and dest.stat().st_size > 0:
                    print(f'  已存在 {dest.name} ({dest.stat().st_size/1024/1024:.2f} MB)，跳过下载')
                else:
                    size = download(url, dest)
                    print(f'  下载完成 {dest.name}  {size/1024/1024:.2f} MB')
            except Exception as exc:
                print(f'  ✗ 下载失败 {url}\n     {type(exc).__name__}: {exc}')
                continue
            rows, fields = peek(dest)
            print(f'  核验：{rows if rows is not None else "?"} 条 · 字段 {fields}')
            entries.append({'url': url, 'file': str(dest.relative_to(ROOT)),
                            'bytes': dest.stat().st_size, 'sha256': sha256(dest),
                            'rows': rows, 'fields': fields})
        manifest[n] = {'license': lic, 'language': lang, 'intended_use': use,
                       'retrieved': time.strftime('%Y-%m-%d'), 'files': entries}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'\n清单已写入 {manifest_path.relative_to(ROOT)}')
    total = sum(f.stat().st_size for f in OUT.rglob('*') if f.is_file())
    print(f'本次累计 {total/1024/1024:.1f} MB')
    print('\n注意：原始数据不进入交付包；用于训练前需完成许可与用途核对。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
