"""知链 HTTP API and static workbench."""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .demo import create_demo
from .llm import (config, mode, mode_label, using_mode, remote_allowed, suggest_links,
                  explain_diagnosis, consume_last_call_metrics)
from . import __version__
from . import ocr, quota, reranker
from .pdf_revised_export import available_docx_backends
from .store import Store, now
from .office import digest
from .report import build_report
from .agent import (run_agent, decide, agent_status, run_cross_document_audit,
                     run_repair_plan)

BASE = Path(__file__).resolve().parent.parent
DOWNLOAD_ROOT = Path(os.getenv('ZHILIAN_DOWNLOAD_DIR', str(BASE / 'downloads'))).resolve()
# 下载白名单按精确文件名匹配，任何一处版本号漏改都会直接 404。因此版本号只在这里
# 出现一次（取自 VERSION 文件），所有带版本的产物名由它插值生成——"发布新版本"
# 变成改 VERSION 一个文件，而不是在 10 个文件名字符串里找 0.2.1。
_V = __version__
PUBLIC_DOWNLOADS = {
    f'知链_v{_V}_Windows安装版.exe',
    f'知链_v{_V}_Windows免安装.zip',
    f'知链_v{_V}_跨平台构建包.zip',
    f'Zhilian-{_V}-linux-x86_64.tar.gz',
    f'Zhilian-{_V}-linux-x86_64-glibc228.tar.gz',
    f'Zhilian-{_V}-linux-x86_64-src.tar.gz',
    f'知链_源代码_v{_V}.zip',
    '知链事实表模板.xlsx',
    'Zhilian-bge-reranker-v2-m3-onnx-int8.zip',
    'Zhilian-bge-reranker-v2-m3-onnx-int8.tar.gz',
}


def load_environment():
    """Simple KEY=VALUE file; no interpolation, execution, or secret logging."""
    env = Path(os.getenv('ZHILIAN_CONFIG_FILE', str(BASE / '.env'))).expanduser()
    if env.exists():
        for raw in env.read_text(encoding='utf-8-sig').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            if re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Link(BaseModel):
    claim_id: str
    refs: list[str] = Field(max_length=2000)


class LinksRequest(BaseModel):
    revision: int
    links: list[Link] = Field(max_length=500)


class ChangeRequest(BaseModel):
    revision: int
    values: dict[str, float]


class RepairRequest(BaseModel):
    revision: int
    claim_ids: list[str] = Field(max_length=500)


class RevisionRequest(BaseModel):
    revision: int


class ModelModeRequest(RevisionRequest):
    mode: Literal['rules', 'local', 'hybrid', 'api', 'api_only']


class OcrRunRequest(RevisionRequest):
    image_ids: list[str] | None = Field(default=None, max_length=500)


class OcrItem(BaseModel):
    image_id: str
    text: str = Field(min_length=1, max_length=20000)


class OcrConfirmRequest(RevisionRequest):
    items: list[OcrItem] = Field(min_length=1, max_length=500)


class AgentDecisionRequest(BaseModel):
    revision: int
    decisions: list[dict] = Field(min_length=1, max_length=1)


