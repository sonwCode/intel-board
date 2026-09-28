const state = { claims: [], filtered: [], sources: [] };
const demoClaims = [
  {id:1,title:'Gemini API 开发者额度',category:'deal',benefit:'新开发者可获得试用额度，适合原型测试。',conditions:['需要开发者账号'],risk_level:'low',status:'published',confidence:.92,source_url:'https://ai.google.dev/',evidence:'官方文档说明新账号可使用试用额度。',published_at:'2026-09-28'},
  {id:2,title:'开源 CLI 版本更新',category:'tech',benefit:'修复连接失败和 JSON 解析异常。',conditions:['升级到最新版本'],risk_level:'low',status:'published',confidence:.88,source_url:'https://github.com/',evidence:'发布说明列出连接稳定性和解析修复。',published_at:'2026-09-28'},
  {id:3,title:'第三方 API 试用活动',category:'deal',benefit:'提供短期体验额度，需阅读服务条款。',conditions:['注册账号','确认计费规则'],risk_level:'medium',status:'published',confidence:.64,source_url:'https://example.com/',evidence:'社区帖子提到试用额度，但尚未找到官方公告。',published_at:'2026-09-28'}
];
const $ = (id) => document.getElementById(id);
function escapeHtml(value='') { return String(value).replace(/[&<>"']/g, (c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c])); }
function normalizeItem(item) {
  const risk = String(item.risk_level || 'INFO').toLowerCase();
  return { ...item, category:item.category || 'intel', benefit:item.benefit || item.summary || item.content || '', evidence:item.evidence || item.content || item.summary || '暂无证据', conditions:item.conditions || item.tags || [], risk_level:risk, status:item.status || 'published', confidence:item.confidence ?? (risk==='high' ? .72 : .86) };
}
function riskText(r) { return ({low:'低风险',medium:'中风险',high:'高风险',info:'提示'})[r] || r; }
function statusText(s) { return s === 'published' ? '已发布' : (s === 'approved' ? '已审核' : s || '待处理'); }
function renderStats() {
  const claims = state.claims;
  $('statTotal').textContent = claims.length;
  $('statApproved').textContent = claims.filter(c=>c.status==='published' || c.status==='approved').length;
  $('statPending').textContent = claims.filter(c=>c.status!=='published' && c.status!=='approved').length;
  $('statHighRisk').textContent = claims.filter(c=>c.risk_level==='high').length;
  $('resultCount').textContent = `${state.filtered.length} 条结果`;
}
function renderCategories() {
  const current = $('categorySelect').value;
  const cats = [...new Set(state.claims.map(c=>c.category).filter(Boolean))].sort();
  $('categorySelect').innerHTML = '<option value="">全部分类</option>' + cats.map(c=>`<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  $('categorySelect').value = cats.includes(current) ? current : '';
}
function sourceStatus(source) {
  if (source.last_error) return `失败：${source.last_error}`;
  if (source.last_success_at) return `最近成功：${source.last_success_at}`;
  return '尚未同步';
}
function renderSources() {
  const root = $('sources');
  if (!root) return;
  if (!state.sources.length) {
    root.innerHTML = '<div class="empty source-empty">还没有配置来源。添加一个 RSS / Atom 地址后即可手动同步。</div>';
    return;
  }
  root.innerHTML = state.sources.map(source => `
    <article class="source-row">
      <div class="source-info">
        <strong>${escapeHtml(source.name)}</strong>
        <a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.url)}</a>
        <span class="meta">${escapeHtml(source.kind.toUpperCase())} · ${escapeHtml(sourceStatus(source))}</span>
      </div>
      <button class="button secondary sync-source" data-source-id="${source.id}" ${source.enabled ? '' : 'disabled'}>${source.enabled ? '立即同步' : '已停用'}</button>
    </article>`).join('');
  root.querySelectorAll('.sync-source').forEach(button => button.addEventListener('click', () => syncSource(Number(button.dataset.sourceId))));
}
async function loadSources() {
  try {
    const response = await fetch('/api/sources');
    if (!response.ok) throw new Error('source API unavailable');
    const data = await response.json();
    state.sources = Array.isArray(data) ? data : (data.sources || []);
  } catch (_) {
    state.sources = [];
  }
  renderSources();
}
async function createSource(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const message = $('sourceMessage');
  const formData = new FormData(form);
  const payload = {
    name: String(formData.get('name') || '').trim(),
    url: String(formData.get('url') || '').trim(),
    kind: String(formData.get('kind') || 'rss')
  };
  message.className = 'form-message';
  message.textContent = '添加中…';
  try {
    const response = await fetch('/api/sources', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || '添加失败');
    message.className = 'form-message success';
    message.textContent = '来源已添加。';
    form.reset();
    await loadSources();
  } catch (error) {
    message.className = 'form-message error';
    message.textContent = error.message || '添加失败，请检查地址。';
  }
}
async function syncSource(sourceId) {
  const message = $('sourceMessage');
  message.className = 'form-message';
  message.textContent = '同步中…';
  try {
    const response = await fetch(`/api/sources/${sourceId}/sync`, {method:'POST'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || '同步失败');
    const run = data.run || {};
    message.className = run.status === 'failed' ? 'form-message error' : 'form-message success';
    message.textContent = run.status === 'failed' ? (run.error || '同步失败') : `同步完成：新增 ${run.inserted_count || 0} 条，跳过 ${run.skipped_count || 0} 条。`;
    await Promise.all([loadSources(), loadClaims()]);
  } catch (error) {
    message.className = 'form-message error';
    message.textContent = error.message || '同步失败。';
  }
}
function renderClaims() {
  const root = $('claims');
  if (!state.filtered.length) { root.innerHTML = '<div class="empty">没有匹配的情报，换个关键词或筛选条件试试。</div>'; return; }
  root.innerHTML = state.filtered.map(c => `
    <article class="claim">
      <div class="claim-top"><div><h3>${escapeHtml(c.title)}</h3><div class="meta">${escapeHtml(c.category || '未分类')} · ${escapeHtml(c.published_at || '')} · 置信度 ${Math.round((c.confidence || 0) * 100)}%</div></div>
        <div class="badges"><span class="badge ${escapeHtml(c.status)}">${statusText(c.status)}</span><span class="badge ${escapeHtml(c.risk_level)}">${riskText(c.risk_level)}</span></div>
      </div>
      <p>${escapeHtml(c.benefit || '')}</p>
      <p class="meta">标签/条件：${escapeHtml((c.conditions || []).join('、') || '未注明')}</p>
      <p class="evidence">证据：${escapeHtml(c.evidence || '暂无证据')}</p>
      <p class="meta"><a href="${escapeHtml(c.source_url || '#')}" target="_blank" rel="noreferrer">${escapeHtml(c.source_url || '无来源')}</a></p>
    </article>`).join('');
}
function applyFilters() {
  const q = $('searchInput').value.trim().toLowerCase();
  const category = $('categorySelect').value;
  const status = $('statusSelect').value;
  const risk = $('riskSelect').value;
  state.filtered = state.claims.filter(c => {
    const haystack = [c.title,c.benefit,c.evidence,c.source_url,c.category].join(' ').toLowerCase();
    const statusMatch = !status || status === c.status || (status === 'approved' && c.status === 'published');
    return (!q || haystack.includes(q)) && (!category || c.category===category) && statusMatch && (!risk || c.risk_level===risk);
  });
  renderStats(); renderClaims();
}
async function loadClaims() {
  $('lastUpdated').textContent = '加载中…';
  try {
    const response = await fetch('/api/claims');
    if (!response.ok) throw new Error('API unavailable');
    const data = await response.json();
    state.claims = (Array.isArray(data) ? data : []).map(normalizeItem);
  } catch (_) {
    state.claims = demoClaims;
  }
  renderCategories(); applyFilters();
  $('lastUpdated').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`;
}
async function generateReport() {
  $('reportBody').textContent = '正在读取…';
  try {
    const response = await fetch('/api/reports/today', {method:'POST'});
    if (!response.ok) throw new Error('report API unavailable');
    const data = await response.json();
    $('reportBody').textContent = data.body_markdown || data.report?.content_markdown || '当前还没有报告。';
    $('reportDate').textContent = data.report_date || data.report?.report_date || '';
  } catch (_) {
    const lines = ['### 今日情报报告','',`生成时间：${new Date().toLocaleString('zh-CN')}`,''];
    state.claims.forEach((c,i)=>lines.push(`#### ${i+1}. ${c.title}`,`- 分类：${c.category}`,`- 内容：${c.benefit}`,`- 证据：${c.evidence}`,`- 来源：${c.source_url}`,''));
    $('reportBody').textContent = lines.join('\n');
    $('reportDate').textContent = new Date().toISOString().slice(0,10);
  }
}
function slugFromTitle(title) {
  const ascii = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return `${ascii || 'intel-item'}-${Date.now()}`;
}
async function createItem(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const message = $('formMessage');
  const formData = new FormData(form);
  const tags = String(formData.get('tags') || '').split(',').map(tag => tag.trim()).filter(Boolean);
  const payload = {
    slug: slugFromTitle(String(formData.get('title') || '')),
    title: String(formData.get('title') || '').trim(),
    category: String(formData.get('category') || 'intel'),
    source: String(formData.get('source') || '').trim(),
    source_url: String(formData.get('source_url') || '').trim() || null,
    summary: String(formData.get('summary') || '').trim(),
    content: String(formData.get('content') || '').trim(),
    risk_level: String(formData.get('risk_level') || 'INFO'),
    tags
  };
  message.className = 'form-message';
  message.textContent = '保存中…';
  try {
    const response = await fetch('/api/items', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || '保存失败');
    message.className = 'form-message success';
    message.textContent = '已保存，列表已刷新。';
    form.reset();
    await loadClaims();
  } catch (error) {
    message.className = 'form-message error';
    message.textContent = error.message || '保存失败，请检查服务是否启动。';
  }
}
async function refreshAll() { await Promise.all([loadClaims(), loadSources()]); }
$('refreshBtn').addEventListener('click', refreshAll);
$('reportBtn').addEventListener('click', generateReport);
if ($('itemForm')) $('itemForm').addEventListener('submit', createItem);
if ($('sourceForm')) $('sourceForm').addEventListener('submit', createSource);
['searchInput','categorySelect','statusSelect','riskSelect'].forEach(id => $(id).addEventListener('input', applyFilters));
loadClaims();
loadSources();


