'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let workspace = null, currentView = 'overview', filter = 'attention', health = null, graphFact = null, quota = null;
let showAllFacts = false, factSearchTerm = '';
const sourcePages = {files: 0, facts: 0, segments: 0, rules: 0};
let sourceSearch = '', fileSearch = '';
const pageSize = 20;
function pageControls(key, count, size=pageSize) {
  const pages = Math.max(1, Math.ceil(count/size));
  sourcePages[key] = Math.min(sourcePages[key], pages-1);
  return `<div class="page-controls"><span>共 ${count} 项 · ${sourcePages[key]+1} / ${pages} 页</span><button class="icon-button" data-source-page="${key}" data-offset="-1" title="上一页" aria-label="上一页" ${sourcePages[key]===0?'disabled':''}>←</button><button class="icon-button" data-source-page="${key}" data-offset="1" title="下一页" aria-label="下一页" ${sourcePages[key]+1===pages?'disabled':''}>→</button></div>`;
}
const modelModes={rules:'纯规则',local:'规则 + 本地模型',hybrid:'规则 + 本地模型 + API',api:'规则 + API',api_only:'纯 API（研究对照）'};
function currentModelMode(){return workspace?.model_mode||health?.model_mode||'hybrid';}
function remoteModelAllowed(){return !['rules','local'].includes(currentModelMode());}
function modelModeOptions(){return Object.entries(modelModes).map(([value,label])=>`<option value="${value}" ${value===currentModelMode()?'selected':''}>${esc(label)}</option>`).join('');}
function modelModeHint(value=currentModelMode()){
  const descriptions={rules:'只使用规则；关联、诊断解释和图片 OCR 均不会请求远程模型。',local:'规则无法确定时使用已配置的本地模型；低置信结果转人工，不使用 API。',hybrid:'规则先处理；本地模型处理语义难例；仍无法确定时请求 API，可能消耗额度。',api:'规则无法确定时请求 API，跳过本地模型，可能消耗额度。',api_only:'把全部已识别、未确认的非图表正文论断按每批最多 40 条发送 API；不提供规则候选。用于研究对照，可能产生更多调用费用。'};
  let hint=descriptions[value]||descriptions.hybrid;
  if(['local','hybrid'].includes(value)&&!health?.local_reranker?.enabled)hint+=' 本地模型未就绪，'+(value==='local'?'当前只能使用规则和人工确认。':'困难项将转 API 或人工。');
  if(!['rules','local'].includes(value)&&!health?.model?.enabled)hint+=' API 未配置，无法完成远程建议，将保留人工处理。';
  return hint;
}
function renderModelMode(){
  const select=$('modelModeSelect');
  if(select){select.innerHTML=modelModeOptions();select.disabled=!workspace;select.title=workspace?modelModeHint():'导入或打开项目后可选择处理模式';}
  const badge=$('modelBadge');
  if(badge){badge.textContent=remoteModelAllowed()?health?.model?.enabled?'API 已配置'+quotaLabel(quota):'API 未配置':'离线模式';badge.title=modelModeHint()+' '+(remoteModelAllowed()?quotaTitle(quota):'');}
}
async function switchModelMode(selected){
  if(!workspace||!(selected in modelModes)){renderModelMode();return;}
  const result=await post('model-mode',{mode:selected},'正在保存项目处理模式');
  renderModelMode();
  if(result){toast('已切换为 '+modelModes[selected]+'；点击运行智能体重新生成候选');return true;}
  return false;
}
const statusNames = {consistent:'仍成立', inconsistent:'已失效', unverifiable:'无法判断'};
const kindNames = {quote:'数值引用', growth:'增长率', ranking:'排名', threshold:'阈值判断', chart:'图表数据'};
const classNames = {consistent:'green', inconsistent:'red', unverifiable:'amber'};
const categoryClasses = {'通过':'green','结论失效':'red','数据缺失':'amber','来源冲突':'amber','口径不一致':'amber','需人工复核':'amber'};
let toastTimer, pendingUploads = [];
// 句柄仅保存在当前页面内，并按项目隔离，避免同名文件跨项目写回。
const fileHandles = new Map();
let pendingFileHandles = new Map(), writingBack = false;
const factDrafts = new Map();
function drafts() {
  if (!factDrafts.has(workspace.id)) factDrafts.set(workspace.id, new Map());
  return factDrafts.get(workspace.id);
}
function trackDraft(input) {
  const fact = workspace.facts.find(f => f.id === input.dataset.fact);
  const value = input.value;
  if (value.trim() !== '' && Number(value) === fact.value) drafts().delete(fact.id);
  else drafts().set(fact.id, value);
  renderDraftStatus();
}
function renderDraftStatus() {
  const count = drafts().size;
  const status = $('factDraftStatus');
  if (status) status.textContent = count ? `${count} 项数值尚未应用，切换视图时会保留` : '当前显示已保存的数据';
  const discard = $('discardDrafts');
  if (discard) discard.classList.toggle('hidden', !count);
  $('saveFacts').disabled = !count;
  document.querySelectorAll('[data-fact]').forEach(input => input.closest('.fact-row').classList.toggle('edited', drafts().has(input.dataset.fact)));
}
function renderUploadFiles() {
  const list = $('uploadFileList');
  if (!list) return;
  const total = pendingUploads.reduce((sum, file) => sum + file.size, 0);
  list.innerHTML = pendingUploads.map((file, index) => {
    const ext = file.name.split('.').pop().toLowerCase();
    const type = ext === 'xlsx' ? 'Excel' : ext === 'docx' ? 'Word' : 'PPT';
    return `<div class="upload-file-row"><span class="file-icon ${ext === 'docx' ? 'docx' : ext === 'pptx' ? 'pptx' : ''}">${ext === 'xlsx' ? 'X' : ext === 'docx' ? 'W' : 'P'}</span><div><strong>${esc(file.name)}</strong><small>${type} · ${(file.size / 1024 / 1024).toFixed(2)} MB</small></div><button type="button" class="icon-button upload-remove" data-upload-remove="${index}" aria-label="移除 ${esc(file.name)}">×</button></div>`;
  }).join('');
  const status = $('uploadFileStatus');
  if (status) status.textContent = pendingUploads.length ? `已加入 ${pendingUploads.length} 份文件 · ${(total / 1024 / 1024).toFixed(2)} / 30 MB` : '尚未加入文件';
  const submit = $('uploadSubmit');
  if (submit) submit.disabled = pendingUploads.length < 2 || pendingUploads.length > 10 || total > 30 * 1024 * 1024;
  list.querySelectorAll('[data-upload-remove]').forEach(button => button.onclick = () => {
    const index=Number(button.dataset.uploadRemove);
    pendingFileHandles.delete(pendingUploads[index].name);
    pendingUploads.splice(index, 1);
    renderUploadFiles();
  });
}
function statusBadge(c, r=resultOf(c)) {
  return `<span class="badge ${c.confirmed ? classNames[r.status] : 'amber'}">${c.confirmed ? esc(statusNames[r.status]) : '待确认 · ' + esc(statusNames[r.status])}</span>`;
}