class Derivation(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    expr: str = Field(min_length=1, max_length=500)
    unit: str = Field('', max_length=16)
    label: str = Field('', max_length=80)


class DerivationsRequest(BaseModel):
    revision: int
    derivations: list[Derivation] = Field(max_length=2000)


def create_app(data_dir=None):
    load_environment()
    store = Store(data_dir or os.getenv('ZHILIAN_DATA_DIR', str(BASE / '.zhilian')))
    app = FastAPI(title='知链', version=__version__, description='跨文档结论验证与增量修复')
    app.state.store = store
    quota.configure(store.root / 'quota.json')

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        # 访客标识必须在处理函数之前落到上下文里：限额按访客计数，
        # 且演示环境位于 Cloudflare 之后，真实来源 IP 只在请求头上。
        token = quota.identify(quota.client_key(
            request.headers, request.client.host if request.client else '-'))
        try:
            return await _boundary(request, call_next)
        finally:
            quota.release(token)

    async def _boundary(request: Request, call_next):
        # Public, allow-listed release assets are served without the workbench
        # Basic Auth prompt.  No directory listing or arbitrary file path is
        # exposed; all other routes keep the normal authentication boundary.
        if request.url.path.startswith('/downloads/'):
            return await call_next(request)
        password = os.getenv('ZHILIAN_ACCESS_PASSWORD', '')
        if password:
            valid = False
            try:
                scheme, token = request.headers.get('authorization', '').split(' ', 1)
                user, supplied = base64.b64decode(token, validate=True).decode().split(':', 1)
                valid = scheme.lower() == 'basic' and hmac.compare_digest(supplied.encode(), password.encode()) and user == 'zhilian'
            except (ValueError, UnicodeError):
                pass
            if not valid:
                return JSONResponse({'detail': '请使用用户名 zhilian 与部署密码登录'}, status_code=401,
                                    headers={'WWW-Authenticate': 'Basic realm="Zhilian", charset="UTF-8"'})
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': '拒绝跨站修改请求'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing(request, exc):
        return JSONResponse({'detail': '项目或文件不存在'}, status_code=404)

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'version': __version__, 'model': config(),
                'model_mode': mode(), 'model_mode_label': mode_label(),
                'ocr': ocr.config(),
                'local_reranker': reranker.status(),
                'quota': quota.config(),
                'auto_export': os.getenv('ZHILIAN_AUTO_EXPORT', '').strip().lower() in ('1', 'true'),
                'password_protected': bool(os.getenv('ZHILIAN_ACCESS_PASSWORD')),
                'pdf_backends': available_docx_backends()}

    @app.get('/downloads/{filename}')
    def public_download(filename: str):
        if filename not in PUBLIC_DOWNLOADS:
            raise HTTPException(status_code=404, detail='下载文件不存在')
        path = (DOWNLOAD_ROOT / filename).resolve()
        if DOWNLOAD_ROOT not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail='下载文件不存在')
        return FileResponse(path, filename=filename,
                            headers={'Cache-Control': 'public, max-age=86400'})

    @app.get('/api/quota')
    def quota_state():
        """只读额度查询：演示时可直接展示"还剩多少次模型调用"。"""
        return {**quota.config(), **quota.snapshot()}

    @app.get('/api/projects')
    def projects():
        return store.list()

    @app.post('/api/projects/demo')
    def demo(request: Request):
        with tempfile.TemporaryDirectory() as tmp:
            # 线上演示显式请求语义改写夹具；默认 API 保持兼容，便于离线
            # 集成测试和第三方调用继续使用稳定的确定性样例。
            semantic_demo = request.query_params.get('semantic', '').lower() in ('1', 'true', 'yes', 'on')
            return store.create('销售分析 · 演示项目', create_demo(tmp, semantic_demo=semantic_demo), demo=True)

    @app.get('/api/template')
    def template():
        folder = store.root / '_templates'
        if not (folder / '业务数据.xlsx').exists():
            create_demo(folder)
        return FileResponse(folder / '业务数据.xlsx', filename='知链事实表模板.xlsx')

    @app.post('/api/projects')
    async def upload(name: str = Form('新项目'), files: list[UploadFile] = File(...)):
        if len(files) > 10:
            raise ValueError('最多上传10份文件')
        paths, total, names = [], 0, set()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                for f in files:
                    filename = (f.filename or '').replace('\\', '/').split('/')[-1]
                    if not filename or filename in names or Path(filename).suffix.lower() not in ('.xlsx', '.docx', '.pptx', *ocr.IMAGE_EXTENSIONS):
                        raise ValueError('文件名重复或类型不受支持，仅接受xlsx、docx、pptx或图片文件(png/jpg/gif/webp/bmp/tiff)')
                    if len(filename) > 150 or any(c in filename for c in '<>:"|?*'):
                        raise ValueError('文件名无效或过长')
                    names.add(filename)
                    path = Path(tmp) / filename
                    with path.open('wb') as out:
                        while chunk := await f.read(1024 * 1024):
                            total += len(chunk)
                            if total > 30 * 1024 * 1024:
                                raise ValueError('每批上传总大小不超过30MB')
                            out.write(chunk)
                    paths.append(path)
                return await run_in_threadpool(store.create, name, paths)
            finally:
                for f in files:
                    await f.close()

    @app.post('/api/projects/from-pdf')
    async def upload_pdf_project(name: str = Form('PDF 导入项目'), subject: str = Form(''), file: UploadFile = File(...)):
        """Convert one PDF to Word/Excel sources, then create an Office project.

        The original PDF is retained for provenance but is never treated as an
        editable project document.  PDFs with no usable structured facts are
        rejected with a message directing the user to review the generated
        intermediate files first.
        """
        filename = (file.filename or '').replace('\\', '/').split('/')[-1]
        if Path(filename).suffix.lower() != '.pdf':
            raise ValueError('请选择 .pdf 文件')
        if len(filename) > 150 or any(c in filename for c in '<>:"|?*'):
            raise ValueError('PDF 文件名无效或过长')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / filename
            total = 0
            try:
                with path.open('wb') as out:
                    while chunk := await file.read(1024 * 1024):
                        total += len(chunk)
                        if total > 100 * 1024 * 1024:
                            raise ValueError('PDF 不超过100MB')
                        out.write(chunk)
                return await run_in_threadpool(store.create_from_pdf, name, path, subject.strip() or None)
            finally:
                await file.close()

    @app.post('/api/projects/{wid}/documents')
    async def append_documents(wid: str, revision: int = Form(...), files: list[UploadFile] = File(...),
                               continue_on_error: bool = Form(False)):
        """Append a bounded batch of Word/PPT/image files to an existing project.

        The Excel source remains the project's single source of truth and is
        deliberately rejected here.  Clients can upload dozens of documents by
        repeating this endpoint; each request is committed as one transaction.
        """
        max_batch = int(os.getenv('ZHILIAN_MAX_APPEND_BATCH', '50'))
        if len(files) > max_batch:
            raise ValueError(f'单批最多追加{max_batch}份文档，请分批上传')
        paths, total, names = [], 0, set()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                for f in files:
                    filename = (f.filename or '').replace('\\', '/').split('/')[-1]
                    suffix = Path(filename).suffix.lower()
                    if not filename or filename in names or suffix not in ('.docx', '.pptx', *ocr.IMAGE_EXTENSIONS):
                        raise ValueError('追加批次只接受docx、pptx或图片文件，不允许再次上传xlsx')
                    if len(filename) > 150 or any(c in filename for c in '<>:"|?*'):
                        raise ValueError('文件名无效或过长')
                    names.add(filename)
                    path = Path(tmp) / filename
                    with path.open('wb') as out:
                        while chunk := await f.read(1024 * 1024):
                            total += len(chunk)
                            if total > 100 * 1024 * 1024:
                                raise ValueError('每批追加总大小不超过100MB')
                            out.write(chunk)
                    paths.append(path)
                if not continue_on_error:
                    return await run_in_threadpool(store.append_documents, wid, revision, paths)
                # Tolerant mode commits each file as its own transaction.  A
                # corrupt or duplicate document is reported while successful
                # files remain available and can be reviewed immediately.
                current_revision = revision
                accepted, rejected = [], []
                for path in paths:
                    try:
                        result = await run_in_threadpool(store.append_documents, wid, current_revision, [path])
                        current_revision = result['revision']
                        accepted.append({'name': path.name, 'batch': result.get('batch')})
                    except Exception as exc:
                        rejected.append({'name': path.name, 'reason': str(exc) or type(exc).__name__})
                with store.lock:
                    state = store.public(store.read(wid))
                state['batch'] = {
                    'status': 'partial' if rejected and accepted else ('failed' if rejected else 'done'),
                    'count': len(accepted), 'rejected': len(rejected),
                    'revision': current_revision,
                }
                state['batch_results'] = {'accepted': accepted, 'rejected': rejected}
                return state
            finally:
                for f in files:
                    await f.close()

    @app.get('/api/projects/{wid}/batches/{batch_id}')
    def batch_status(wid: str, batch_id: str):
        return store.batch(wid, batch_id)

    @app.get('/api/projects/{wid}')
    def get_project(wid: str):
        with store.lock:
            return store.public(store.read(wid))

    @app.delete('/api/projects/{wid}')
    def delete_project(wid: str):
        """Delete a project selected from 我的项目 after client confirmation."""
        return store.delete(wid)

    @app.get('/api/projects/{wid}/report')
    def report(wid: str):
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, ws['revision'])
            return {'revision': ws['revision'], 'report': build_report(ws)}

    @app.post('/api/projects/{wid}/links')
    def confirm(wid: str, body: LinksRequest):
        return store.confirm(wid, body.revision, [i.model_dump() for i in body.links])

    @app.post('/api/projects/{wid}/facts')
    def change(wid: str, body: ChangeRequest):
        return store.change(wid, body.revision, body.values)

    @app.get('/api/projects/{wid}/derivations')
    def list_derivations(wid: str):
        with store.lock:
            ws = store.read(wid)
            derivations = store.derivations_of(ws)
            return {'revision': ws['revision'], 'derivations': derivations.records,
                    'order': derivations.order, 'cycles': derivations.cycles,
                    'graph': ws.get('graph') or {}}

    @app.post('/api/projects/{wid}/derivations')
    def set_derivations(wid: str, body: DerivationsRequest):
        return store.set_derivations(wid, body.revision, [i.model_dump() for i in body.derivations])

    @app.delete('/api/projects/{wid}/derivations/{rid}')
    def remove_derivation(wid: str, rid: str, revision: int):
        return store.remove_derivation(wid, revision, rid)

    async def uploaded_source(wid, revision, file, operation):
        try:
            if Path(file.filename or '').suffix.lower() != '.xlsx':
                raise ValueError('请选择保存后的 .xlsx 文件')
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'updated.xlsx'
                total = 0
                with path.open('wb') as out:
                    while chunk := await file.read(1024 * 1024):
                        total += len(chunk)
                        if total > 30 * 1024 * 1024:
                            raise ValueError('更新表不超过30MB')
                        out.write(chunk)
                return await run_in_threadpool(operation, wid, revision, path)
        finally:
            await file.close()

    @app.post('/api/projects/{wid}/source/preview')
    async def preview_source(wid: str, revision: int = Form(...), file: UploadFile = File(...)):
        return await uploaded_source(wid, revision, file, store.preview_source)

    @app.post('/api/projects/{wid}/source')
    async def import_source(wid: str, revision: int = Form(...), file: UploadFile = File(...)):
        return await uploaded_source(wid, revision, file, store.import_source)

    @app.post('/api/projects/{wid}/repair')
    def repair(wid: str, body: RepairRequest):
        return store.repair(wid, body.revision, body.claim_ids)

    @app.post('/api/projects/{wid}/undo')
    def undo(wid: str, body: RevisionRequest):
        return store.undo(wid, body.revision)

    @app.post('/api/projects/{wid}/suggest')
    def suggestions(wid: str, body: RevisionRequest):
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, body.revision)
            selected = mode(ws)
        with using_mode(selected):
            if not remote_allowed():
                raise ValueError(f'当前为 {selected} 模式，不允许调用远程 API')
            eligible = [c for c in ws['claims'] if not c['confirmed'] and c['kind'] != 'chart']
            if eligible and config()['enabled'] and len(ws['facts']) <= 150:
                gate = quota.check()
                if not gate['allowed']:
                    raise ValueError(gate['reason'])
                quota.consume()
            result = suggest_links(ws['claims'], ws['facts'])
            call_metrics = consume_last_call_metrics()
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, body.revision)
            ws['suggestions'] = result
            store.append_model_call(ws, call_metrics, node='link_agent', mode_value=selected,
                                    candidates_returned=len(result))
            ws['revision'] += 1
            ws['audit'].append({'time': now(), 'event': '模型建议', 'detail': f'模型 {config()["model"]} 提出{len(result)}项建议，尚未确认'})
            store.write(ws)
            return store.public(ws)

    @app.post('/api/projects/{wid}/diagnosis/explain')
    def diagnosis_explain(wid: str, body: RevisionRequest):
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, body.revision)
            records = store.public(ws)['diagnosis']
            selected = mode(ws)
        with using_mode(selected):
            can_call = remote_allowed() and config()['enabled'] and bool(records)
            if can_call:
                gate = quota.check()
                if not gate['allowed']:
                    raise ValueError(gate['reason'])
                quota.consume()
            explanations = explain_diagnosis(records)
            call_metrics = consume_last_call_metrics()
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, body.revision)
            ws['diagnosis_explanations'] = explanations
            store.append_model_call(ws, call_metrics, node='diagnosis_agent', mode_value=selected,
                                    candidates_returned=len(explanations))
            ws['revision'] += 1
            ws['audit'].append({'time': now(), 'event': '诊断解释',
                                'revision': ws['revision'],
                                'detail': (f'模型 {config()["model"]} 返回{len(explanations)}项解释；未返回的项使用确定性模板'
                                           if can_call
                                           else ('未配置 DeepSeek，使用确定性模板，未调用模型'
                                                 if not config()['enabled']
                                                 else f'当前为 {selected} 模式，使用确定性模板，未调用远程模型'))})
            store.write(ws)
            return store.public(ws)

    @app.post('/api/projects/{wid}/ocr/run')
    def ocr_run(wid: str, body: OcrRunRequest):
        return store.ocr_run(wid, body.revision, body.image_ids)

    @app.post('/api/projects/{wid}/ocr/confirm')
    def ocr_confirm(wid: str, body: OcrConfirmRequest):
        return store.ocr_confirm(wid, body.revision, [i.model_dump() for i in body.items])

    @app.get('/api/projects/{wid}/images/{iid}')
    def image_download(wid: str, iid: str):
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, ws['revision'])
            image = next((i for i in ws.get('images', []) if i['id'] == iid), None)
            if not image:
                raise HTTPException(404, '图片不存在')
            path = store.image_path(ws, image)
            media_type = image['mime'] if image['mime'] in ocr.MIME_EXT else 'application/octet-stream'
            return FileResponse(path, media_type=media_type,
                                headers={'Content-Security-Policy': "default-src 'none'; sandbox"})

    @app.get('/api/projects/{wid}/files/{fid}')
    def download(wid: str, fid: str):
        ws = store.read(wid)
        store.verify(ws, ws['revision'])
        d = next((d for d in ws['documents'] if d['id'] == fid), None)
        if not d:
            raise HTTPException(404, '文件不存在')
        return FileResponse(store.folder(wid) / ws['generation'] / d['stored_name'], filename=d['name'])

    @app.get('/api/projects/{wid}/pdf-origins/{index}')
    def download_pdf_origin(wid: str, index: int):
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, ws['revision'])
            origins = ws.get('pdf_origins', [])
            if index < 0 or index >= len(origins):
                raise HTTPException(404, '原始 PDF 不存在')
            origin = origins[index]
            path = store.folder(wid) / ws['generation'] / origin['stored_name']
            if not path.is_file() or digest(path) != origin.get('sha256'):
                raise HTTPException(409, '原始 PDF 校验失败，请重新导入')
            return FileResponse(path, filename=origin['name'], media_type='application/pdf')

    @app.post('/api/projects/{wid}/pdf-revised')
    def export_revised_pdf(wid: str, body: RevisionRequest):
        """Generate an independent PDF from the current Word revision."""
        return store.export_revised_pdf(wid, body.revision)

    @app.get('/api/projects/{wid}/pdf-revisions/{index}')
    def download_revised_pdf(wid: str, index: int):
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, ws['revision'])
            items = ws.get('pdf_revisions', [])
            if index < 0 or index >= len(items):
                raise HTTPException(404, '修订版 PDF 不存在')
            item = items[index]
            verification = item.get('verification') or {}
            if verification.get('status') != 'passed':
                raise HTTPException(409, '修订版 PDF 未通过严格视觉一致性验收，请先查看转换清单和差异页')
            path = store.folder(wid) / ws['generation'] / item.get('stored_name', '')
            if not path.is_file():
                raise HTTPException(404, '修订版 PDF 不存在')
            return FileResponse(path, filename=item.get('name', path.name), media_type='application/pdf')

    @app.get('/api/projects/{wid}/pdf-revisions/{index}/manifest')
    def download_revised_manifest(wid: str, index: int):
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, ws['revision'])
            items = ws.get('pdf_revisions', [])
            if index < 0 or index >= len(items):
                raise HTTPException(404, '修订版 PDF 清单不存在')
            item = items[index]
            path = store.folder(wid) / ws['generation'] / item.get('manifest_stored_name', '')
            if not path.is_file():
                raise HTTPException(404, '修订版 PDF 清单不存在')
            return FileResponse(path, filename=path.name, media_type='application/json')

    @app.post('/api/projects/{wid}/agent/run')
    def agent_run(wid: str, body: RevisionRequest):
        return run_agent(store, wid, body.revision)

    @app.post('/api/projects/{wid}/model-mode')
    def set_model_mode(wid: str, body: ModelModeRequest):
        return store.set_model_mode(wid, body.revision, body.mode)

    @app.post('/api/projects/{wid}/agent/decide')
    def agent_decide(wid: str, body: AgentDecisionRequest):
        return decide(store, wid, body.revision, body.decisions)

    @app.get('/api/projects/{wid}/agent/status')
    def get_agent_status(wid: str):
        return agent_status(store, wid)

    @app.post('/api/projects/{wid}/review/cross-document')
    def cross_document_review(wid: str, body: RevisionRequest):
        return run_cross_document_audit(store, wid, body.revision)

    @app.post('/api/projects/{wid}/review/repair-plan')
    def repair_plan(wid: str, body: RevisionRequest):
        return run_repair_plan(store, wid, body.revision)

    @app.get('/api/projects/{wid}/export')
    def export(wid: str):
        return FileResponse(store.archive(wid), filename='知链成果与核验记录.zip', media_type='application/zip')

    @app.post('/api/projects/{wid}/export-local')
    def export_local(wid: str, body: RevisionRequest):
        with store.lock:
            ws = store.read(wid)
            store.verify(ws, body.revision)
        return store.export_local(wid)

    @app.get('/')
    def index():
        return FileResponse(BASE / 'web' / 'index.html')

    app.mount('/static', StaticFiles(directory=BASE / 'web'), name='static')
    return app


app = create_app()
