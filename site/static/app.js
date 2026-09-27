'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let workspace = null, currentView = 'overview', filter = 'all', health = null, graphFact = null, quota = null;
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
async function busy(label, action) {
  $('busyText').textContent=label; $('busy').classList.remove('hidden');
  try { return await action(); } catch(e) { toast(e.message, true); return null; }
  finally { $('busy').classList.add('hidden'); }
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
  $('projects').innerHTML=projects.map(p=>`<button class="project-link ${workspace?.id===p.id?'active':''}" data-project="${p.id}">${esc(p.name)}</button>`).join('');
  return projects;
}
async function loadProject(id) {
  const w=await busy('正在读取项目',()=>api('/api/projects/'+id));
  if(w){workspace=w;graphFact=null;localStorage.setItem('zhilian-project',w.id);render();await refreshProjects();}
}
function switchView(view) {
  currentView=view;
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  const labels={overview:'一致性工作台',diagnosis:'异常诊断',ocr:'图片文字',sources:'来源与文件',graph:'证据依赖图',history:'变更记录',report:'变更报告'};
  $('breadcrumb').textContent=labels[view];
  if(workspace)render();
}
function render() {
  $('welcome').classList.toggle('hidden',!!workspace);$('workspace').classList.toggle('hidden',!workspace);
  if(!workspace)return;
  const w=workspace,s=w.summary;
  const textIssues=w.claims.filter(c=>c.source!=='ocr'&&resultOf(c).status==='inconsistent').length;
  for (const f of w.facts) if (drafts().has(f.id) && drafts().get(f.id).trim() !== '' && Number(drafts().get(f.id)) === f.value) drafts().delete(f.id);
  $('projectName').textContent=w.name;
  $('workspaceEyebrow').textContent=w.demo?'DEMO PROJECT · 模拟数据':'PROJECT WORKSPACE';
  $('projectSubtitle').textContent=`${s.documents} 份文件 · ${s.facts} 条来源事实 · 版本 ${w.revision} · 数据与成果均保留历史版本`;
  ['overview','diagnosis','ocr','sources','graph','history','report'].forEach(v=>$(v+'View').classList.toggle('hidden',currentView!==v));
  $('undoButton').disabled=!w.history.length;
  $('agentButton').textContent=w.agent?.phase==='awaiting_decision'&&w.agent.revision===w.revision?'继续智能体待办':'运行智能体';
  const verified=w.claims.filter(c=>c.confirmed&&resultOf(c).status==='consistent').length;
  const stats=[['已识别论断',s.claims,'','逐项核验已识别的受支持表达','◇'],['已确认且成立',verified,'success','来源已确认，计算结果一致','✓'],['计算不一致',s.inconsistent,'danger','确认来源后可预览局部修复','↗'],['来源待确认',s.pending,'','候选结果须经人工核对','◷']];
  $('stats').innerHTML=stats.map(([label,value,cls,note,icon])=>`<div class="stat"><div class="stat-top">${label}<span class="stat-icon">${icon}</span></div><div class="stat-value ${cls}">${value}<span style="font-size:11px;color:#9bab9d;font-weight:400;margin-left:7px">项</span></div><div class="stat-bottom">${note}</div></div>`).join('');
  $('successBanner').innerHTML=textIssues?`<div class="banner">${textIssues} 项正文内容尚未更新到 Word / PPT。${s.repairable?'请预览并确认修复。':'请先确认来源关联。'} <button class="button primary small" id="continueRepair">${s.repairable?'预览修复并生成文件':'审阅候选来源'}</button></div>`:w.last_repair?`<div class="banner">✓ 已修复 ${w.last_repair.count} 项并重新验证。${s.pending?`另有 ${s.pending} 项来源待确认。`:''}<div class="delivery-links">${downloadLinks()}</div></div>`:'';
  if(w.claims.some(c=>c.source==='ocr'))$('successBanner').insertAdjacentHTML('beforeend','<p class="hint">OCR 图片论断只读：候选来源不能确认关联，图片异常请在原文件中人工处理。智能体只处理正文和原生图表。</p>');
  if($('continueRepair'))$('continueRepair').onclick=s.repairable?repairModal:confirmAllModal;
  $('workflow').innerHTML=`<span class="step done"><i>✓</i> 导入文件</span><span class="rule"></span><span class="step ${s.pending===0?'done':''}"><i>${s.pending===0?'✓':'2'}</i> 确认来源</span><span class="rule"></span><span class="step ${s.pending===0?'done':''}"><i>3</i> 验证结论</span><span class="rule"></span><span class="step ${w.last_repair?'done':''}"><i>4</i> 修复与导出</span>`;
  $('suggestButton').disabled=!health?.model.enabled;
  $('suggestButton').title=health?.model.enabled?'将待确认论断及事实元数据发送给 DeepSeek':'DeepSeek 尚未开通，请联系管理员或继续人工确认';
  $('filters').querySelectorAll('button').forEach(b=>b.classList.toggle('selected',b.dataset.filter===filter));
  const claims=w.claims.filter(c=>filter==='all'||(filter==='pending'?!c.confirmed:resultOf(c).status===filter));
  $('claimCount').textContent=`显示 ${claims.length} 项 · 仅覆盖已识别的论断`;
  $('claims').innerHTML=claims.length?claims.map(c=>{
    const r=resultOf(c),f=fileOf(c),names=c.refs.map(id=>w.facts.find(f=>f.id===id)).filter(Boolean).map(f=>`${factName(f)} ${f.value??'缺失'}${f.unit}`);
    return `<article class="claim-card ${r.status==='inconsistent'?'problem':''}"><div class="claim-top"><span class="file-tag">${f.kind==='docx'?'W':f.kind==='pptx'?'P':'X'} · ${esc(kindNames[c.kind])}</span><span class="place" title="${esc(f.name+' · '+c.label)}">${esc(c.label)}</span><span class="badge ${classNames[r.status]}">${esc(statusNames[r.status])}${c.source==='ocr'?' · 候选核验':c.confirmed?'':' · 待确认'}</span></div><div class="claim-text">${c.source==='ocr'?'<span class="badge neutral">OCR · 只读</span> ':''}${esc(c.original)}</div>${r.status==='inconsistent'?`<div class="suggested"><span>建议更新为</span>${esc(r.expected)}</div>`:''}${r.status==='unverifiable'?`<div class="hint">${esc(r.reason)}</div>`:''}<div class="claim-bottom"><span class="evidence-label" title="${esc(names.join('；'))}">${c.confirmed?'✓ 来源已确认':'◷ 候选来源'} · ${esc(names.join(' / ')||'请手动指定')}</span><button class="link-button" data-claim="${c.id}">查看依据 →</button></div></article>`;
  }).join(''):'<div class="empty-list">当前筛选下没有论断。</div>';
  $('confirmAll').disabled=!w.claims.some(c=>c.source!=='ocr'&&c.repairable!==false&&!c.confirmed&&c.refs.length&&resultOf(c).status!=='unverifiable');
  $('reviewRepairs').disabled=!s.repairable;
  $('repairHint').textContent=s.repairable?`${s.repairable} 项已确认来源的结论可修复`:'确认来源后，仅修复已失效的结论';
  $('factInputs').innerHTML=w.facts.map(f=>`<div class="fact-row"><label for="fact-${esc(f.id)}">${esc(factName(f))}<small>${esc(f.sheet+'!'+f.cell)}</small></label><div class="fact-field"><input id="fact-${esc(f.id)}" data-fact="${esc(f.id)}" type="number" step="any" value="${f.value??''}" aria-label="${esc(factName(f))}"><span>${esc(f.unit)}</span></div></div>`).join('');
  if (!$('factDraftStatus')) $('factInputs').insertAdjacentHTML('afterend','<div class="draft-status" id="factDraftStatus"></div><button class="button text full hidden" id="discardDrafts">撤销未应用输入</button>');
  $('demoChange').classList.toggle('hidden',!w.demo);
  document.querySelectorAll('[data-fact]').forEach(input => {if(drafts().has(input.dataset.fact))input.value=drafts().get(input.dataset.fact);});
  renderDraftStatus();
  renderOcr();renderDiagnosis();renderSources();renderGraph();renderHistory();
  if(currentView==='report')renderReport();
  document.querySelectorAll('[data-fact]').forEach(input => input.oninput=()=>trackDraft(input));
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
  $('sourcesView').innerHTML=`<div class="file-grid">${w.documents.map(d=>`<article class="file-card"><span class="file-icon ${d.kind}">${d.kind==='xlsx'?'X':d.kind==='docx'?'W':'P'}</span><h3>${esc(d.name)}</h3><p>${d.kind==='xlsx'?'主要数据源':'关联成果'} · SHA256 ${esc(d.sha256.slice(0,12))}…</p><a class="button secondary small" href="${d.download_url}">↓ 下载当前版本</a></article>`).join('')}</div><section class="panel"><h2>结构化事实表</h2><p class="hint">主体、期间、单位与统计口径共同决定一条事实的含义。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>ID</th><th>主体</th><th>指标</th><th>期间</th><th>数值</th><th>单位</th><th>口径</th><th>位置</th></tr></thead><tbody>${w.facts.map(f=>`<tr>${[f.id,f.subject,f.metric,f.period,f.value??'缺失',f.unit,f.scope,f.sheet+'!'+f.cell].map(x=>`<td>${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div></section><section class="panel" style="margin-top:20px"><h2>检查范围与未识别内容</h2><p class="hint">${segments.length} 个片段没有覆盖受支持论断，可能是标题、普通说明或不支持的表达，不能据此认定正确。</p><ul class="warning-list">${w.documents.flatMap(d=>(d.warnings||[]).map(x=>`<li>${esc(d.name)}：${esc(x)}</li>`)).join('')}<li>不验证业务因果关系或主观评价；不保证任意排版和嵌入对象保真。</li></ul><details><summary class="link-button">查看未识别文本</summary>${segments.map(b=>`<div class="evidence-block"><small>${esc(b.label)}</small><p>${esc(b.text)}</p></div>`).join('')||'<p class="hint">所有可读正文片段都已有受支持论断覆盖。</p>'}</details></section>`;
  $('sourcesView').insertAdjacentHTML('afterbegin',`<section class="panel" style="margin-bottom:20px"><div class="panel-top"><div><h2>追加成果文档</h2><p class="hint">当前项目共 ${w.documents.length} 份文件。Excel 保持唯一，Word / PPT / 图片可分批追加，单批最多 50 份。</p></div><button class="button primary small" id="appendDocuments">＋ 追加文档</button></div></section>`);
  $('appendDocuments').onclick=appendDocumentsModal;
  $('sourcesView').insertAdjacentHTML('beforeend',`<section class="panel" style="margin-top:20px"><h2>已学习关联规则</h2><p class="hint">规则仅用于候选排序与预选，仍需勾选批准。次数仅在人工确认时增加；再次确认会更新来源记录。</p>${rules.length?`<div class="table-scroll"><table class="data-table"><thead><tr><th>规则 key</th><th>事实 ID</th><th>统计口径</th><th>确认次数</th><th>最近来源论断</th><th>最近确认时间</th></tr></thead><tbody>${rules.map(r=>`<tr>${[r.key,r.fact_id,r.scope,r.hits,r.source_claim_id,r.created_at?localTime(r.created_at):''].map(x=>`<td>${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<p class="hint">尚无已确认的复用规则</p>'}</section>`);
}
function appendDocumentsModal(){
  const revision=workspace.revision;
  modal('追加成果文档',`<p class="modal-intro">选择本批新增的 Word、PPT 或图片。Excel 已在项目创建时绑定，不需要重复上传。</p><form id="appendForm"><label class="dropzone"><h3>选择一批文档</h3><p>单批最多 50 份，合计不超过 100MB；可重复操作继续追加</p><input id="appendFiles" type="file" accept=".docx,.pptx,.png,.jpg,.jpeg,.gif,.webp,.bmp,.tiff" multiple required></label><div id="appendStatus" class="upload-file-status">尚未选择文件</div><div class="modal-actions"><button type="button" class="button secondary" id="appendCancel">取消</button><button type="submit" class="button primary" id="appendSubmit" disabled>上传并识别</button></div></form>`);
  const input=$('appendFiles'), submit=$('appendSubmit'), status=$('appendStatus');
  input.onchange=()=>{const files=[...input.files], total=files.reduce((n,f)=>n+f.size,0);status.textContent=files.length?`已选择 ${files.length} 份 · ${(total/1024/1024).toFixed(2)} / 100 MB`:'尚未选择文件';submit.disabled=!files.length||files.length>50||total>100*1024*1024;};
  $('appendCancel').onclick=closeModal;
  $('appendForm').onsubmit=async e=>{e.preventDefault();const form=new FormData();form.set('revision',revision);[...input.files].forEach(f=>form.append('files',f,f.name));const result=await busy('追加文档并重新扫描',()=>api(`/api/projects/${workspace.id}/documents`,{method:'POST',body:form}));if(result){workspace=result;render();await refreshProjects();closeModal();toast(`已追加 ${result.batch.count} 份文档`);}};
}
function renderGraph(){
  const w=workspace,cs=graphFact?w.claims.filter(c=>c.refs.includes(graphFact)):w.claims;
  $('graphView').innerHTML=`<p class="graph-note">选择左侧事实，查看依赖它的论断。虚线含义的候选关系须经人工确认，才可用于自动修复。</p><div class="graph-columns"><div class="graph-column"><h3>来源事实 · ${w.facts.length}</h3><button class="link-button" id="clearGraph" style="margin-bottom:15px">显示全部关联</button>${w.facts.map(f=>`<button class="graph-node ${graphFact===f.id?'selected':''}" style="width:100%;text-align:left" data-graph-fact="${esc(f.id)}">${esc(factName(f))}<small>${esc(f.value)} ${esc(f.unit)} · ${esc(f.scope)}</small></button>`).join('')}</div><div class="graph-arrow">→</div><div class="graph-column"><h3>依赖论断 · ${cs.length}</h3>${cs.map(c=>`<div class="graph-node"><div style="display:flex;justify-content:space-between;gap:10px"><span>${esc(c.original)}</span><span class="badge ${classNames[resultOf(c).status]}">${statusNames[resultOf(c).status]}</span></div><small>${esc(fileOf(c).name)} · ${esc(c.label)}</small><div class="graph-ref">${c.confirmed?'已确认':'候选'} · ${esc(c.refs.join(' + '))}</div><button class="link-button" data-claim="${c.id}" style="margin-top:8px">查看计算与依据 →</button></div>`).join('')||'<div class="empty-list">没有关联论断</div>'}</div></div>`;
}
function renderHistory(){
  $('historyView').innerHTML=`<section class="panel"><h2>变更与核验记录</h2><p class="hint">文件变更创建新版本；确认来源和模型建议也会留下记录。</p>${workspace.audit.slice().reverse().map(a=>`<div class="history-row"><time>${esc(localTime(a.time))}</time><div><h3>${esc(a.event)}</h3><p>${esc(a.detail)}</p></div></div>`).join('')}</section>`;
  const agent=workspace.agent;
  if(agent){
    const nodes={run:'启动任务',prepare:'准备',list_facts:'读取事实',list_claims:'读取论断',propose_links:'规则候选',suggest_links_llm:'DeepSeek 关联建议',model_skipped:'跳过模型',link:'整理关联',check_claim:'核验论断',diagnose:'汇总诊断',ask:'等待人工',decide:'人工批准',confirm_links:'确认来源',repair:'修复文件',verify:'复核',done:'完成'};
    const phases={idle:'执行未完成',running:'执行中',awaiting_decision:'等待人工决定',done:'已完成'};
    $('historyView').insertAdjacentHTML('afterbegin',`<section class="panel"><h2>智能体执行轨迹 · ${esc(phases[agent.phase])}</h2><p class="hint">任务 ${esc(agent.run_id)}${agent.revision!==workspace.revision?' · 数据已更新，请重新运行':''}</p>${agent.trace.map(t=>`<div class="history-row"><time>${esc(localTime(t.at))}</time><div><h3>${esc(nodes[t.node]||t.node)}</h3><p>${esc(t.summary)}</p><small>${t.model?'模型请求：'+esc(t.model):'未调用模型'} · ${esc(t.duration_ms)} ms</small></div></div>`).join('')}</section>`);
  }
}
const ocrDrafts=new Map();
function renderOcr(){
  const images=workspace.images||[], enabled=health?.ocr?.enabled;
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
  const card=r=>`<article class="claim-card ${r.code==='inconsistent'?'problem':''}">
    <div class="claim-top"><span class="badge ${categoryClasses[r.category]||'amber'}">${esc(r.category)}</span><span class="file-tag">${esc(r.file_name)} · ${esc(r.label)}</span><span class="badge ${r.confirmed?'neutral':'amber'}">${r.source==='ocr'?'图片文字已核对 · 来源为候选':r.confirmed?'来源已确认':'来源待确认'}</span></div>
    <div class="claim-text">${r.source==='ocr'?'<span class="badge neutral">OCR · 只读</span> ':''}${esc(r.original)}</div>
    <p class="hint">${r.kind==='chart'?'原生图表对象，无正文字符区间':`文本区间：[${esc(r.start)}, ${esc(r.end)})（从 0 起，右端不含）`}</p>
    <details><summary class="link-button">查看定位锚点与诊断码</summary><p class="hint">${esc(r.location)} · ${esc(r.code)}</p></details>
    <p><strong>技术原因：</strong>${esc(r.reason||'无')}</p>
    <p><strong>确定性解释：</strong>${esc(r.explanation)}</p>
    ${explanations[r.claim_id]?`<p><strong>DeepSeek 润色（仅供参考）：</strong>${esc(explanations[r.claim_id])}</p>`:''}
    <p><strong>修复建议：</strong>${esc(r.fix_hint)}</p>
    ${r.expected&&r.code==='inconsistent'?`<div class="suggested"><span>程序计算的建议文本</span>${esc(r.expected)}</div>`:''}
    ${(r.evidence||[]).length?`<div class="evidence-block"><strong>Excel 证据</strong>${r.evidence.map(f=>`<p>${esc(f.sheet)}!${esc(f.cell)} · 事实 ID：${esc(f.id)}<br>主体：${esc(f.subject)} · 指标：${esc(f.metric)} · 期间：${esc(f.period)}<br>数值：${esc(facts.get(f.id)?.value??'缺失')} · 单位：${esc(f.unit)} · 口径：${esc(f.scope)}</p>`).join('')}</div>`:'<p class="hint">暂无可用 Excel 单元格证据，请先审阅来源。</p>'}
    <button class="link-button" data-claim="${esc(r.claim_id)}">审阅来源与依据 →</button></article>`;
  const anomalies=records.filter(r=>r.code!=='consistent'), passed=records.filter(r=>r.code==='consistent');
  const groups=['结论失效','数据缺失','来源冲突','口径不一致','需人工复核'].map(category=>{
    const items=anomalies.filter(r=>r.category===category);
    return items.length?`<h3 class="section-heading">${category} · ${items.length} 项</h3>${items.map(card).join('')}`:'';
  }).join('');
  $('diagnosisView').innerHTML=`<section class="panel"><div class="panel-top"><div><h2>异常诊断</h2><p class="hint">共 ${summary.total??records.length} 条已识别论断 · ${anomalies.length} 项异常</p></div><button class="button primary small" id="explainDiagnosis">生成诊断解释</button></div>
    <p class="hint">${health?.model.enabled?'点击后将论断、诊断原因、期望文本及事实元数据发送给 DeepSeek 润色。':'未配置 DeepSeek，当前使用确定性模板解释，不会发送模型请求。'} 来源确认和文件修复仍需人工批准。</p>
    ${groups||'<p class="hint">未发现异常。仅覆盖已识别的论断，候选来源仍需确认。</p>'}
    ${passed.length?`<details><summary class="link-button">查看计算通过的论断与证据（${passed.length} 项）</summary>${passed.map(card).join('')}</details>`:''}</section>`;
  const button=$('explainDiagnosis');
  button.onclick=async()=>{
    if(!health?.model.enabled){toast('未配置 DeepSeek，继续使用确定性模板解释，未发送请求');return;}
    const result=await post('diagnosis/explain',{},'DeepSeek 正在润色解释，最长约60秒');
    if(result)toast(Object.keys(result.diagnosis_explanations||{}).length?'诊断解释已生成；未返回的项继续使用模板':'未收到可用模型解释，继续使用确定性模板');
  };
}
function agentModal(){
  if(workspace.agent?.phase==='awaiting_decision'&&workspace.agent.revision===workspace.revision){showAgentResult();return;}
  const projectId=workspace.id, revision=workspace.revision;
  modal('运行证据链验证智能体',`<p class="modal-intro">按准备、关联、诊断、人工决定、修复、复核的顺序处理当前已保存的数据。来源确认和文件修复分别等待你批准。</p><p class="hint">${health?.local_reranker?.enabled?'本地 BGE 会先处理规则多候选和零候选语义改写；低置信样本再升级 API。':health?.model.enabled?'关联阶段会把最多40条未确认文本论断及事实元数据发送给 DeepSeek，可能产生调用费用；模型只提出候选。':'无模型时使用规则候选，功能仍可运行。'} 图表使用规则来源。关闭待办窗口后可继续。${quota&&typeof quota.client_remaining==='number'?` 今日剩余额度：本访客 ${quota.client_remaining} 次，全局 ${quota.global_remaining} 次。`:''}</p>${drafts().size?'<p class="hint">右侧还有未应用的数值。若要核验这些变更，请先关闭窗口并点击“更新数据并验证”。</p>':''}<div class="modal-actions"><button class="button primary" id="startAgent">开始运行</button></div>`);
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
  const routeHint=route.zero_candidate_claims!=null?`<p class="hint">本次路由：规则零候选 ${route.zero_candidate_claims} 项 · 本地零候选召回 ${route.local_zero_recall_claims||0} 项 · API 难例 ${route.api_candidate_claims||0} 项。</p>`:'';
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
  modal('导入文件，建立项目',`<p class="modal-intro">支持的浏览器会自动打开可写文件选择器。选择后，修复完成时可直接覆盖本机原文件，无需额外勾选；也可继续使用普通上传。</p><p class="hint">一份 Excel 事实表，加一份或多份 Word / PPT。可以分多次选择，后续选择会加入列表，不会替换之前的文件。</p><form id="uploadForm"><label class="field-label" for="uploadName">项目名称</label><input id="uploadName" class="text-input" name="name" value="我的数据分析项目" maxlength="100" required><label class="dropzone"><h3>选择并加入文件</h3><p>可重复打开文件选择器；最多10份，合计不超过30MB</p><input id="uploadFiles" type="file" accept=".xlsx,.docx,.pptx" multiple></label>${window.showOpenFilePicker?'<button type="button" class="button secondary full" id="pickWritableFiles">选择文件（支持修改后写回原文件）</button><p class="hint">导入后可显式点击“直接修改本地文件”覆盖所选原文件，浏览器可能请求写入权限。刷新页面后需重新选择并导入；同名文件以最后一次选择为准。</p>':'<p class="hint">当前浏览器不支持原文件写回，请使用浏览器下载。</p>'}<div id="uploadFileList" class="upload-file-list"></div><div id="uploadFileStatus" class="upload-file-status">尚未加入文件</div><a class="link-button" href="/api/template">↓ 下载 Excel 事实表模板</a><p class="hint">需要一份 .xlsx 和至少一份 .docx / .pptx。Excel 首行包含：事实ID、主体、指标、期间、数值、单位、统计口径。</p><div class="modal-actions"><button type="button" class="button secondary" id="modalDemo">先体验演示</button><button class="button primary" id="uploadSubmit" type="submit" disabled>导入并识别</button></div></form>`);
  $('modalDemo').onclick=demo;
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
  modal('模型与运行方式',`<p class="modal-intro">当前模式：${health?.model.enabled?esc(health.model.provider)+' · '+esc(health.model.model):'规则识别与人工确认，无模型调用'}</p><div class="help-section"><h3>DeepSeek 辅助关联</h3><p>服务由管理员统一开通，你无需提供 API Key。点击“DeepSeek 关联建议”获取候选来源，在“查看依据”中审阅并确认。服务未开通时，请联系管理员或继续人工确认。</p></div><div class="help-section"><h3>模型做什么</h3><p>关联接口为已识别的待确认正文论断提出来源建议；诊断接口可润色解释，不会自动确认、改文件或执行代码。计算、口径校验与文件复核始终由程序完成。任意自然语言论断抽取尚未实现。</p></div><div class="help-section"><h3>本地与公网</h3><p>本机使用不需要服务器。公网部署需要设置访问密码与HTTPS，并配置模型额度。当前系统是共享工作空间，尚未实现团队成员分级权限。</p></div><div class="help-section"><h3>发送给模型的内容</h3><p>点击“DeepSeek 关联建议”，或在启用模型时确认“开始运行”智能体，会发送待确认正文论断及事实元数据。原文件不会直接上传到模型服务。返回建议必须由你核对。</p></div>`);
  $('modalContent').insertAdjacentHTML('beforeend',`<div class="help-section"><h3>OCR 图片文字</h3><p>在“图片文字”中逐张运行 OCR，会发送抽取的图片给 DeepSeek，需要模型支持多模态输入。识别完成后必须人工核对，确认后的文字只读验证，不支持关联确认或修复图片。关闭 OCR 可设置 ZHILIAN_OCR_BACKEND=off。</p></div><div class="help-section"><h3>修复完成后自动落盘</h3><label><input type="checkbox" id="autoExportSetting" ${localStorage.getItem('zhilian-auto-export')==='1'?'checked':''}> 智能体完成时自动把当前 Excel、Word、PPT 和变更报告写入本地输出目录</label><p class="hint">默认关闭。此设置只影响当前浏览器；服务器端也可用 ZHILIAN_AUTO_EXPORT=1 开启。</p></div>`);
  $('autoExportSetting').onchange=e=>{if(e.target.checked)localStorage.setItem('zhilian-auto-export','1');else localStorage.removeItem('zhilian-auto-export');};
}

document.addEventListener('click',e=>{
  const nav=e.target.closest('[data-view]');if(nav)switchView(nav.dataset.view);
  const project=e.target.closest('[data-project]');if(project)loadProject(project.dataset.project);
  const claim=e.target.closest('[data-claim]');if(claim)claimModal(claim.dataset.claim);
  const graph=e.target.closest('[data-graph-fact]');if(graph){graphFact=graph.dataset.graphFact;renderGraph();}
  if(e.target.closest('#clearGraph')){graphFact=null;renderGraph();}
});
$('filters').onclick=e=>{const b=e.target.closest('[data-filter]');if(b){filter=b.dataset.filter;render();}};
$('closeModal').onclick=closeModal;
$('modal').addEventListener('click',e=>{if(e.target===$('modal'))closeModal();});
$('newProject').onclick=()=>uploadModal(true);$('welcomeUpload').onclick=()=>uploadModal(true);$('loadDemo').onclick=demo;
$('helpButton').onclick=helpModal;$('scopeButton').onclick=helpModal;$('settingsButton').onclick=settingsModal;
$('confirmAll').onclick=confirmAllModal;$('reviewRepairs').onclick=repairModal;
$('agentButton').onclick=agentModal;
$('demoChange').insertAdjacentHTML('afterend','<button class="button secondary full" id="importSource">导入更新后的 Excel</button>');
$('importSource').onclick=importSourceModal;
  $('saveFacts').onclick=async()=>{
  const values={};for(const input of document.querySelectorAll('[data-fact]')){const old=workspace.facts.find(f=>f.id===input.dataset.fact);if(input.value.trim()===''){toast('数值不能为空',true);return;}const value=Number(input.value);if(!Number.isFinite(value)){toast('请输入有效数字',true);return;}if(value!==old.value)values[old.id]=value;}
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
  const badge=$('modelBadge');
  if(badge){
    badge.textContent=(health&&health.model.enabled?'DeepSeek 已配置':'规则模式 · 无需 API')+quotaLabel(quota);
    badge.title=quotaTitle(quota);
  }
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