function toast(message, error=false) {
  $('toast').textContent = message; $('toast').className = 'show' + (error ? ' error' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').className='', 5000);
}
async function api(path, options={}) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error(response.ok?'服务器返回格式异常，请刷新后重试':`操作失败（HTTP ${response.status}），请查看服务端日志；这不表示网络未连接`); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求参数不正确，请刷新后重试');
  return data;
}
function apiWithUploadProgress(path, body, onUploadProgress, onProcessing) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) onUploadProgress(Math.round(event.loaded / event.total * 100));
    };
    xhr.upload.onload = onProcessing;
    xhr.onload = () => {
      let data;
      try { data = JSON.parse(xhr.responseText); }
      catch { reject(new Error(xhr.status >= 200 && xhr.status < 300 ? '服务器返回格式异常，请刷新后重试' : `操作失败（HTTP ${xhr.status}），请查看服务端日志`)); return; }
      if (xhr.status < 200 || xhr.status >= 300) reject(new Error(typeof data.detail === 'string' ? data.detail : 'PDF 导入失败，请检查文件后重试'));
      else resolve(data);
    };
    xhr.onerror = () => reject(new Error('网络连接中断；请检查网络后重试'));
    xhr.send(body);
  });
}
function updateBusyProgress(label, percent, detail, indeterminate=false) {
  busyProgressManaged=true;
  $('busyText').textContent = label;
  $('busyProgress').classList.remove('hidden');
  $('busyDetail').classList.remove('hidden');
  $('busyDetail').textContent = detail;
  const fill = $('busyProgressFill');
  fill.classList.toggle('indeterminate', indeterminate);
  fill.style.width = indeterminate ? '' : `${percent}%`;
}
let busyHintTimer = null, busyProgressManaged = false;
async function busy(label, action) {
  busyProgressManaged=false;
  $('busyProgress').classList.add('hidden');
  $('busyDetail').classList.add('hidden');
  $('busyProgressFill').classList.remove('indeterminate');
  $('busyText').textContent=label; $('busy').classList.remove('hidden');
  const started=Date.now();
  busyHintTimer=setTimeout(()=>{
    if(busyProgressManaged)return;
    updateBusyProgress(label,0,`仍在处理中 · 已用 ${Math.floor((Date.now()-started)/1000)} 秒`,true);
    busyProgressManaged=false;
    busyHintTimer=setInterval(()=>{
      if(!busyProgressManaged)$('busyDetail').textContent=`仍在处理中 · 已用 ${Math.floor((Date.now()-started)/1000)} 秒`;
    },1000);
  },1500);
  try { return await action(); } catch(e) { toast(e.message, true); return null; }
  finally {
    clearTimeout(busyHintTimer); clearInterval(busyHintTimer); busyHintTimer=null;
    $('busy').classList.add('hidden');
  }
}
async function post(action, payload, label) {
  const result = await busy(label, () => api(`/api/projects/${workspace.id}/${action}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:workspace.revision,...payload})
  }));
  if (result) { workspace=result; render(); await refreshProjects(); }
  return result;
}
function modal(title, html) { $('modalTitle').textContent=title; $('modalContent').innerHTML=html; if (!$('modal').open) $('modal').showModal(); }
function closeModal() { $('modal').close(); }
function localTime(s) { return new Date(s).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
function fileOf(c) { return workspace.documents.find(d=>d.id===c.file_id); }
function resultOf(c) { return workspace.checks.find(r=>r.claim_id===c.id); }
function factName(f) { return `${f.subject==='总计'?'':f.subject+' · '}${f.period}${f.metric}`; }
function downloadLinks() {
  return workspace.documents.map(d=>`<a class="button secondary small" href="${esc(d.download_url)}" download>↓ ${d.kind==='xlsx'?'Excel':d.kind==='docx'?'Word':'PPT'} · ${esc(d.name)}</a>`).join('');
}
async function writeBackLocal() {
  if(writingBack)return;
  const handles=fileHandles.get(workspace.id), docs=workspace.documents.slice();
  if(!handles?.size){
    toast('本次上传未选择可写回的文件，请重新用“选择文件（支持写回）”导入，或使用浏览器下载',true);
    return [];
  }
  writingBack=true;
  try{
    return await busy('正在写回已选择的本地原文件',async()=>{
      const results=[], allowed=new Set();
      // 点击后先申请写权限，避免等待文件下载消耗浏览器的用户手势。
      for(const d of docs){
        const handle=handles.get(d.name);
        if(!handle){results.push({name:d.name,ok:false,reason:'未记录原文件句柄，请用浏览器下载'});continue;}
        try{
          if(await handle.requestPermission({mode:'readwrite'})!=='granted'){
            results.push({name:d.name,ok:false,reason:'未获得写入权限，请允许写入或使用浏览器下载'});
          }else allowed.add(d.name);
        }catch{
          results.push({name:d.name,ok:false,reason:'无法取得写入权限，请重新导入或使用浏览器下载'});
        }
      }
      for(const d of docs){
        if(!allowed.has(d.name))continue;
        let writable=null, reason='获取修改后文件失败，请重试或使用浏览器下载';
        try{
          const resp=await fetch(d.download_url);
          if(!resp.ok)throw new Error();
          const blob=await resp.blob();
          reason='写回失败，请检查文件是否被占用、权限是否有效，或使用浏览器下载';
          writable=await handles.get(d.name).createWritable();
          await writable.write(blob);
          await writable.close();
          results.push({name:d.name,ok:true});
        }catch{
          if(writable){try{await writable.abort();}catch{}}
          results.push({name:d.name,ok:false,reason});
        }
      }
      const success=results.filter(r=>r.ok).length;
      modal('本地文件写回结果',`<p class="modal-intro">成功 ${success} 份，未写回 ${results.length-success} 份。成功项已覆盖本次导入时选择的原文件；各文件独立写回。</p><ul class="warning-list">${results.map(r=>`<li>${esc(r.name)}：${r.ok?'已写回':esc(r.reason)}</li>`).join('')}</ul><p class="hint">仍可通过“导出成果”下载当前文件或完整成果包。</p>`);
      return results;
    });
  }finally{writingBack=false;}
}
async function exportLocal(auto=false) {
  const projectId=workspace.id, revision=workspace.revision;
  const result=await busy('正在写入本地文件夹',()=>api(`/api/projects/${projectId}/export-local`,{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision})
  }));
  if(!result)return null;
  const current=await api(`/api/projects/${projectId}`);
  if(workspace.id===projectId){workspace=current;render();await refreshProjects();}
  modal(auto?'已自动导出到本地':'已导出到本地文件夹',`<p class="modal-intro">文件已写入本地，可在文件管理器中打开该文件夹。</p><p class="evidence-block"><strong>本地路径</strong><br><code>${esc(result.folder)}</code></p><ul class="warning-list">${result.files.map(file=>`<li>${esc(file.name)}<br><code>${esc(file.path)}</code></li>`).join('')}</ul><p class="hint">本次落盘生成了项目副本，不会覆盖原始上传文件。</p>`);
  return result;
}
function deliveryModal() {
  const s=workspace.summary;
  modal('下载成果',`<p class="modal-intro">项目：${esc(workspace.name)} · 版本 ${workspace.revision}</p>${s.inconsistent?`<p class="hint">还有 ${s.inconsistent} 项内容与当前数据不一致。下方下载的是当前版本，请先完成修复。</p>`:'<p class="modal-intro">已识别论断中没有计算不一致项。点击下方按钮下载当前文件。</p>'}${s.pending?`<p class="hint">仍有 ${s.pending} 项来源待确认，候选计算结果不能代替人工确认。</p>`:''}${s.unverifiable?`<p class="hint">另有 ${s.unverifiable} 项无法判断，需要人工复核。</p>`:''}<div class="delivery-links">${downloadLinks()}</div><div class="modal-actions" style="flex-wrap:wrap">${s.repairable?'<button class="button primary" id="deliveryRepair">预览并修复剩余内容</button>':''}<button class="button secondary" id="deliveryReport">查看变更报告</button><button class="button secondary" id="deliveryLocal">导出到本地文件夹</button>${window.showOpenFilePicker?'<button class="button secondary" id="deliveryWriteBack">直接修改本地文件</button>':''}<a class="button secondary" href="/api/projects/${workspace.id}/export" download>下载完整成果包</a></div>`);
  if($('deliveryRepair'))$('deliveryRepair').onclick=repairModal;
  $('deliveryReport').onclick=()=>{closeModal();switchView('report');};
  $('deliveryLocal').onclick=()=>exportLocal();
  if($('deliveryWriteBack'))$('deliveryWriteBack').onclick=()=>writeBackLocal();
  if(window.showOpenFilePicker)$('modalContent').insertAdjacentHTML('beforeend','<p class="hint">直接修改将覆盖本次导入时授权的原 Excel / Word / PPT。句柄仅在当前页面有效，刷新后需重新用支持写回的入口导入。</p>');
}
function afterDataUpdate() {
  if(workspace.summary.repairable) repairModal();
  else if(workspace.claims.some(c=>c.source!=='ocr'&&resultOf(c).status==='inconsistent')) {
    modal('数据已更新，成果尚未修复',`<p class="modal-intro">发现 ${workspace.summary.inconsistent} 项不一致内容，需要先确认对应来源，才能生成修改后的 Word / PPT。</p><div class="modal-actions"><button class="button primary" id="nextConfirm">审阅候选来源</button></div>`);
    $('nextConfirm').onclick=confirmAllModal;
  } else deliveryModal();
}

async function refreshProjects() {
  const projects=await api('/api/projects');
  $('projects').innerHTML=projects.map(p=>`<div class="project-item"><button class="project-link ${workspace?.id===p.id?'active':''}" data-project="${p.id}">${esc(p.name)}</button><button class="project-delete" data-project-delete="${p.id}" data-project-name="${esc(p.name)}" title="删除项目" aria-label="删除 ${esc(p.name)}">×</button></div>`).join('');
  $('projects').querySelectorAll('[data-project-delete]').forEach(button=>button.onclick=async event=>{
    event.stopPropagation();
    const id=button.dataset.projectDelete, name=button.dataset.projectName||'这个项目';
    if(!window.confirm(`确定删除“${name}”吗？\n项目文件、历史版本和核验记录都会永久删除。`))return;
    const result=await busy('正在删除项目',()=>api(`/api/projects/${id}`,{method:'DELETE'}));
    if(!result)return;
    if(workspace?.id===id){workspace=null;graphFact=null;localStorage.removeItem('zhilian-project');render();}
    await refreshProjects();
    toast(`已删除项目：${name}`);
  });
  return projects;
}
async function loadProject(id) {
  const w=await busy('正在读取项目',()=>api('/api/projects/'+id));
  if(w){if(workspace?.id!==id){sourceSearch='';fileSearch='';Object.keys(sourcePages).forEach(k=>sourcePages[k]=0);}workspace=w;graphFact=null;localStorage.setItem('zhilian-project',w.id);render();await refreshProjects();}
}
function switchView(view) {
  currentView=view;
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  const labels={overview:'一致性工作台',diagnosis:'异常诊断',ocr:'图片文字',sources:'来源与文件',graph:'证据依赖图',history:'变更记录',report:'变更报告',models:'模型调用'};
  $('breadcrumb').textContent=labels[view];
  if(workspace)render();
}
function renderWorkspacePulse(w) {
  const el = $('workspacePulse');
  if (!el) return;
  const s = w.summary || {};
  const pending = Number(s.pending || 0);
  const repairable = Number(s.repairable || 0);
  const inconsistent = Number(s.inconsistent || 0);
  const unverifiable = Number(s.unverifiable || 0);
  let state = 'ready', title = '项目已就绪', copy = '先确认来源，再处理数据变化。';
  let action = `<button class="button primary small" data-pulse-action="agent">运行智能体</button>`;
  if (pending) {
    state = 'attention'; title = `${pending} 项来源等待确认`; copy = '模型和规则只提供候选，确认后才会进入确定性核验。';
    action = `<button class="button primary small" data-pulse-action="confirm">审阅候选来源</button>`;
  } else if (repairable || inconsistent) {
    state = 'repair'; title = `${inconsistent || repairable} 项结论需要处理`; copy = '系统已定位受影响内容，预览后只修改失效论断。';
    action = `<button class="button primary small" data-pulse-action="repair">预览修复</button>`;
  } else if (unverifiable) {
    state = 'review'; title = `${unverifiable} 项需要人工复核`; copy = '这些内容超出当前确定性支持范围，已被单独保留。';
    action = `<button class="button secondary small" data-pulse-action="diagnosis">查看诊断</button>`;
  } else if (w.last_repair) {
    state = 'complete'; title = '本轮核验已完成'; copy = '文件已重新读取复核，可以导出成果和变更报告。';
    action = `<button class="button primary small" data-pulse-action="export">导出成果</button>`;
  }
  el.innerHTML = `<div class="pulse-mark ${state}"><span></span></div><div class="pulse-copy"><strong>${esc(title)}</strong><span>${esc(copy)}</span></div><div class="pulse-meta"><span>${s.documents || 0} 文件</span><span>${s.claims || 0} 条论断</span><span>版本 ${w.revision}</span></div>${action}`;
  el.querySelector('[data-pulse-action]')?.addEventListener('click', e => {
    const action = e.currentTarget.dataset.pulseAction;
    if (action === 'agent') agentModal();
    if (action === 'confirm') confirmAllModal();
    if (action === 'repair') repairModal();
    if (action === 'diagnosis') switchView('diagnosis');
    if (action === 'export') deliveryModal();
  });
  const routeSummary = $('routingSummary'), a = w.agent;
  if (routeSummary) {
    routeSummary.classList.toggle('hidden', !a?.model_routing);
    if (a?.model_routing) {
      const routes = Object.values(a.claim_routes || {});
      const labels = {rules:'规则', local:'本地模型', api:'API', human:'人工复核'};
      const selected = a.model_profile || {};
      const model = selected.selected === 'annual' ? '年报 BERT' : (selected.model_name || '通用本地模型');
      const stale = a.revision !== w.revision;
      routeSummary.innerHTML = `<span class="route-profile">${a.document_profile?.profile === 'annual' ? '年报资料' : '通用资料'} · ${esc(model)}${selected.fallback ? ' · 已回退' : ''}${stale ? ' · 历史任务' : ''}</span><div class="routing-strip">${Object.entries(labels).map(([key,label])=>`<span><b>${routes.filter(r=>r.source===key).length}</b> ${label}</span>`).join('')}</div><button class="link-button" id="openRoutingLedger">查看调用依据 →</button>`;
      $('openRoutingLedger').onclick=()=>switchView('models');
    }
  }
}
function renderFactInputs() {
  const facts = workspace?.facts || [], container = $('factInputs');
  if (!container) return;
  const term = factSearchTerm.trim().toLowerCase();
  const matches = facts.filter(f => !term || [f.subject, f.metric, f.period, f.scope, f.id].some(v => String(v ?? '').toLowerCase().includes(term)));
  const attentionIds = new Set((workspace?.claims || []).filter(c => {
    const r = resultOf(c); return !c.confirmed || r?.status === 'inconsistent' || r?.status === 'unverifiable';
  }).flatMap(c => c.refs || []));
  const priority = matches.filter(f => drafts().has(f.id) || attentionIds.has(f.id));
  const visible = showAllFacts || term ? matches : [...priority, ...matches.filter(f => !priority.includes(f))].slice(0, 5);
  const label = $('factCountLabel');
  if (label) label.textContent = term ? `找到 ${matches.length} / ${facts.length} 条` : `共 ${facts.length} 条 · 显示 ${visible.length} 条`;
  const toggle = $('toggleFacts');
  if (toggle) toggle.textContent = showAllFacts ? '收起' : (facts.length > visible.length ? '查看全部' : '已全部显示');
  container.innerHTML = visible.length ? visible.map(f => `<div class="fact-row"><label for="fact-${esc(f.id)}">${esc(factName(f))}<small>${esc(f.sheet+'!'+f.cell)}</small></label><div class="fact-field"><input id="fact-${esc(f.id)}" data-fact="${esc(f.id)}" type="number" step="any" value="${f.value??''}" aria-label="${esc(factName(f))}"><span>${esc(f.unit)}</span></div></div>`).join('') : '<div class="empty-list compact-empty">没有匹配的事实数据</div>';
  for (const input of container.querySelectorAll('[data-fact]')) {
    if (drafts().has(input.dataset.fact)) input.value = drafts().get(input.dataset.fact);
    input.oninput = () => trackDraft(input);
  }
}
function render() {
  $('welcome').classList.toggle('hidden',!!workspace);$('workspace').classList.toggle('hidden',!workspace);
  renderModelMode();
  if(!workspace)return;
  const w=workspace,s=w.summary;
  const textIssues=w.claims.filter(c=>c.source!=='ocr'&&resultOf(c).status==='inconsistent').length;
  for (const f of w.facts) if (drafts().has(f.id) && drafts().get(f.id).trim() !== '' && Number(drafts().get(f.id)) === f.value) drafts().delete(f.id);
  $('projectName').textContent=w.name;
  $('workspaceEyebrow').textContent=w.demo?'DEMO PROJECT · 模拟数据':'PROJECT WORKSPACE';
  $('projectSubtitle').textContent=`${s.documents} 份文件 · ${s.facts} 条来源事实 · 版本 ${w.revision} · 数据与成果均保留历史版本`;
  renderWorkspacePulse(w);
  ['overview','diagnosis','ocr','sources','graph','history','report','models'].forEach(v=>$(v+'View').classList.toggle('hidden',currentView!==v));
  $('undoButton').disabled=!w.history.length;
  $('agentButton').textContent=w.agent?.phase==='awaiting_decision'&&w.agent.revision===w.revision?'继续智能体待办':'运行智能体';
  const verified=w.claims.filter(c=>c.confirmed&&resultOf(c).status==='consistent').length;
  const stats=[['已识别论断',s.claims,'','逐项核验已识别的受支持表达','◇'],['已确认且成立',verified,'success','来源已确认，计算结果一致','✓'],['计算不一致',s.inconsistent,'danger','确认来源后可预览局部修复','↗'],['来源待确认',s.pending,'','候选结果须经人工核对','◷']];
  $('stats').innerHTML=stats.map(([label,value,cls,note,icon])=>`<div class="stat"><div class="stat-top">${label}<span class="stat-icon">${icon}</span></div><div class="stat-value ${cls}">${value}<span style="font-size:11px;color:#9bab9d;font-weight:400;margin-left:7px">项</span></div><div class="stat-bottom">${note}</div></div>`).join('');
  const chartTotal = Math.max(Number(s.claims || 0), 1);
  const confirmedBad = w.claims.filter(c => c.confirmed && resultOf(c)?.status === 'inconsistent').length;
  const confirmedUnknown = w.claims.filter(c => c.confirmed && resultOf(c)?.status === 'unverifiable').length;
  const chartItems = [['已确认成立', verified, 'ok'], ['来源待确认', s.pending || 0, 'pending'], ['已确认失效', confirmedBad, 'bad'], ['已确认需复核', confirmedUnknown, 'unknown']];
  $('statusChart').innerHTML = `<div class="status-chart-head"><span>当前处理进度</span><b>${verified} / ${s.claims || 0} 条已确认成立</b></div><div class="status-bar">${chartItems.map(([,value,cls]) => `<span class="status-segment ${cls}" style="width:${value / chartTotal * 100}%" title="${value} 项"></span>`).join('')}</div><div class="status-legend">${chartItems.map(([label,value,cls]) => `<span><i class="legend-dot ${cls}"></i>${label} ${value}</span>`).join('')}</div>`;
  $('successBanner').innerHTML=textIssues?`<div class="banner">${textIssues} 项正文内容尚未更新到 Word / PPT。${s.repairable?'请预览并确认修复。':'请先确认来源关联。'} <button class="button primary small" id="continueRepair">${s.repairable?'预览修复并生成文件':'审阅候选来源'}</button></div>`:w.last_repair?`<div class="banner">✓ 已修复 ${w.last_repair.count} 项并重新验证。${s.pending?`另有 ${s.pending} 项来源待确认。`:''}<div class="delivery-links">${downloadLinks()}</div></div>`:'';
  if(w.claims.some(c=>c.source==='ocr'))$('successBanner').insertAdjacentHTML('beforeend','<p class="hint">OCR 图片论断只读：候选来源不能确认关联，图片异常请在原文件中人工处理。智能体只处理正文和原生图表。</p>');
  if($('continueRepair'))$('continueRepair').onclick=s.repairable?repairModal:confirmAllModal;
  const flow = {
    sources: {done: true, active: false},
    confirm: {done: s.pending === 0, active: s.pending > 0},
    verify: {done: s.pending === 0 && !s.inconsistent && !s.unverifiable && !s.repairable, active: s.pending === 0 && !s.repairable && (s.inconsistent > 0 || s.unverifiable > 0)},
    repair: {done: !!w.last_repair && !s.repairable, active: s.pending === 0 && s.repairable > 0},
  };
  const step = (key, number, label) => {
    const state = flow[key];
    const cls = `${state.done ? 'done' : ''} ${state.active ? 'active' : ''}`.trim();
    const icon = state.done ? '✓' : number;
    const current = state.active ? ' aria-current="step"' : '';
    return `<button class="step ${cls}" data-flow-step="${key}"${current} title="${state.active ? '当前步骤' : state.done ? '已完成' : '待处理'}"><i>${icon}</i> ${label}${state.active ? '<small>当前</small>' : ''}</button>`;
  };
  $('workflow').innerHTML=`${step('sources', 1, '导入文件')}<span class="rule"></span>${step('confirm', 2, '确认来源')}<span class="rule"></span>${step('verify', 3, '验证结论')}<span class="rule"></span>${step('repair', 4, '修复与导出')}`;
  $('workflow').querySelectorAll('[data-flow-step]').forEach(button => button.onclick=()=>{
    const step=button.dataset.flowStep;
    if(step==='sources')switchView('sources');
    if(step==='confirm')confirmAllModal();
    if(step==='verify')switchView('overview');
    if(step==='repair')w.summary.repairable?repairModal():deliveryModal();
  });
  const currentMode=currentModelMode();
  const remoteAllowed=remoteModelAllowed();
  $('suggestButton').disabled=!health?.model.enabled||!remoteAllowed;
  $('suggestButton').title=!remoteAllowed?`当前为 ${currentMode} 模式，不调用远程 API`:health?.model.enabled?'将待确认论断及事实元数据发送给 DeepSeek':'DeepSeek 尚未开通，请联系管理员或继续人工确认';
  $('filters').querySelectorAll('button').forEach(b=>b.classList.toggle('selected',b.dataset.filter===filter));
  const claims=w.claims.filter(c=>filter==='all'||(filter==='attention'?(!c.confirmed||['inconsistent','unverifiable'].includes(resultOf(c).status)):(filter==='pending'?!c.confirmed:resultOf(c).status===filter)));
  $('claimCount').textContent=`显示 ${claims.length} 项 · 仅覆盖已识别的论断`;
  $('claims').innerHTML=claims.length?claims.map(c=>{
    const r=resultOf(c),f=fileOf(c),names=c.refs.map(id=>w.facts.find(f=>f.id===id)).filter(Boolean).map(f=>`${factName(f)} ${f.value??'缺失'}${f.unit}`);
    return `<article class="claim-card ${r.status==='inconsistent'?'problem':''}"><div class="claim-top"><span class="file-tag">${f.kind==='docx'?'W':f.kind==='pptx'?'P':'X'} · ${esc(kindNames[c.kind])}</span><span class="place" title="${esc(f.name+' · '+c.label)}">${esc(c.label)}</span><span class="badge ${classNames[r.status]}">${esc(statusNames[r.status])}${c.source==='ocr'?' · 候选核验':c.confirmed?'':' · 待确认'}</span></div><div class="claim-text">${c.source==='ocr'?'<span class="badge neutral">OCR · 只读</span> ':''}${esc(c.original)}</div>${r.status==='inconsistent'?`<div class="suggested"><span>建议更新为</span>${esc(r.expected)}</div>`:''}${r.status==='unverifiable'?`<div class="hint">${esc(r.reason)}</div>`:''}<div class="claim-bottom"><span class="evidence-label" title="${esc(names.join('；'))}">${c.confirmed?'✓ 来源已确认':'◷ 候选来源'} · ${esc(names.join(' / ')||'请手动指定')}</span><button class="link-button" data-claim="${c.id}">查看依据 →</button></div></article>`;
  }).join(''):'<div class="empty-list">当前筛选下没有论断。</div>';
  $('confirmAll').disabled=!w.claims.some(c=>c.source!=='ocr'&&c.repairable!==false&&!c.confirmed&&c.refs.length&&resultOf(c).status!=='unverifiable');
  $('reviewRepairs').disabled=!s.repairable;
  $('repairHint').textContent=s.repairable?`${s.repairable} 项已确认来源的结论可修复`:'确认来源后，仅修复已失效的结论';
  renderFactInputs();
  if (!$('factDraftStatus')) $('factInputs').insertAdjacentHTML('afterend','<div class="draft-status" id="factDraftStatus"></div><button class="button text full hidden" id="discardDrafts">撤销未应用输入</button>');
  $('demoChange').classList.toggle('hidden',!w.demo);
  renderDraftStatus();
  const viewRenderers={ocr:renderOcr,diagnosis:renderDiagnosis,sources:renderSources,graph:renderGraph,history:renderHistory,models:renderModelLedger};
  if(viewRenderers[currentView])viewRenderers[currentView]();
  if(currentView==='report')renderReport();
  $('factSearch').oninput=event=>{factSearchTerm=event.target.value;renderFactInputs();renderDraftStatus();};
  $('toggleFacts').onclick=()=>{showAllFacts=!showAllFacts;renderFactInputs();renderDraftStatus();};
  $('discardDrafts').onclick=()=>{drafts().clear();render();toast('已撤销尚未应用的输入');};
}
async function renderReport(){
  const projectId=workspace.id, revision=workspace.revision, view=$('reportView');
  view.innerHTML='<section class="panel"><p class="hint">正在生成变更报告…</p></section>';
  try{
    const result=await api(`/api/projects/${projectId}/report`);
    if(workspace?.id!==projectId||workspace.revision!==revision||currentView!=='report')return;
    view.innerHTML=`<section class="panel"><div class="panel-top"><div><h2>成果与变更报告</h2><p class="hint">版本 ${esc(result.revision)} · 包含数据变更、修改对照、核验依据与人工待办。</p></div><a class="button primary small" href="/api/projects/${projectId}/export" download>下载完整成果包</a></div><pre id="reportText" style="white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.8"></pre></section>`;
    $('reportText').textContent=result.report;
  }catch(e){
    if(workspace?.id!==projectId||workspace.revision!==revision||currentView!=='report')return;
    view.innerHTML=`<section class="panel"><p class="hint">报告读取失败：${esc(e.message)}</p><button class="button secondary" id="retryReport">重试</button></section>`;
    $('retryReport').onclick=renderReport;
  }
}
function renderSources(){
  const w=workspace, segments=w.unmatched_segments||[];
  const rules=(Array.isArray(w.link_rules)?w.link_rules:[]).filter(r=>r&&typeof r==='object');
  const warnings=w.documents.flatMap(d=>(d.warnings||[]).map(x=>`<li>${esc(d.name)}：${esc(x)}</li>`));
  $('sourcesView').innerHTML=`<div class="view-toolbar"><div><h2>来源与文件</h2><p class="hint">${w.documents.length} 份文件 · ${w.facts.length} 条事实 · ${segments.length} 个未覆盖片段</p></div><div class="source-actions"><button class="button secondary small" id="runCrossAudit">运行跨文档审计</button><button class="button primary small" id="buildRepairPlan">生成修复计划</button><button class="button secondary small" id="appendDocuments">＋ 追加文档</button></div></div><div id="reviewSummary"></div><div class="fact-search"><input id="sourceFileSearch" type="search" value="${esc(fileSearch)}" placeholder="搜索文件名" aria-label="搜索文件名"></div><div id="sourceFileList"></div><details class="source-section"><summary>事实数据 <span>${w.facts.length} 条</span></summary><div class="fact-search"><input id="sourceFactSearch" type="search" value="${esc(sourceSearch)}" placeholder="搜索主体、指标、期间或事实 ID" aria-label="搜索事实表"></div><div id="sourceFactTable"></div></details><details class="source-section"><summary>检查范围与未识别内容 <span>${segments.length} 个片段</span></summary><p class="hint">未覆盖内容可能是标题、普通说明或不支持的表达，不能据此认定正确。</p><ul class="warning-list">${warnings.join('')}<li>不验证业务因果关系或主观评价；不保证任意排版和嵌入对象保真。</li></ul><div id="sourceSegmentList"></div></details><details class="source-section"><summary>已学习关联规则 <span>${rules.length} 条</span></summary><p class="hint">规则用于候选排序，来源仍需人工批准。</p><div id="sourceRuleList"></div></details>`;
  const quality=w.fact_quality||{};
  if(quality.status){const qClass=quality.status==='pass'?'green':quality.status==='review'?'amber':'red';const qTitle=quality.status==='pass'?'结构质量检查通过':quality.status==='review'?'事实源需要复核':'事实源已阻止自动修复';$('sourcesView').insertAdjacentHTML('afterbegin',`<div class="source-quality"><span class="badge ${qClass}">${qTitle}</span><span>阻断 ${quality.summary?.blocking||0} · 警告 ${quality.summary?.warnings||0}</span>${quality.issues?.length?`<details><summary>查看 ${quality.issues.length} 项问题</summary><ul class="warning-list">${quality.issues.map(i=>`<li>${esc(i.message)} · ${esc(i.sheet||'')}${i.cell?'!'+esc(i.cell):''}</li>`).join('')}</ul></details>`:''}</div>`)}
  if((w.pdf_origins||[]).length)$('sourcesView').insertAdjacentHTML('afterbegin',`<section class="panel" style="margin-bottom:20px"><h2>原始 PDF 溯源</h2><p class="hint">PDF 仅作为只读证据保存，正文和表格已转换为可维护的 Word 与 Excel。</p>${w.pdf_origins.map(o=>`<p><a class="button secondary small" href="${o.download_url}" download>↓ 下载原始 PDF</a> <span class="hint">SHA256 ${esc((o.sha256||'').slice(0,12))}…</span></p>`).join('')}</section>`);
  if((w.pdf_origins||[]).length && w.documents.some(d=>d.kind==='docx')){
    const revisions=w.pdf_revisions||[];
    $('sourcesView').insertAdjacentHTML('afterbegin',`<section class="panel" style="margin-bottom:20px"><div class="panel-top"><div><h2>独立修订版 PDF</h2><p class="hint">确认 Word 修复后重新导出新 PDF；原始 PDF 始终只读。页数、文字层或像素存在差异时，系统阻止正式下载并提示视觉复核。</p></div><button class="button secondary small" id="exportRevisedPdf">生成修订版 PDF</button></div>${revisions.map(r=>{const passed=r.verification?.status==='passed';const visual=r.verification?.visual_check;const detail=visual&&visual.changed_page_count?` · ${visual.changed_page_count} 页存在视觉差异`:'';return `<p>${passed?`<a class="button secondary small" href="${r.download_url}" download>↓ 下载 ${esc(r.name)}</a>`:`<span class="button secondary small" aria-disabled="true">⛔ 未通过视觉验收，暂不下载</span>`} <a class="link-button" href="${r.manifest_url}" download>查看转换清单</a> <span class="hint">${esc(r.verification?.status||'待复核')}${detail}</span></p>`}).join('')}</section>`);
    $('exportRevisedPdf').onclick=async()=>{const result=await busy('正在由 Word 重新导出 PDF',()=>api(`/api/projects/${workspace.id}/pdf-revised`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:workspace.revision})}));if(result){workspace=await api(`/api/projects/${workspace.id}`);render();toast(`已生成修订版 PDF：${result.pdf_revision.verification.status}`);}};
  }
  $('appendDocuments').onclick=appendDocumentsModal;
  $('runCrossAudit').onclick=async()=>{const result=await busy('正在归并 Word、Excel、PPT 并检查冲突',()=>api(`/api/projects/${w.id}/review/cross-document`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:w.revision})}));if(result){workspace=result;render();toast(`跨文档审计完成：发现 ${result.cross_audit?.findings?.length||0} 项疑点`);}};
  $('buildRepairPlan').onclick=async()=>{const result=await busy('正在生成结构化修复计划',()=>api(`/api/projects/${w.id}/review/repair-plan`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:w.revision})}));if(result){workspace=result;render();toast(`修复计划已生成：${result.repair_plan?.summary?.total||0} 项，仍需人工批准`);}};
  const audit=w.cross_audit, plan=w.repair_plan;
  if(audit || plan){$('reviewSummary').innerHTML=`<div class="review-summary-grid">${audit?`<section class="review-summary"><div><b>跨文档审计</b><span class="badge ${audit.stale?'red':audit.findings?.length?'amber':'green'}">${audit.stale?'已过期':`${audit.findings?.length||0} 项疑点`}</span></div><p>已归并 ${audit.groups?.length||0} 组同主体、指标和期间内容。数值差异由程序计算；语义模型状态：${esc(audit.semantic_status||'未请求')}，模型疑点仍需人工复核。</p>${audit.findings?.length?`<details><summary>查看疑点</summary>${audit.findings.slice(0,8).map(f=>`<div class="review-finding"><b>${esc(f.category)} · ${esc(f.source||'rules')}</b><span>${esc(f.reason)}</span><small>${f.evidence?.map(e=>esc(e.file_name+' · '+e.label)).join('；')}</small></div>`).join('')}</details>`:''}</section>`:''}${plan?`<section class="review-summary"><div><b>修复规划</b><span class="badge ${plan.stale||plan.audit_stale?'red':plan.summary?.blocked?'red':'neutral'}">${plan.stale||plan.audit_stale?'已过期':`${plan.summary?.total||0} 项计划`}</span></div><p>高风险 ${plan.summary?.high_risk||0} 项 · 涉及 ${plan.summary?.files||0} 个文件 · ${plan.requires_approval?'等待用户批准后才能写回':'只读计划'}</p>${plan.items?.length?`<details><summary>查看修复计划</summary>${plan.items.slice(0,8).map(i=>`<div class="review-finding"><b>${esc(i.file_name)} · ${esc(i.label)}</b><span>${esc(i.before)} → ${esc(i.after)}</span><small>风险：${esc(i.risk_level)} · ${esc(i.risk_reasons?.join('；')||'常规复核')}</small></div>`).join('')}</details>`:''}</section>`:''}</div>`;}
  ['files','facts','segments','rules'].forEach(renderSourcePage);
  $('sourceFileSearch').oninput=e=>{fileSearch=e.target.value;sourcePages.files=0;renderSourcePage('files');};
  $('sourceFactSearch').oninput=e=>{sourceSearch=e.target.value;sourcePages.facts=0;renderSourcePage('facts');};
}
function renderSourcePage(key){
  const w=workspace, term=sourceSearch.trim().toLowerCase();
  if(key==='files'){
    const files=w.documents.filter(d=>d.name.toLowerCase().includes(fileSearch.trim().toLowerCase()));
    const pager=pageControls(key,files.length,12), page=files.slice(sourcePages.files*12,(sourcePages.files+1)*12);
    $('sourceFileList').innerHTML=`<div class="file-grid">${page.map(d=>`<article class="file-card"><span class="file-icon ${d.kind}">${d.kind==='xlsx'?'X':d.kind==='docx'?'W':d.kind==='pptx'?'P':'I'}</span><h3>${esc(d.name)}</h3><p>${d.kind==='xlsx'?'事实源':'成果文件'}${d.warnings?.length?' · '+d.warnings.length+' 项提示':''}</p><a class="button secondary small" href="${esc(d.download_url)}" download>↓ 下载</a><details><summary>文件校验</summary><small class="hash-value">SHA256 ${esc(d.sha256||'')}</small></details></article>`).join('')||'<p class="hint">没有匹配的文件。</p>'}</div>${pager}`;
  }else if(key==='facts'){
    const facts=w.facts.filter(f=>[f.id,f.subject,f.metric,f.period,f.scope].some(v=>String(v||'').toLowerCase().includes(term)));
    const pager=pageControls(key,facts.length), page=facts.slice(sourcePages.facts*pageSize,(sourcePages.facts+1)*pageSize);
    $('sourceFactTable').innerHTML=`<div class="table-scroll"><table class="data-table"><thead><tr><th>主体 / 指标</th><th>期间</th><th>数值</th><th>单位</th><th>口径</th><th>来源</th></tr></thead><tbody>${page.map(f=>`<tr><td>${esc(f.subject)} · ${esc(f.metric)}<small class="cell-meta">${esc(f.id)}</small></td><td>${esc(f.period)}</td><td>${esc(f.value??'缺失')}</td><td>${esc(f.unit)}</td><td>${esc(f.scope)}</td><td>${esc(f.sheet)}!${esc(f.cell)}</td></tr>`).join('')}</tbody></table></div>${pager}`;
  }else if(key==='segments'){
    const rows=w.unmatched_segments||[], pager=pageControls(key,rows.length);
    $('sourceSegmentList').innerHTML=rows.slice(sourcePages[key]*pageSize,(sourcePages[key]+1)*pageSize).map(b=>`<div class="source-text-row"><small>${esc(w.documents.find(d=>d.id===b.file_id)?.name||'')} · ${esc(b.label)}</small><p>${esc(b.text)}</p></div>`).join('')+pager;
  }else{
    const rows=(w.link_rules||[]).filter(r=>r&&typeof r==='object'),pager=pageControls(key,rows.length);
    $('sourceRuleList').innerHTML=`<div class="table-scroll"><table class="data-table"><thead><tr><th>规则</th><th>事实 ID</th><th>口径</th><th>确认次数</th><th>最近确认</th></tr></thead><tbody>${rows.slice(sourcePages[key]*pageSize,(sourcePages[key]+1)*pageSize).map(r=>`<tr>${[r.key,r.fact_id,r.scope,r.hits,r.created_at?localTime(r.created_at):''].map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${pager}`;
  }
}
function appendDocumentsModal(){
  const revision=workspace.revision;
  modal('追加成果文档',`<p class="modal-intro">选择本批新增的 Word、PPT 或图片。Excel 已在项目创建时绑定，不需要重复上传。</p><form id="appendForm"><label class="dropzone"><h3>选择一批文档</h3><p>单批最多 50 份，合计不超过 100MB；可重复操作继续追加</p><input id="appendFiles" type="file" accept=".docx,.pptx,.png,.jpg,.jpeg,.gif,.webp,.bmp,.tiff" multiple required></label><div id="appendStatus" class="upload-file-status">尚未选择文件</div><div class="modal-actions"><button type="button" class="button secondary" id="appendCancel">取消</button><button type="submit" class="button primary" id="appendSubmit" disabled>上传并识别</button></div></form>`);
  const input=$('appendFiles'), submit=$('appendSubmit'), status=$('appendStatus');
  input.onchange=()=>{const files=[...input.files], total=files.reduce((n,f)=>n+f.size,0);status.textContent=files.length?`已选择 ${files.length} 份 · ${(total/1024/1024).toFixed(2)} / 100 MB`:'尚未选择文件';submit.disabled=!files.length||files.length>50||total>100*1024*1024;};
  $('appendCancel').onclick=closeModal;
  $('appendForm').onsubmit=async e=>{e.preventDefault();const form=new FormData();form.set('revision',revision);form.set('continue_on_error','true');[...input.files].forEach(f=>form.append('files',f,f.name));const result=await busy('追加文档并重新扫描',()=>api(`/api/projects/${workspace.id}/documents`,{method:'POST',body:form}));if(result){workspace=result;render();await refreshProjects();closeModal();const rejected=result.batch_results?.rejected||[];const detail=rejected.map(x=>`${x.name}：${x.reason}`).join('；');if(result.batch.status==='done')toast(`已追加 ${result.batch.count} 份文档`);else if(result.batch.status==='partial')toast(`已追加 ${result.batch.count} 份，${result.batch.rejected} 份失败：${detail}`,true);else toast(`本批文件均未追加：${detail}`,true);}};
}
function renderGraph(){
  const w=workspace,cs=graphFact?w.claims.filter(c=>c.refs.includes(graphFact)):w.claims;
  $('graphView').innerHTML=`<p class="graph-note">选择左侧事实，查看依赖它的论断。虚线含义的候选关系须经人工确认，才可用于自动修复。</p><div class="graph-columns"><div class="graph-column"><h3>来源事实 · ${w.facts.length}</h3><button class="link-button" id="clearGraph" style="margin-bottom:15px">显示全部关联</button>${w.facts.map(f=>`<button class="graph-node ${graphFact===f.id?'selected':''}" style="width:100%;text-align:left" data-graph-fact="${esc(f.id)}">${esc(factName(f))}<small>${esc(f.value)} ${esc(f.unit)} · ${esc(f.scope)}</small></button>`).join('')}</div><div class="graph-arrow">→</div><div class="graph-column"><h3>依赖论断 · ${cs.length}</h3>${cs.map(c=>`<div class="graph-node"><div style="display:flex;justify-content:space-between;gap:10px"><span>${esc(c.original)}</span><span class="badge ${classNames[resultOf(c).status]}">${statusNames[resultOf(c).status]}</span></div><small>${esc(fileOf(c).name)} · ${esc(c.label)}</small><div class="graph-ref">${c.confirmed?'已确认':'候选'} · ${esc(c.refs.join(' + '))}</div><button class="link-button" data-claim="${c.id}" style="margin-top:8px">查看计算与依据 →</button></div>`).join('')||'<div class="empty-list">没有关联论断</div>'}</div></div>`;
}
function renderHistory(){
  const audit = (workspace.audit || []).slice().reverse();
  const auditRow = a => `<div class="history-row compact"><time>${esc(localTime(a.time))}</time><div class="history-main"><h3>${esc(a.event)}</h3><p title="${esc(a.detail || '')}">${esc(a.detail || '已记录')}</p></div></div>`;
  const recentAudit = audit.slice(0, 6), olderAudit = audit.slice(6);
  const auditMore = olderAudit.length ? `<details class="history-details"><summary>查看更早的 ${olderAudit.length} 条记录</summary>${olderAudit.map(auditRow).join('')}</details>` : '';
  $('historyView').innerHTML=`<section class="panel history-panel"><div class="panel-top"><div><h2>变更记录</h2><p class="hint">只显示关键动作；完整审计信息可展开查看。</p></div><span class="badge neutral">共 ${audit.length} 条</span></div><div class="history-highlight"><strong>${workspace.last_repair ? `最近一次修复：${workspace.last_repair.count} 项` : '尚未生成修复版本'}</strong><span>当前版本 ${esc(workspace.revision)}</span></div>${recentAudit.map(auditRow).join('')||'<p class="hint">暂无变更记录。</p>'}${auditMore}</section>`;
  const agent=workspace.agent;
  if(agent){
  const nodes={run:'启动任务',prepare:'准备',list_facts:'事实智能体 · 读取事实',list_claims:'抽取智能体 · 读取论断',propose_links:'规则候选',suggest_semantic_claims:'抽取智能体 · 语义候选召回',suggest_links_llm:'关联智能体 · DeepSeek 建议',suggest_links_local:'关联智能体 · 本地语义模型',cross_document_audit:'跨文档审计智能体',plan_repair:'修复规划智能体 · 生成计划',model_skipped:'跳过模型',link:'整理关联',check_claim:'核验论断',diagnose:'核验智能体 · 汇总诊断',ask:'等待人工',decide:'人工批准',confirm_links:'确认来源',repair:'修复智能体 · 修复文件',verify:'复核智能体 · 结果复核',done:'完成'};
    const phases={idle:'执行未完成',running:'执行中',awaiting_decision:'等待人工决定',done:'已完成'};
    const calls=workspace.model_call_summary||{};
    const callHint=`模型台账：${calls.total||0} 次 · 远程 ${calls.remote||0} · 本地 ${calls.local||0} · token ${(calls.prompt_tokens||0)+(calls.completion_tokens||0)}${calls.estimated_cost_cny==null?' · 费用按服务端价格配置后估算':` · 估算 ${calls.estimated_cost_cny} 元`}`;
    const trace = agent.trace || [], traceRow = t => `<div class="history-row compact"><time>${esc(localTime(t.at))}</time><div class="history-main"><h3>${esc(nodes[t.node]||t.node)}</h3><p title="${esc(t.summary || '')}">${esc(t.summary || '已完成')}</p><small>${t.model?'模型请求：'+esc(t.model):'确定性处理'} · ${esc(t.duration_ms ?? '')} ms</small></div></div>`;
    const recentTrace = trace.slice(-4), olderTrace = trace.slice(0, -4);
    const traceMore = olderTrace.length ? `<details class="history-details"><summary>查看完整执行轨迹（${trace.length} 个节点）</summary>${olderTrace.map(traceRow).join('')}</details>` : '';
    $('historyView').insertAdjacentHTML('afterbegin',`<section class="panel history-panel agent-history"><div class="panel-top"><div><h2>智能体进度 · ${esc(phases[agent.phase])}</h2><p class="hint">${esc(agent.run_id)}${agent.revision!==workspace.revision?' · 数据已更新，请重新运行':''}</p></div><span class="badge neutral">${trace.length} 个节点</span></div><p class="history-call-summary">${esc(callHint)}</p>${recentTrace.map(traceRow).join('')}${traceMore}</section>`);
  }
}
function renderModelLedger(){
  const calls=workspace.model_calls||[], s=workspace.model_call_summary||{}, agent=workspace.agent||{}, routes=agent.claim_routes||{};
  const rows=Object.entries(routes), totalMs=calls.reduce((n,c)=>n+(c.duration_ms||0),0);
  const routeNames={rules:'规则候选',local:'本地模型候选',api:'API 候选',human:'转人工'};
  const nodes={link_agent:'关联',extraction_agent:'抽取',diagnosis_agent:'解释',audit_agent:'跨文档审计',planning_agent:'修复规划'};
  const outcomes={succeeded:'成功',failed:'失败',invalid_response:'无效响应'};
  const raw=v=>v==null?'—':Number(v).toFixed(3);
  const fresh=agent.revision===workspace.revision;
  $('modelsView').innerHTML=`<div class="view-toolbar"><div><h2>模型调用</h2><p class="hint">本项目累计 ${calls.length} 次调用 · 今日访客剩余 ${quota?.client_remaining??'—'} 次</p></div><button class="button secondary small" id="refreshModelQuota">刷新额度</button></div><div class="ledger-metrics"><div><strong>${s.remote||0} / ${s.local||0}</strong><span>远程 / 本地调用</span></div><div><strong>${(s.prompt_tokens||0)+(s.completion_tokens||0)}</strong><span>累计 token</span></div><div><strong>${(totalMs/1000).toFixed(2)} s</strong><span>调用耗时累计（非总运行时间）</span></div><div><strong>${s.estimated_cost_cny==null?'未配置价格':Number(s.estimated_cost_cny).toFixed(6)+' 元'}</strong><span>已知调用费用估算</span></div></div><section class="source-section"><h3>最近任务的候选路由 ${rows.length?`· ${fresh?'当前结果':'历史结果，需重新运行'}`:''}</h3><div class="routing-strip">${Object.entries(routeNames).map(([key,label])=>`<span><b>${rows.filter(([,r])=>r.source===key).length}</b> ${label}</span>`).join('')}</div><details><summary>查看逐条分数与弃答原因</summary><p class="hint">模型原始分数与分差用于阈值判断，不代表正确概率。候选来源必须人工确认。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>论断</th><th>候选来源</th><th>原始分数</th><th>分差</th><th>本地耗时</th><th>说明</th></tr></thead><tbody>${rows.map(([id,r])=>`<tr><td>${esc(workspace.claims.find(c=>c.id===id)?.original||id)}</td><td>${routeNames[r.source]||'历史记录'}</td><td>${raw(r.top_raw_score)}</td><td>${raw(r.margin)}</td><td>${r.local_duration_ms??'—'} ms</td><td>${esc(r.reason||'')}</td></tr>`).join('')}</tbody></table></div></details></section><details class="source-section"><summary>完整调用台账 <span>${calls.length} 条</span></summary><div class="table-scroll"><table class="data-table"><thead><tr><th>时间</th><th>节点</th><th>模型</th><th>结果</th><th>输入 / 输出 token</th><th>耗时</th><th>估算费用</th></tr></thead><tbody>${calls.slice().reverse().map(c=>`<tr><td>${esc(localTime(c.at))}</td><td>${nodes[c.node]||esc(c.node)}</td><td>${esc(c.model)}</td><td>${outcomes[c.outcome]||esc(c.outcome)}</td><td>${c.prompt_tokens||0} / ${c.completion_tokens||0}</td><td>${c.duration_ms||0} ms</td><td>${c.estimated_cost_cny==null?'未配置':Number(c.estimated_cost_cny).toFixed(6)+' 元'}</td></tr>`).join('')}</tbody></table></div></details>`;
  $('refreshModelQuota').onclick=async()=>{await refreshQuota();renderModelLedger();};
}
const ocrDrafts=new Map();
function renderOcr(){
  const images=workspace.images||[], enabled=(workspace.ocr_enabled??health?.ocr?.enabled)&&remoteModelAllowed();
  const names={pending:'待识别',done:'待核对',failed:'识别失败',confirmed:'文字已确认'};
  $('ocrView').innerHTML=`<section class="panel"><h2>图片文字</h2><p class="hint">${enabled?'OCR 已启用，需当前 DeepSeek 模型支持图片输入。点击运行会将这张图片发送至 DeepSeek，可能产生调用费用。':'OCR 未启用。管理员可配置 DEEPSEEK_API_KEY，并将 ZHILIAN_OCR_BACKEND 设为 auto 或 deepseek。'} 核对并确认文字后才参与验证，图片不会自动修复。</p>
    ${images.map(i=>`<article class="evidence-block"><h3>${esc(fileOf(i)?.name)} · ${esc(i.label)} <span class="badge ${i.ocr_status==='confirmed'?'green':'amber'}">${names[i.ocr_status]}</span></h3>
      <a href="${esc(i.image_url)}" target="_blank" rel="noopener"><img src="${esc(i.image_url)}" alt="${esc(i.label)}" loading="lazy" style="display:block;max-width:100%;max-height:320px;object-fit:contain;margin:12px 0"></a>
      ${i.ocr_error?`<p class="hint">${esc(i.ocr_error)}</p>`:''}
      <label class="field-label" for="ocr-${esc(i.id)}">核对图片文字（可修正）</label><textarea class="text-input" id="ocr-${esc(i.id)}" data-ocr-text="${esc(i.id)}" rows="5" maxlength="20000" ${i.ocr_status==='pending'?'disabled':''}>${esc(ocrDrafts.get(workspace.id+':'+i.id)??i.ocr_text??'')}</textarea>
      <p class="hint">${i.ocr_status==='confirmed'?`已生成 ${(workspace.ocr_claims||[]).filter(c=>c.image_id===i.id).length} 条受支持论断；修改文字后请再次确认。`:i.ocr_status==='failed'?'可重试识别，也可对照图片人工录入文字后确认。':'识别出的文字尚未核对前，不作为核验依据。'} 表格行列文本不一定包含受支持论断，未识别部分可在“来源与文件”查看。</p>
      <div class="modal-actions"><button class="button secondary small" data-ocr-run="${esc(i.id)}" ${!enabled||!['pending','failed'].includes(i.ocr_status)?'disabled':''}>${i.ocr_status==='failed'?'重试 OCR':'运行 OCR'}</button><button class="button primary small" data-ocr-confirm="${esc(i.id)}" ${i.ocr_status==='pending'?'disabled':''}>${i.ocr_status==='confirmed'?'重新确认文字':'确认文字'}</button></div></article>`).join('')||'<p class="hint">没有抽取到图片。请新建项目，导入含内嵌图片的 Word 或含顶层图片的 PPT。旧项目需重新导入；组合对象中的图片暂不处理。</p>'}</section>`;
  $('ocrView').querySelectorAll('[data-ocr-text]').forEach(input=>input.oninput=()=>ocrDrafts.set(workspace.id+':'+input.dataset.ocrText,input.value));
  $('ocrView').querySelectorAll('[data-ocr-run]').forEach(button=>button.onclick=async()=>{
    const result=await post('ocr/run',{image_ids:[button.dataset.ocrRun]},'识别图片文字，最长约60秒');
    if(result)toast(result.images.find(i=>i.id===button.dataset.ocrRun)?.ocr_status==='done'?'识别完成，请对照图片核对文字':'识别未完成，请查看图片下方提示');
  });
  $('ocrView').querySelectorAll('[data-ocr-confirm]').forEach(button=>button.onclick=async()=>{
    const id=button.dataset.ocrConfirm, text=$('ocr-'+id).value, key=workspace.id+':'+id;
    if(!text.trim()){toast('请先核对并填写图片文字',true);return;}
    const result=await post('ocr/confirm',{items:[{image_id:id,text}]},'确认文字并生成只读论断');
    if(result){ocrDrafts.delete(key);renderOcr();toast('文字已确认，请在一致性工作台或异常诊断中查看只读核验结果');}
  });
}
function renderDiagnosis(){
  const records=workspace.diagnosis||[], summary=workspace.diagnosis_summary||{};
  const explanations=workspace.diagnosis_explanations||{};
  const facts=new Map(workspace.facts.map(f=>[f.id,f]));
  const card=r=>`<article class="diagnosis-card ${r.code==='inconsistent'?'problem':''}">
    <div class="claim-top"><span class="badge ${categoryClasses[r.category]||'amber'}">${esc(r.category)}</span><span class="file-tag">${esc(r.file_name)} · ${esc(r.label)}</span><span class="badge ${r.confirmed?'neutral':'amber'}">${r.source==='ocr'?'图片候选':r.confirmed?'来源已确认':'来源待确认'}</span></div>
    <div class="claim-text">${r.source==='ocr'?'<span class="badge neutral">OCR</span> ':''}${esc(r.original)}</div>
    ${r.expected&&r.code==='inconsistent'?`<div class="suggested"><span>建议更新为</span>${esc(r.expected)}</div>`:''}
    <div class="diagnosis-action"><span>${esc(r.fix_hint||'请查看证据后决定下一步')}</span><button class="link-button" data-claim="${esc(r.claim_id)}">审阅来源 →</button></div>
    <details class="diagnosis-details"><summary>查看完整诊断与证据</summary><p class="hint">${r.kind==='chart'?'原生图表对象，无正文字符区间':`文本区间：[${esc(r.start)}, ${esc(r.end)})（从 0 起，右端不含）`}</p><p><strong>技术原因：</strong>${esc(r.reason||'无')}</p><p><strong>确定性解释：</strong>${esc(r.explanation)}</p>${explanations[r.claim_id]?`<p><strong>模型润色（仅供参考）：</strong>${esc(explanations[r.claim_id])}</p>`:''}<p><strong>定位：</strong>${esc(r.location)} · ${esc(r.code)}</p>${(r.evidence||[]).length?`<div class="evidence-block"><strong>Excel 证据</strong>${r.evidence.map(f=>`<p>${esc(f.sheet)}!${esc(f.cell)} · ${esc(f.subject)} · ${esc(f.metric)} · ${esc(f.period)}<br>数值：${esc(facts.get(f.id)?.value??'缺失')} ${esc(f.unit)} · 口径：${esc(f.scope)}</p>`).join('')}</div>`:'<p class="hint">暂无可用 Excel 单元格证据，请先审阅来源。</p>'}</details>
  </article>`;
  const anomalies=records.filter(r=>r.code!=='consistent'), passed=records.filter(r=>r.code==='consistent');
  const categories=['结论失效','数据缺失','来源冲突','口径不一致','需人工复核'];
  const groups=categories.map(category=>{
    const items=anomalies.filter(r=>r.category===category);
    return items.length?`<h3 class="section-heading">${category} · ${items.length} 项</h3>${items.map(card).join('')}`:'';
  }).join('');
  const categorySummary=categories.map(category=>{const count=anomalies.filter(r=>r.category===category).length;return `<div class="diagnosis-stat"><span class="badge ${categoryClasses[category]||'amber'}">${esc(category)}</span><strong>${count}</strong><small>项</small></div>`}).join('');
  $('diagnosisView').innerHTML=`<section class="panel diagnosis-panel"><div class="panel-top"><div><h2>异常诊断</h2><p class="hint">共 ${summary.total??records.length} 条已识别论断 · ${anomalies.length} 项需要关注</p></div><button class="button primary small" id="explainDiagnosis">生成诊断解释</button></div><div class="diagnosis-summary">${categorySummary}</div>
    <p class="hint">${health?.model.enabled&&remoteModelAllowed()?'点击后将论断、诊断原因、期望文本及事实元数据发送给 DeepSeek 润色。':'当前使用确定性模板解释，不会发送模型请求。'} 来源确认和文件修复仍需人工批准。</p>
    ${groups||'<p class="hint">未发现异常。仅覆盖已识别的论断，候选来源仍需确认。</p>'}
    ${passed.length?`<details class="passed-diagnosis"><summary class="link-button">查看计算通过的论断与证据（${passed.length} 项）</summary>${passed.map(card).join('')}</details>`:''}</section>`;
  const button=$('explainDiagnosis');
  button.onclick=async()=>{
    if(!health?.model.enabled||!remoteModelAllowed()){toast('当前使用确定性模板解释，未发送请求');return;}
    const result=await post('diagnosis/explain',{},'DeepSeek 正在润色解释，最长约60秒');
    await refreshQuota();
    if(result)toast(Object.keys(result.diagnosis_explanations||{}).length?'诊断解释已生成；未返回的项继续使用模板':'未收到可用模型解释，继续使用确定性模板');
  };
}
function agentModal(){
  if(workspace.agent?.phase==='awaiting_decision'&&workspace.agent.revision===workspace.revision){showAgentResult();return;}
  const projectId=workspace.id, revision=workspace.revision;
  const mode=currentModelMode();
  const modeLabel=modelModes[mode];
  modal('运行证据链验证智能体',`<p class="modal-intro">当前模式：<b>${esc(mode)}</b>（${esc(modeLabel)}）。按准备、关联、诊断、人工决定、修复、复核的顺序处理当前已保存的数据。来源确认和文件修复分别等待你批准。</p><p class="hint">${mode==='rules'?'仅使用确定性规则，不调用模型。':mode==='local'?'规则无法确定时使用本地语义模型，不调用远程 API。':mode==='api'?'规则无法确定时调用 API，跳过本地模型。':mode==='api_only'?'所有已识别正文论断交给 API，用于对照实验。':health?.local_reranker?.enabled?'本地语义模型 会先处理规则多候选和零候选语义改写；低置信样本再升级 API。':health?.model.enabled?'关联阶段会把困难论断及事实元数据发送给 DeepSeek，可能产生调用费用；模型只提出候选。':'无模型时使用规则候选，功能仍可运行。'} 图表使用规则来源。关闭待办窗口后可继续。${quota&&typeof quota.client_remaining==='number'?` 今日剩余额度：本访客 ${quota.client_remaining} 次，全局 ${quota.global_remaining} 次。`:''}</p>${drafts().size?'<p class="hint">右侧还有未应用的数值。若要核验这些变更，请先关闭窗口并点击“更新数据并验证”。</p>':''}<div class="modal-actions"><button class="button primary" id="startAgent">开始运行</button></div>`);
  $('startAgent').onclick=async()=>{
    if(workspace.id!==projectId||workspace.revision!==revision){toast('项目已更新，请关闭窗口重新运行',true);return;}
    const result=await post('agent/run',{},'智能体正在关联与诊断，模型请求最长约60秒');
    await refreshQuota();
    if(result)showAgentResult();
  };
}
function showAgentResult(){
  const agent=workspace.agent;
  if(agent.phase==='idle'){closeModal();toast(agent.error||'智能体执行未完成，请查看变更记录',true);return;}
  if(agent.phase==='done'){
    const r=agent.result;
    modal('智能体已完成',`<p class="modal-intro">一致 ${r.consistent} 项，不一致 ${r.inconsistent} 项，无法判断 ${r.unverifiable} 项；本次修复 ${r.repaired} 项。</p><p class="hint">结果仅覆盖已识别且受支持的论断，无法判断项和未识别正文仍需人工复核。执行过程可在“变更记录”查看。</p><p class="hint">下载当前版本的 Excel / Word / PPT（按项目已导入文件列出）</p><div class="delivery-links">${downloadLinks()}</div><div class="modal-actions" style="flex-wrap:wrap"><button class="button secondary" id="viewAgentTrace">查看执行轨迹</button><button class="button secondary" id="agentLocal">导出到本地文件夹</button>${window.showOpenFilePicker?'<button class="button secondary" id="agentWriteBack">直接修改本地文件</button>':''}<a class="button secondary" href="/api/projects/${workspace.id}/export" download>下载完整成果包</a></div>`);
    $('viewAgentTrace').onclick=()=>{closeModal();switchView('history');};
    $('agentLocal').onclick=()=>exportLocal();
    if($('agentWriteBack'))$('agentWriteBack').onclick=()=>writeBackLocal();
    if(window.showOpenFilePicker)$('modalContent').insertAdjacentHTML('beforeend','<p class="hint">直接修改将覆盖本次导入时授权的原 Excel / Word / PPT。刷新页面后句柄丢失，需重新用支持写回的入口导入。</p>');
    if(health?.auto_export || localStorage.getItem('zhilian-auto-export')==='1')exportLocal(true);
    return;
  }
  const pending=agent.pending;
  if(!pending){closeModal();toast('任务尚在运行，请稍后刷新');return;}
  const projectId=workspace.id, revision=workspace.revision, repair=pending.kind==='approve_repair';
  const canSelectAll=repair||pending.kind==='confirm_links';
  const titles={confirm_links:'智能体 · 审阅来源关联',resolve_ambiguity:'智能体 · 选择有歧义的来源',approve_repair:'智能体 · 预览并批准修复'};
  const sources=refs=>refs.map(id=>{const f=workspace.facts.find(f=>f.id===id);return f?`${factName(f)}：${f.value??'缺失'}${f.unit} · ${f.scope} · ${f.sheet}!${f.cell} [${id}]`:id;}).join('；');
  const route=agent.model_routing||{};
  const routeHint=route.zero_candidate_claims!=null?`<p class="hint">本次模式：${esc(route.mode||health?.model?.mode||'hybrid')} · 规则零候选 ${route.zero_candidate_claims} 项 · 本地零候选召回 ${route.local_zero_recall_claims||0} 项 · API 难例 ${route.api_candidate_claims||0} 项。</p>`:'';
  modal(titles[pending.kind],`<p class="modal-intro">${repair?'只有勾选并批准的内容才写入文件，随后重新读取复核。':'请核对主体、期间、单位及统计口径。选择候选还不代表批准，需要勾选该项并提交。'} 共 ${pending.items.length} 项，可分批处理。</p>${routeHint}${canSelectAll?`<div class="claim-toolbar" style="margin-bottom:12px"><button type="button" class="button secondary small" id="agentSelectAll">全选</button><span id="agentSelectedCount" class="hint">已选 0 / ${pending.items.length} 项</span></div>`:''}<form id="agentForm">${pending.items.map((item,index)=>`<div class="diff-row"><label><input type="checkbox" data-agent-approve="${index}"> 批准此项 · ${esc(workspace.documents.find(d=>d.id===item.file_id)?.name)} · ${esc(item.label)}</label><div class="evidence-block">${esc(item.original)}</div><p class="hint">${esc(item.reason)}</p>${repair?`<div class="diff-new">修复为：${esc(item.expected)}</div><p class="hint">来源：${esc(sources(item.refs))}</p>`:`<label class="field-label" for="agentOption${index}">来源候选</label><select class="text-input" id="agentOption${index}">${pending.kind==='resolve_ambiguity'?'<option value="">请选择一个候选，或手动指定</option>':''}${item.options.map((o,n)=>`<option value="${n}" ${o.remembered?'selected ':''}${o.status==='unverifiable'?'disabled':''}>${esc(sources(o.refs))} · ${esc(o.reason)}${o.remembered?'（已记住）':''}${o.status==='unverifiable'?'（无法计算）':''}</option>`).join('')}<option value="manual">手动指定事实 ID</option></select><div id="agentManualGroup${index}" class="hidden"><label class="field-label" for="agentManual${index}">事实 ID（以逗号分隔，增长率按上期、本期；预算判断按支出、预算）</label><input class="text-input" id="agentManual${index}" value="${esc(item.refs.join(','))}"></div>`}</div>`).join('')}${repair?'':`<details><summary>查看事实 ID 与单元格</summary>${workspace.facts.map(f=>`<p class="hint">${esc(sources([f.id]))}</p>`).join('')}</details>`}<p class="hint">未勾选的条目保持待办。可关闭窗口稍后处理。</p><div class="modal-actions"><button class="button secondary" type="button" id="agentLater">稍后处理</button><button class="button primary" type="submit">${repair?'批准所选修复并复核':'确认所选来源并继续'}</button></div></form>`);
  $('agentLater').onclick=closeModal;
  if(canSelectAll){
    const boxes=()=>[...$('agentForm').querySelectorAll('[data-agent-approve]')];
    const sync=()=>{
      const all=boxes(), checked=all.filter(b=>b.checked).length;
      $('agentSelectedCount').textContent=`已选 ${checked} / ${all.length} 项`;
      $('agentSelectAll').textContent=all.length&&checked===all.length?'全不选':'全选';
    };
    $('agentSelectAll').onclick=()=>{
      const all=boxes(), select=!all.every(b=>b.checked);
      all.forEach(b=>{b.checked=select;});
      sync();
    };
    boxes().forEach(b=>{b.onchange=sync;});
  }
  $('modal').scrollTop=0;
  if(!repair)pending.items.forEach((item,index)=>{
    $('agentOption'+index).onchange=()=>$('agentManualGroup'+index).classList.toggle('hidden',$('agentOption'+index).value!=='manual');
  });
  $('agentForm').onsubmit=async e=>{
    e.preventDefault();
    if(workspace.id!==projectId||workspace.revision!==revision){toast('项目已更新，请关闭窗口重新运行',true);return;}
    const items=[];
    for(const input of document.querySelectorAll('[data-agent-approve]:checked')){
      const index=Number(input.dataset.agentApprove), item=pending.items[index], decision={claim_id:item.claim_id};
      if(!repair){
        const choice=$('agentOption'+index).value;
        if(choice===''){toast('请为每个勾选的论断选择来源',true);return;}
        decision.refs=choice==='manual'?$('agentManual'+index).value.split(/[,，]/).map(s=>s.trim()).filter(Boolean):item.options[Number(choice)].refs;
      }
      items.push(decision);
    }
    if(!items.length){toast('请先勾选明确批准的条目',true);return;}
    const result=await post('agent/decide',{decisions:[{kind:pending.kind,items}]},repair?'修复文件并重新核验':'保存人工决定并继续诊断');
    if(result)showAgentResult();
  };
}
function uploadModal(autoWritable=false){
  pendingUploads = [];
  pendingFileHandles = new Map();
  modal('导入文件，建立项目',`<p class="modal-intro">支持的浏览器会自动打开可写文件选择器。选择后，修复完成时可直接覆盖本机原文件，无需额外勾选；也可继续使用普通上传。</p><p class="hint">一份 Excel 事实表，加一份或多份 Word / PPT。可以分多次选择，后续选择会加入列表，不会替换之前的文件。</p><form id="uploadForm"><label class="field-label" for="uploadName">项目名称</label><input id="uploadName" class="text-input" name="name" value="我的数据分析项目" maxlength="100" required><label class="dropzone"><h3>选择并加入文件</h3><p>可重复打开文件选择器；最多10份，合计不超过30MB</p><input id="uploadFiles" type="file" accept=".xlsx,.docx,.pptx" multiple></label>${window.showOpenFilePicker?'<button type="button" class="button secondary full" id="pickWritableFiles">选择文件（支持修改后写回原文件）</button><p class="hint">导入后可显式点击“直接修改本地文件”覆盖所选原文件，浏览器可能请求写入权限。刷新页面后需重新选择并导入；同名文件以最后一次选择为准。</p>':'<p class="hint">当前浏览器不支持原文件写回，请使用浏览器下载。</p>'}<div id="uploadFileList" class="upload-file-list"></div><div id="uploadFileStatus" class="upload-file-status">尚未加入文件</div><a class="link-button" href="/api/template">↓ 下载 Excel 事实表模板</a><p class="hint">需要一份 .xlsx 和至少一份 .docx / .pptx。Excel 首行包含：事实ID、主体、指标、期间、数值、单位、统计口径。</p><div class="modal-actions"><button type="button" class="button secondary" id="pdfProjectButton">从 PDF 导入并生成 Word / Excel</button><button type="button" class="button secondary" id="modalDemo">先体验演示</button><button class="button primary" id="uploadSubmit" type="submit" disabled>导入并识别</button></div></form>`);
  $('modalDemo').onclick=demo;
  $('pdfProjectButton').onclick=pdfImportModal;
  const formElement=$('uploadForm'), handles=pendingFileHandles;
  const addFile=(file,handle)=>{
    const ext=file.name.split('.').pop().toLowerCase();
    if(!['xlsx','docx','pptx'].includes(ext))return;
    const index=pendingUploads.findIndex(old=>old.name===file.name);
    if(index<0&&pendingUploads.length>=10)return;
    if(index<0)pendingUploads.push(file);else pendingUploads[index]=file;
    if(handle)handles.set(file.name,handle);else handles.delete(file.name);
  };
  $('uploadFiles').onchange=event=>{
    for(const file of event.target.files)addFile(file);
    event.target.value='';
    renderUploadFiles();
  };
  const pickWritableFiles=async()=>{
    try{
      const selected=await window.showOpenFilePicker({multiple:true,types:[{description:'Office 文件',accept:{
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':['.xlsx'],
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document':['.docx'],
        'application/vnd.openxmlformats-officedocument.presentationml.presentation':['.pptx']
      }}]});
      for(const handle of selected){
        const file=await handle.getFile();
        if($('uploadForm')!==formElement)return;
        addFile(file,handle);
      }
    }catch(e){
      if(e?.name!=='AbortError')toast('文件选择失败，请重试或使用普通上传入口',true);
    }
    if($('uploadForm')===formElement)renderUploadFiles();
  };
  if($('pickWritableFiles'))$('pickWritableFiles').onclick=pickWritableFiles;
  if(autoWritable&&window.showOpenFilePicker)pickWritableFiles();
  $('uploadForm').onsubmit=async e=>{
    e.preventDefault();
    const total = pendingUploads.reduce((sum, file) => sum + file.size, 0);
    const extensions = pendingUploads.map(file => file.name.split('.').pop().toLowerCase());
    if (pendingUploads.length < 2 || !extensions.includes('xlsx') || !extensions.some(ext => ext === 'docx' || ext === 'pptx')) { toast('请加入一份 Excel 和至少一份 Word / PPT', true); return; }
    if (total > 30 * 1024 * 1024) { toast('每批上传总大小不超过30MB', true); return; }
    const form = new FormData();
    form.set('name', $('uploadName').value);
    pendingUploads.forEach(file => form.append('files', file, file.name));
    const selectedHandles=new Map(handles);
    const result=await busy('解析文件并识别论断',()=>api('/api/projects',{method:'POST',body:form}));
    if(result){
      fileHandles.set(result.id,selectedHandles);
      workspace=result;filter='all';graphFact=null;switchView('overview');localStorage.setItem('zhilian-project',result.id);await refreshProjects();
      modal('导入识别完成',`<p class="modal-intro">已导入 ${result.summary.documents} 份文件，识别 ${result.summary.claims} 条论断。</p><p class="hint">${result.summary.claims?'现在可以运行智能体，整理来源、诊断不一致内容，并在你分别确认来源和批准修复后更新 Word / PPT。':'当前未识别到受支持的论断。请先在“来源与文件”中检查原文及识别范围；智能体仅核验已识别的论断。'}</p><p class="hint">也可稍后从页面顶部的“运行智能体”继续。</p><div class="modal-actions"><button class="button secondary" id="reviewImported">先查看识别结果</button><button class="button primary" id="runImportedAgent">运行智能体</button></div>`);
      $('reviewImported').onclick=closeModal;
      $('runImportedAgent').onclick=agentModal;
    }
  };
  renderUploadFiles();
}
function pdfImportModal(){
  modal('从 PDF 导入项目',`<p class="modal-intro">知链不会修改 PDF。系统会提取正文生成 Word，并把表格及事实候选写入 Excel；识别不完整的内容保留待人工整理，原 PDF 作为只读证据保存。</p><form id="pdfProjectForm"><label class="field-label" for="pdfProjectName">项目名称</label><input id="pdfProjectName" class="text-input" name="name" value="PDF 年报项目" maxlength="100" required><label class="field-label" for="pdfSubject">主体（可选）</label><input id="pdfSubject" class="text-input" name="subject" placeholder="例如：某公司"><label class="dropzone"><h3>选择 PDF</h3><p>单份不超过100MB；扫描件暂不支持 OCR</p><input id="pdfProjectFile" name="file" type="file" accept=".pdf" required></label><div id="pdfProjectStatus" class="upload-file-status">尚未选择文件</div><div class="modal-actions"><button type="button" class="button secondary" id="pdfBack">返回 Office 导入</button><button class="button primary" type="submit" id="pdfProjectSubmit" disabled>提取并建立项目</button></div></form>`);
  const form=$('pdfProjectForm'), input=$('pdfProjectFile'), submit=$('pdfProjectSubmit'), status=$('pdfProjectStatus');
  input.onchange=()=>{const file=input.files[0];status.textContent=file?`${file.name} · ${(file.size/1024/1024).toFixed(2)} MB`:'尚未选择文件';submit.disabled=!file||file.size>100*1024*1024;};
  $('pdfBack').onclick=()=>uploadModal();
  form.onsubmit=async event=>{
    event.preventDefault();
    const data=new FormData(form), started=Date.now();
    let processingTimer=null;
    const result=await busy('准备上传 PDF',()=>apiWithUploadProgress('/api/projects/from-pdf',data,
      percent=>updateBusyProgress(`正在上传 PDF：${percent}%`,percent,`文件大小 ${(input.files[0].size/1024/1024).toFixed(1)} MB`),
      ()=>{
        const detail=()=>`服务端正在提取全文、识别表格并生成 Word / Excel · 已用 ${Math.floor((Date.now()-started)/1000)} 秒`;
        updateBusyProgress('文件已上传，正在解析 PDF',100,detail(),true);
        processingTimer=setInterval(()=>updateBusyProgress('文件已上传，正在解析 PDF',100,detail(),true),1000);
      }
    ));
    if(processingTimer)clearInterval(processingTimer);
    if(result){
      workspace=result;filter='all';graphFact=null;switchView('overview');localStorage.setItem('zhilian-project',result.id);await refreshProjects();
      const c=result.pdf_conversion||{};
      const factMessage=c.facts?`识别 ${c.facts} 条事实候选、${c.tables||0} 张表格；候选仍需复核。`:`未自动识别事实候选，正文和原始表格已保留在 Word / Excel 中供整理。`;
      modal('PDF 导入完成',`<p class="modal-intro">已生成并加入正文 Word 与事实表 Excel。</p><p class="hint">${factMessage}原 PDF 已作为只读溯源文件保留。</p><div class="modal-actions"><button class="button secondary" id="reviewImported">查看项目</button><button class="button primary" id="runImportedAgent">运行智能体</button></div>`);
      $('reviewImported').onclick=closeModal;$('runImportedAgent').onclick=agentModal;
    }
  };
}
function importSourceModal(){
  const projectId=workspace.id, revision=workspace.revision;
  modal('导入更新后的 Excel',`<p class="modal-intro">在 Excel / WPS 中保存后选择文件。先查看数值差异和受影响结论，确认后建立新版本。</p><form id="sourceForm"><label class="dropzone"><h3>选择更新后的源表</h3><p>一份 .xlsx，最大 30MB</p><input type="file" name="file" accept=".xlsx" required></label><p class="hint">事实ID、主体、指标、期间、单位和统计口径需保持一致；支持调整行顺序。若增删事实或改变含义，请建立新项目。</p><div class="modal-actions"><button class="button primary" type="submit">查看变更与影响 →</button></div></form>`);
  $('sourceForm').onsubmit=async e=>{
    e.preventDefault();
    const form=new FormData(e.target);form.set('revision',revision);
    const file=form.get('file');
    if(file.size>30*1024*1024){toast('更新表不超过30MB',true);return;}
    const preview=await busy('分析 Excel 变更及结论影响',()=>api(`/api/projects/${projectId}/source/preview`,{method:'POST',body:form}));
    if(!preview)return;
    sourcePreviewModal(preview, file, projectId, revision);
  };
}
function sourcePreviewModal(preview, file, projectId, revision){
  const facts=new Map(workspace.facts.map(f=>[f.id,f]));
  const visibleAffected=preview.affected.filter(item=>item.before.status!==item.after.status || item.before.expected!==item.after.expected);
  modal('确认源表变更',`<p class="modal-intro">${esc(file.name)} · ${preview.changes.length} 项数值变化。以下为应用后的预计结果，当前文件尚未修改。</p><div class="preview-metrics"><div><strong>${visibleAffected.length}</strong><span>需要关注</span></div><div><strong>${preview.summary.consistent}</strong><span>仍然成立</span></div><div><strong>${preview.summary.inconsistent}</strong><span>计算不一致</span></div><div><strong>${preview.summary.unverifiable}</strong><span>无法判断</span></div></div><h3>数值变更</h3><div class="table-scroll"><table class="data-table"><thead><tr><th>来源事实</th><th>当前值</th><th>新值</th></tr></thead><tbody>${preview.changes.map(c=>{const f=facts.get(c.id);return `<tr><td>${esc(factName(f))}<small class="cell-meta">${esc(c.id)}</small></td><td>${esc(c.before??'缺失')} ${esc(f.unit)}</td><td class="changed-value">${esc(c.after??'缺失')} ${esc(f.unit)}</td></tr>`;}).join('')}</tbody></table></div><h3 class="section-heading">需要关注的结论</h3><p class="hint">这里只显示状态或预计文本发生变化的结论；仍然成立且无需修改的内容已折叠。</p>${visibleAffected.map(item=>{const c=workspace.claims.find(c=>c.id===item.claim_id);return `<div class="impact-row"><div><span class="file-tag">${esc(fileOf(c).name)} · ${esc(c.label)}</span>${statusBadge(c,item.after)}</div><p>${esc(c.original)}</p><small>${esc(statusNames[item.before.status])} → ${esc(statusNames[item.after.status])} · ${esc(item.after.reason)}</small>${item.after.status==='inconsistent'?`<div class="diff-new">预计修复为：${esc(item.after.expected)}</div>`:''}</div>`;}).join('')||'<p class="hint">这次变化没有导致任何已识别结论需要修改。</p>'}<p class="hint">确认后更新源表并重新验证，Word / PPT 将在你预览并确认修复后更新。${drafts().size?'右侧尚未应用的输入将被此次源表更新替换。':''}</p><div class="modal-actions"><button class="button secondary" id="chooseSourceAgain">重新选文件</button><button class="button primary" id="applySource">确认导入并验证</button></div>`);
  $('chooseSourceAgain').onclick=importSourceModal;
  $('applySource').onclick=async()=>{
    const form=new FormData();form.set('file',file);form.set('revision',revision);
    const result=await busy('保存源表新版本并重新验证',()=>api(`/api/projects/${projectId}/source`,{method:'POST',body:form}));
    if(result){factDrafts.delete(projectId);workspace=result;filter='all';closeModal();switchView('overview');await refreshProjects();afterDataUpdate();}
  };
}
async function demo(){
  const result=await busy('正在创建真实 Office 演示文件',()=>api('/api/projects/demo?semantic=1',{method:'POST'}));
  if(result){closeModal();workspace=result;filter='all';switchView('overview');localStorage.setItem('zhilian-project',result.id);await refreshProjects();toast('演示已就绪：确认来源 → 载入演示变更 → 更新数据');}
}
function claimModal(id){
  const c=workspace.claims.find(c=>c.id===id),r=resultOf(c),suggestion=workspace.suggestions?.find(s=>s.claim_id===id);
  if(c.source==='ocr'){
    modal('图片论断 · 只读依据',`<span class="badge neutral">OCR · 只读</span><p class="hint">图片文字已经人工核对。下面是规则候选来源，不支持确认关联或修复图片。</p><div class="evidence-block">${esc(c.original)}</div><p>${esc(fileOf(c).name)} · ${esc(c.label)}</p><p class="hint">${esc(statusNames[r.status])} · ${esc(r.reason)}</p>${r.evidence.map(f=>`<p class="hint">${esc(f.sheet)}!${esc(f.cell)} · ${esc(f.id)} · ${esc(f.subject)} · ${esc(f.metric)} · ${esc(f.period)} · ${esc(f.value??'缺失')}${esc(f.unit)} · ${esc(f.scope)}</p>`).join('')||'<p class="hint">没有可用候选事实，需人工复核。</p>'}`);
    return;
  }
  modal('来源与计算依据',`<span class="badge ${classNames[r.status]}">${statusNames[r.status]}</span> <span class="badge neutral">${esc(kindNames[c.kind])}</span><div class="evidence-block">${esc(c.original)}</div><p class="hint">${esc(fileOf(c).name)} · ${esc(c.label)} · ${esc(c.extraction)}</p><h3 style="margin-top:18px">验证过程</h3><p class="hint">${esc(r.reason)}</p>${r.expected&&r.status==='inconsistent'?`<div class="diff-new">${esc(r.expected)}</div>`:''}<h3 style="margin-top:20px">关联来源</h3><p class="hint">增长率依次选择上期、本期；排名需确认比较集合完整。单纯存在引用并不证明统计口径一致。</p>${suggestion?`<div class="evidence-block"><small>模型建议，尚未生效</small><p>${esc(suggestion.reason)}</p><button class="link-button" id="useSuggestion">选择建议的来源</button></div>`:''}<div class="check-list">${workspace.facts.map(f=>`<label><input type="checkbox" name="ref" value="${esc(f.id)}" ${c.refs.includes(f.id)?'checked':''}><div>${esc(factName(f))} · ${esc(f.value??'缺失')}${esc(f.unit)}<small>${esc(f.id)} · ${esc(f.scope)} · ${esc(f.sheet+'!'+f.cell)}</small></div></label>`).join('')}</div><p class="hint">${c.confirmed?'此关联已经确认，可以重新选择。':'当前是候选关联，请核对后确认。'}</p><div class="modal-actions"><button class="button primary" id="confirmLink">确认所选来源</button></div>`);
  if(suggestion)$('useSuggestion').onclick=()=>document.querySelectorAll('input[name=ref]').forEach(x=>x.checked=suggestion.refs.includes(x.value));
  $('confirmLink').onclick=async()=>{const selected=[...document.querySelectorAll('input[name=ref]:checked')].map(x=>x.value);let refs=selected;if(c.kind==='growth')refs.sort((a,b)=>{const period=id=>workspace.facts.find(f=>f.id===id)?.period;return (period(a)==='上期'?0:1)-(period(b)==='上期'?0:1);});if(c.kind==='threshold'&&!('limit' in c.spec))refs.sort((a,b)=>(workspace.facts.find(f=>f.id===a)?.metric==='支出'?0:1)-(workspace.facts.find(f=>f.id===b)?.metric==='支出'?0:1));const result=await post('links',{links:[{claim_id:id,refs}]},'检查关联口径');if(result){closeModal();toast('来源关联已确认');}};
}
function confirmAllModal(){
  const cs=workspace.claims.filter(c=>c.source!=='ocr'&&c.repairable!==false&&!c.confirmed&&c.refs.length&&resultOf(c).status!=='unverifiable');
  modal('审阅候选来源',`<p class="modal-intro">以下 ${cs.length} 项已找到可计算的候选来源。请核对主体、期间、单位和统计口径；确认后才允许修复。</p><div class="check-list">${cs.map(c=>`<label><input type="checkbox" name="confirmClaim" value="${c.id}" checked><div>${esc(c.original)}<small>${esc(c.refs.map(id=>{const f=workspace.facts.find(f=>f.id===id);return `${factName(f)} ${f.value}${f.unit} [${f.scope}]`;}).join(' / '))}</small></div></label>`).join('')}</div><div class="modal-actions"><button class="button primary" id="applyConfirmAll">确认选中的关联</button></div>`);
  $('applyConfirmAll').onclick=async()=>{const ids=[...document.querySelectorAll('input[name=confirmClaim]:checked')].map(x=>x.value);const links=cs.filter(c=>ids.includes(c.id)).map(c=>({claim_id:c.id,refs:c.refs}));const result=await post('links',{links},'保存确认结果');if(result){closeModal();if(result.summary.repairable)repairModal();else toast('关联已确认，可以更新数据');}};
}
function repairModal(){
  const cs=workspace.claims.filter(c=>c.source!=='ocr'&&c.repairable!==false&&c.confirmed&&resultOf(c).status==='inconsistent');
  modal('预览并确认修复',`<p class="modal-intro">仅修改选中的受支持位置。写入新版本后，系统重新读取成果并检查这些论断，原版本保留。</p>${cs.map(c=>`<div class="diff-row"><label><input type="checkbox" name="repairClaim" value="${c.id}" checked> ${esc(fileOf(c).name)} · ${esc(c.label)}</label><div class="diff-old">− ${esc(c.original)}</div><div class="diff-new">＋ ${esc(resultOf(c).expected)}</div></div>`).join('')}<div class="modal-actions"><button class="button primary" id="applyRepairs">确认修复并复核</button></div>`);
  $('applyRepairs').onclick=async()=>{const ids=[...document.querySelectorAll('input[name=repairClaim]:checked')].map(x=>x.value);const result=await post('repair',{claim_ids:ids},'正在修复文件并重新验证');if(result){closeModal();filter='all';render();deliveryModal();}};
}
function helpModal(){
  modal('使用知链',`<div class="help-section"><h3>1 导入或体验演示</h3><p>准备一份符合模板的 Excel，以及结构清晰的 Word 或 PPT。演示项目会创建包含真实文本、不同文字格式和原生图表的文件。</p></div><div class="help-section"><h3>2 审阅来源关联</h3><p>规则识别会给出候选来源。查看依据，确认主体、指标、期间、单位和口径；支持手动纠正关联。</p></div><div class="help-section"><h3>3 修改并重新验证</h3><p>在右侧修改数值。系统检查原有论断是仍成立、已失效，还是无法判断。依赖变化不会直接触发改写。</p></div><div class="help-section"><h3>4 修复与导出</h3><p>预览修改前后内容，确认后写入新版本。导出包含Excel、Word/PPT、核验记录JSON和报告。可撤销上一次文件变更。</p></div><div class="help-section"><h3>识别范围</h3><p>支持明确表达的数值引用、较上期/环比增长率、最高排名和阈值。语义歧义、因果解释、主观结论、未识别文本不会被当作验证通过。完整范围见项目 README。</p></div>`);
}
function settingsModal(){
  const mode=currentModelMode();
  modal('模型与运行方式',`<p class="modal-intro">当前${workspace?'项目':'服务端默认'}模式：<b>${esc(mode)}</b>（${esc(modelModes[mode])}）。</p>
    <div class="help-section"><label class="field-label" for="settingsModelMode">选择项目处理模式</label><select class="text-input" id="settingsModelMode" ${workspace?'':'disabled'}>${modelModeOptions()}</select><p class="hint" id="settingsModeHint">${esc(modelModeHint(mode))}</p><p class="hint">${workspace?'保存后下次运行生效，旧候选待办失效；不会重置已确认来源或自动发起模型请求。':'打开项目后可切换；服务端默认值由 ZHILIAN_LLM_MODE 设置。'} 做对照实验请使用分别新建且尚未确认来源的相同资料。</p><button class="button primary small" id="saveModelMode" ${workspace?'':'disabled'}>保存处理模式</button></div>
    <div class="help-section"><h3>使用边界</h3><p>模型只提出来源候选，仍需人工确认，不计算、不裁决、不直接写文件。图表继续使用确定性路径；未识别或已确认的论断不进入 API 对照。</p></div>
    <div class="help-section"><h3>联网与费用</h3><p>hybrid、api、api_only 在实际调用时使用部署方配置的密钥，消耗每日额度；发送已识别的论断文本及事实元数据，原始 Office 文件不会直接上传到模型服务。论断文本可能含业务数值。rules 和 local 禁止远程关联、解释和 OCR 请求；local 需要先安装模型。</p></div>
    <div class="help-section"><h3>本地与公网</h3><p>本机可独立运行。公网部署需访问密码、HTTPS 和调用额度。项目模式是共享项目设置，尚未实现团队成员分级权限。</p></div>`);
  $('settingsModelMode').onchange=e=>{$('settingsModeHint').textContent=modelModeHint(e.target.value);};
  $('saveModelMode').onclick=async()=>{if(await switchModelMode($('settingsModelMode').value))settingsModal();};
  $('modalContent').insertAdjacentHTML('beforeend',`<div class="help-section"><h3>OCR 图片文字</h3><p>在“图片文字”中逐张运行 OCR，会发送抽取的图片给 DeepSeek，需要模型支持多模态输入。识别完成后必须人工核对，确认后的文字只读验证，不支持关联确认或修复图片。关闭 OCR 可设置 ZHILIAN_OCR_BACKEND=off。</p></div><div class="help-section"><h3>修复完成后自动落盘</h3><label><input type="checkbox" id="autoExportSetting" ${localStorage.getItem('zhilian-auto-export')==='1'?'checked':''}> 智能体完成时自动把当前 Excel、Word、PPT 和变更报告写入本地输出目录</label><p class="hint">默认关闭。此设置只影响当前浏览器；服务器端也可用 ZHILIAN_AUTO_EXPORT=1 开启。</p></div>`);
  $('autoExportSetting').onchange=e=>{if(e.target.checked)localStorage.setItem('zhilian-auto-export','1');else localStorage.removeItem('zhilian-auto-export');};
}

document.addEventListener('click',e=>{
  const pager=e.target.closest('[data-source-page]');if(pager){sourcePages[pager.dataset.sourcePage]=Math.max(0,sourcePages[pager.dataset.sourcePage]+Number(pager.dataset.offset));renderSourcePage(pager.dataset.sourcePage);}
  const nav=e.target.closest('[data-view]');if(nav)switchView(nav.dataset.view);
  const project=e.target.closest('[data-project]');if(project)loadProject(project.dataset.project);
  const claim=e.target.closest('[data-claim]');if(claim)claimModal(claim.dataset.claim);
  const graph=e.target.closest('[data-graph-fact]');if(graph){graphFact=graph.dataset.graphFact;renderGraph();}
  if(e.target.closest('#clearGraph')){graphFact=null;renderGraph();}
});
document.querySelector('.sidebar-tools').insertAdjacentHTML('beforeend','<button class="nav-item" data-view="models"><span>◇</span> 模型调用</button>');
$('filters').onclick=e=>{const b=e.target.closest('[data-filter]');if(b){filter=b.dataset.filter;render();}};
$('closeModal').onclick=closeModal;
$('modal').addEventListener('click',e=>{if(e.target===$('modal'))closeModal();});
$('newProject').onclick=()=>uploadModal(true);$('welcomeUpload').onclick=()=>uploadModal(true);$('loadDemo').onclick=demo;
$('helpButton').onclick=helpModal;$('scopeButton').onclick=helpModal;$('settingsButton').onclick=settingsModal;
$('modelModeSelect').onchange=e=>switchModelMode(e.target.value);
$('confirmAll').onclick=confirmAllModal;$('reviewRepairs').onclick=repairModal;
$('agentButton').onclick=agentModal;
$('demoChange').insertAdjacentHTML('afterend','<button class="button secondary full" id="importSource">导入更新后的 Excel</button>');
$('importSource').onclick=importSourceModal;
  $('saveFacts').onclick=async()=>{
  const values={};for(const [id,text] of drafts()){const old=workspace.facts.find(f=>f.id===id);if(!old)continue;if(text.trim()===''){toast('数值不能为空',true);return;}const value=Number(text);if(!Number.isFinite(value)){toast('请输入有效数字',true);return;}if(value!==old.value)values[id]=value;}
  if(!Object.keys(values).length){toast('数值没有变化');return;}
  const result=await post('facts',{values},'写入数据新版本并重新验证');
  if(result){factDrafts.delete(result.id);render();afterDataUpdate();}
};
$('demoChange').onclick=()=>{const values={sales_current:90,product_a:60,spending:110};document.querySelectorAll('[data-fact]').forEach(x=>{if(x.dataset.fact in values){x.value=values[x.dataset.fact];trackDraft(x);}});toast('已填写示例变更，点击“更新数据并验证”应用');};
$('exportButton').onclick=deliveryModal;
$('undoButton').onclick=()=>{modal('撤销上一次文件变更',`<p class="modal-intro">恢复上一次数据更新或修复之前的文件。操作记录会保留。</p><div class="modal-actions"><button class="button primary" id="applyUndo">确认恢复</button></div>`);$('applyUndo').onclick=async()=>{if(await post('undo',{},'恢复文件版本')){closeModal();toast('已恢复上一次文件变更之前的版本');}};};
$('suggestButton').onclick=()=>{const left=quota&&typeof quota.client_remaining==='number'?`<p class="hint">今日剩余额度：本访客 ${quota.client_remaining} 次，全局 ${quota.global_remaining} 次。${quota.client_remaining<=0?'额度已用完，本次不会发起请求。':''}</p>`:'';modal('请求 DeepSeek 关联建议',`<p class="modal-intro">将最多40条未确认论断及事实元数据发送至 ${esc(health.model.provider)} 服务，调用费用由服务提供方承担。模型只提出建议，结果仍需人工确认。</p>${left}<div class="modal-actions"><button class="button primary" id="callModel">发送并获取建议</button></div>`);$('callModel').onclick=async()=>{if(await post('suggest',{},'等待模型建议，最长约60秒')){closeModal();await refreshQuota();toast('建议已保存，在“查看依据”中审阅');}};};
// 演示环境由服务提供方付费，所以界面要明确显示"今天还剩多少次模型调用"：
// 既是使用提示，也让"限额是设计的一部分"这件事对使用者可见。
function quotaLabel(q){
  if(!q || typeof q.client_remaining!=='number') return '';
  return ` · 今日剩 ${q.client_remaining} 次`;
}
function quotaTitle(q){
  if(!q || typeof q.client_remaining!=='number') return '';
  return `模型调用限额：本访客已用 ${q.client_used}/${q.client_limit} 次，全局已用 ${q.global_used}/${q.global_limit} 次；按自然日重置，超出后自动降级为规则模式。`;
}
async function refreshQuota(){
  try{ quota=await api('/api/quota'); }catch(e){ quota=null; }
  renderModelMode();
  return quota;
}
async function init(){
  try{
    health=await api('/api/health');$('connection').innerHTML='工作空间已连接<small>本地持久化存储</small>';
    // 版本号由 /api/health 提供（后端读根目录 VERSION）。此前页脚硬编码 v0.2，
    // 而应用已是 v0.2.1，两者长期不一致——前端不再自己维护版本字符串。
    const versionLabel=$('appVersion');
    if(versionLabel&&health&&health.version)versionLabel.textContent='v'+health.version;
    await refreshQuota();
    const projects=await refreshProjects();const previous=localStorage.getItem('zhilian-project');
    if(projects.length)await loadProject(projects.find(p=>p.id===previous)?.id||projects[0].id);else render();
  }catch(e){$('welcome').classList.remove('hidden');$('connection').textContent='连接失败';toast(e.message,true);}
}
init();


