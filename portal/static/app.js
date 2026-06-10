(function() {
'use strict';

// === Tab Switching ===
const tabs = document.querySelectorAll('.app-tab');
const panels = document.querySelectorAll('.tab-panel');
tabs.forEach(btn => btn.addEventListener('click', () => {
  tabs.forEach(t => t.classList.remove('active'));
  panels.forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
}));

// === Utilities ===
function escHtml(s) { return s ? s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') : ''; }

async function api(url, method, body) {
  try {
    const opts = { method: method || 'GET' };
    if (body) opts.body = body;
    const res = await fetch(url, opts);
    return await res.json();
  } catch(e) { return null; }
}

function makeDrop(container, name, label, accept, formId) {
  const el = document.createElement('label');
  el.className = 'drop';
  el.textContent = label;
  const input = document.createElement('input');
  input.name = name; input.type = 'file'; input.accept = accept;
  if (formId) input.setAttribute('form', formId);
  const span = document.createElement('span');
  span.textContent = '未上传';
  const rmBtn = document.createElement('button');
  rmBtn.className = 'removeMediaBtn'; rmBtn.type = 'button'; rmBtn.textContent = '移除';
  rmBtn.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); input.value=''; el.classList.remove('hasPreview'); el.querySelector('.preview')?.remove(); span.textContent='未上传'; });
  el.append(input, span, rmBtn);
  wireFileDrop(el, input, name);
  container.appendChild(el);
}

function wireFileDrop(drop, input, name) {
  input.addEventListener('change', () => {
    const f = input.files?.[0];
    if (!f) { drop.classList.remove('hasPreview'); drop.querySelector('.preview')?.remove(); drop.querySelector('span').textContent='未上传'; return; }
    showPreview(drop, name, URL.createObjectURL(f), f.name);
  });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('isDragging'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('isDragging'));
  drop.addEventListener('drop', e => {
    e.preventDefault(); drop.classList.remove('isDragging');
    const f = e.dataTransfer?.files?.[0]; if (!f) return;
    const dt = new DataTransfer(); dt.items.add(f); input.files = dt.files;
    input.dispatchEvent(new Event('change', {bubbles:true}));
  });
}

function showPreview(drop, name, url, filename) {
  drop.classList.add('hasPreview');
  drop.querySelector('.preview')?.remove();
  const kind = name.includes('video') ? 'video' : name.includes('audio') ? 'audio' : 'image';
  const media = document.createElement(kind === 'image' ? 'img' : kind);
  media.className = 'preview'; media.src = url;
  if (kind !== 'image') media.controls = true;
  if (kind !== 'audio') media.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); openPreview(kind, url); });
  drop.insertBefore(media, drop.querySelector('span'));
  drop.querySelector('span').textContent = filename || '已上传';
}

function openPreview(kind, url) {
  const dlg = document.getElementById('previewDialog');
  const body = document.getElementById('previewDialogBody');
  body.innerHTML = '';
  const m = document.createElement(kind === 'image' ? 'img' : 'video');
  m.src = url; if (kind === 'video') m.controls = true;
  body.append(m); dlg.showModal();
}

document.getElementById('closePreviewBtn').addEventListener('click', () => document.getElementById('previewDialog').close());
document.getElementById('previewDialog').addEventListener('click', e => { if(e.target.id==='previewDialog') e.target.close(); });

// === Build upload slots ===
const sdImgRefs = document.getElementById('sd-imageRefs');
const sdVidRefs = document.getElementById('sd-videoRefs');
const sdAudRefs = document.getElementById('sd-audioRefs');
for (let i=1;i<=9;i++) makeDrop(sdImgRefs, `ref_image_${i}`, `@ref_image${i}`, 'image/*', 'sd-form');
for (let i=1;i<=3;i++) { makeDrop(sdVidRefs, `ref_video_${i}`, `参考视频${i}`, 'video/*', 'sd-form'); makeDrop(sdAudRefs, `ref_audio_${i}`, `参考音频${i}`, 'audio/*', 'sd-form'); }

const nbImgRefs = document.getElementById('nb-imageRefs');
for (let i=1;i<=14;i++) makeDrop(nbImgRefs, `image_${i}`, `Image ${i}`, 'image/*', 'nb-form');

// Wire existing drops (first/last frame in seedance + dreamina)
document.querySelectorAll('.drop').forEach(drop => {
  const input = drop.querySelector('input[type="file"]');
  if (input && !input.dataset.wired) { input.dataset.wired='1'; wireFileDrop(drop, input, input.name); }
});

// === Seedance ===
const sdForm = document.getElementById('sd-form');
const sdSubmit = document.getElementById('sd-submitBtn');
const sdStatus = document.getElementById('sd-statusText');
const sdResults = document.getElementById('sd-results');
const sdEvents = document.getElementById('sd-events');

sdForm.addEventListener('submit', async e => {
  e.preventDefault();
  sdSubmit.disabled = true; sdSubmit.textContent = '生成中';
  sdStatus.textContent = '提交中'; sdResults.innerHTML = ''; sdEvents.textContent = '';
  const data = new FormData(sdForm);
  const res = await api('/seedance/api/jobs', 'POST', data);
  if (!res || res.error) { sdSubmit.disabled=false; sdSubmit.textContent='开始生成'; sdStatus.textContent=res?.error||'提交失败'; return; }
  pollSd(res.job_id);
});

async function pollSd(jobId) {
  while (true) {
    const job = await api(`/seedance/api/jobs/${jobId}`);
    if (!job) break;
    sdStatus.textContent = `${job.status} ${job.done||0}/${job.total||0}`;
    sdEvents.textContent = (job.events||[]).map(e=>`[${e.time}] ${e.message}`).join('\n');
    sdResults.innerHTML = '';
    for (const r of job.results||[]) {
      sdResults.innerHTML += `<article class="result"><video controls src="${r.download_url}"></video><a href="${r.download_url}" download="${r.filename}">下载</a><div class="meta">Run ${r.index} · ${r.task_id}</div></article>`;
    }
    for (const err of job.errors||[]) sdResults.innerHTML += `<article class="result">${escHtml(err)}</article>`;
    if (['succeeded','failed'].includes(job.status)) break;
    await new Promise(r => setTimeout(r, 2500));
  }
  sdSubmit.disabled=false; sdSubmit.textContent='开始生成';
}

// Seedance archives
document.getElementById('sd-savePresetBtn').addEventListener('click', async () => {
  const data = new FormData(sdForm);
  const res = await api('/seedance/api/preset', 'POST', data);
  document.getElementById('sd-presetHint').textContent = res?.archive ? `已保存: ${res.archive}` : (res?.error||'保存失败');
  loadSdArchives();
});
document.getElementById('sd-loadArchiveBtn').addEventListener('click', async () => {
  const name = document.getElementById('sd-archiveSelect').value;
  if (!name) return;
  const data = new FormData(); data.set('archive_name', name);
  const res = await api('/seedance/api/archive/load', 'POST', data);
  if (res && res.values) { for (const [k,v] of Object.entries(res.values)) { const el=sdForm.elements[k]; if(el&&el.type!=='file') { if(el.type==='checkbox') el.checked=['on','true','1'].includes(v); else el.value=v; } } }
  document.getElementById('sd-presetHint').textContent = `已读取: ${name}`;
});
document.getElementById('sd-deleteArchiveBtn').addEventListener('click', async () => {
  const name = document.getElementById('sd-archiveSelect').value;
  if (!name) return;
  const data = new FormData(); data.set('archive_name', name);
  await api('/seedance/api/archive/delete', 'POST', data);
  loadSdArchives();
});
async function loadSdArchives() {
  const res = await api('/seedance/api/archives');
  const sel = document.getElementById('sd-archiveSelect'); sel.innerHTML='';
  for (const a of res?.archives||[]) { const o=document.createElement('option'); o.value=a.name; o.textContent=a.name; sel.append(o); }
}
async function loadSdConfig() {
  const res = await api('/seedance/api/config');
  if (res) document.getElementById('sd-keyHint').textContent = res.has_key ? `已检测到 key: ${res.masked_key}` : '未检测到本地 key';
}

function bindProviderSwitch(prefix, appPath, form) {
  let providersData = {};
  async function loadProviders() {
    const res = await api(`${appPath}/api/config`);
    if (!res?.providers) return;
    providersData = res.providers;
    const providerSel = form.elements['provider'];
    providerSel.innerHTML = '';
    for (const [key, cfg] of Object.entries(res.providers)) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = cfg.label || key;
      providerSel.append(opt);
    }
    if (res.default_provider) providerSel.value = res.default_provider;
    applyProvider(providerSel.value);
    const hint = document.getElementById(`${prefix}-keyHint`);
    if (hint) hint.textContent = res.has_key ? `已检测到 key: ${res.masked_key}` : '未检测到本地 key';
  }
  function applyProvider(provider) {
    const cfg = providersData[provider];
    if (!cfg) return;
    if (cfg.base_url) form.elements['base_url'].value = cfg.base_url;
    const hintEl = document.getElementById(`${prefix}-providerHint`);
    if (hintEl) hintEl.textContent = cfg.hint || '';
    const modelSel = form.elements['model'];
    const prevModel = modelSel.value;
    modelSel.innerHTML = '';
    for (const m of cfg.models || []) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.label || m.id;
      modelSel.append(opt);
    }
    if ([...modelSel.options].some(o => o.value === prevModel)) {
      modelSel.value = prevModel;
    }
  }
  form.elements['provider'].addEventListener('change', e => applyProvider(e.target.value));
  loadProviders();
}

// Seedance output dir buttons
function bindOutputDirBtns(prefix, appPath, form) {
  document.getElementById(`${prefix}-chooseOutputBtn`)?.addEventListener('click', async () => {
    const res = await api(`${appPath}/api/choose-output-dir`, 'POST');
    if (res?.path) form.elements['output_dir'].value = res.path;
  });
  document.getElementById(`${prefix}-appOutputBtn`)?.addEventListener('click', () => {
    form.elements['output_dir'].value = '';
  });
  document.getElementById(`${prefix}-desktopOutputBtn`)?.addEventListener('click', async () => {
    const res = await api(`${appPath}/api/default-output-dir`);
    if (res?.path) form.elements['output_dir'].value = res.path;
  });
  document.getElementById(`${prefix}-openOutputBtn`)?.addEventListener('click', async () => {
    const data = new FormData(); data.set('output_dir', form.elements['output_dir'].value);
    await api(`${appPath}/api/open-output-dir`, 'POST', data);
  });
  document.getElementById(`${prefix}-cleanCacheBtn`)?.addEventListener('click', async () => {
    const res = await api(`${appPath}/api/cleanup-cache`, 'POST');
    if (res) alert(`清理完成：素材 ${res.media_deleted||0} 个，日志 ${res.logs_deleted||0} 个`);
  });
}
bindOutputDirBtns('sd', '/seedance', sdForm);

// === Nano Banana ===
const nbForm = document.getElementById('nb-form');
const nbSubmit = document.getElementById('nb-submitBtn');
const nbStatus = document.getElementById('nb-statusText');
const nbResults = document.getElementById('nb-results');
const nbEvents = document.getElementById('nb-events');
bindOutputDirBtns('nb', '/nano-banana', nbForm);

nbForm.addEventListener('submit', async e => {
  e.preventDefault();
  nbSubmit.disabled = true; nbSubmit.textContent = '生成中';
  nbStatus.textContent = '提交中'; nbResults.innerHTML = ''; nbEvents.textContent = '';
  const data = new FormData(nbForm);
  const res = await api('/nano-banana/api/jobs', 'POST', data);
  if (!res || res.error) { nbSubmit.disabled=false; nbSubmit.textContent='开始生成'; nbStatus.textContent=res?.error||'提交失败'; return; }
  pollNb(res.job_id);
});

async function pollNb(jobId) {
  while (true) {
    const job = await api(`/nano-banana/api/jobs/${jobId}`);
    if (!job) break;
    nbStatus.textContent = `${job.status} ${job.done||0}/${job.total||0}`;
    nbEvents.textContent = (job.events||[]).map(e=>`[${e.time}] ${e.message}`).join('\n');
    nbResults.innerHTML = '';
    for (const r of job.results||[]) {
      nbResults.innerHTML += `<article class="result"><img src="${r.download_url}" style="width:100%;border-radius:6px"><a href="${r.download_url}" download="${r.filename}">下载</a><div class="meta">Run ${r.index}</div></article>`;
    }
    for (const err of job.errors||[]) nbResults.innerHTML += `<article class="result">${escHtml(err)}</article>`;
    if (['succeeded','failed'].includes(job.status)) break;
    await new Promise(r => setTimeout(r, 2500));
  }
  nbSubmit.disabled=false; nbSubmit.textContent='开始生成';
}

// NB archives
document.getElementById('nb-savePresetBtn').addEventListener('click', async () => {
  const data = new FormData(nbForm);
  const res = await api('/nano-banana/api/preset', 'POST', data);
  document.getElementById('nb-presetHint').textContent = res?.archive ? `已保存: ${res.archive}` : (res?.error||'保存失败');
  loadNbArchives();
});
document.getElementById('nb-loadArchiveBtn').addEventListener('click', async () => {
  const name = document.getElementById('nb-archiveSelect').value;
  if (!name) return;
  const data = new FormData(); data.set('archive_name', name);
  const res = await api('/nano-banana/api/archive/load', 'POST', data);
  if (res && res.values) { for (const [k,v] of Object.entries(res.values)) { const el=nbForm.elements[k]; if(el&&el.type!=='file') { if(el.type==='checkbox') el.checked=['on','true','1'].includes(v); else el.value=v; } } }
  document.getElementById('nb-presetHint').textContent = `已读取: ${name}`;
});
document.getElementById('nb-deleteArchiveBtn').addEventListener('click', async () => {
  const name = document.getElementById('nb-archiveSelect').value;
  if (!name) return;
  const data = new FormData(); data.set('archive_name', name);
  await api('/nano-banana/api/archive/delete', 'POST', data);
  loadNbArchives();
});
async function loadNbArchives() {
  const res = await api('/nano-banana/api/archives');
  const sel = document.getElementById('nb-archiveSelect'); sel.innerHTML='';
  for (const a of res?.archives||[]) { const o=document.createElement('option'); o.value=a.name; o.textContent=a.name; sel.append(o); }
}

// === Platform status bar ===
async function loadPlatformStatus() {
  const res = await api('/api/platform/status');
  if (!res || !res.ok) return;
  document.getElementById('lanInfo').textContent = `LAN: http://${res.lan_ip}:${res.portal_port}`;
  for (const app of res.apps||[]) {
    const dot = document.getElementById({seedance:'sd-dot','nano-banana':'nb-dot',dreamina:'dm-dot'}[app.name]);
    if (dot) dot.className = 'app-status-dot ' + (app.status||'unknown');
  }
}

async function loadStats() {
  const res = await api('/api/platform/stats');
  if (!res || !res.ok) return;
  document.getElementById('todayJobs').textContent = res.today_jobs || 0;
  document.getElementById('todayRequests').textContent = res.today_requests || 0;
  const byApp = document.getElementById('statsByApp');
  const entries = Object.entries(res.by_app || {});
  byApp.innerHTML = entries.map(([n,s]) => `<div class="app-stat-item"><span class="name">${n}</span><span class="count">${s.jobs||0} jobs / ${s.requests||0} req</span></div>`).join('');
  document.getElementById('barStats').textContent = `今日: ${res.today_jobs||0} jobs`;

  // IP stats table
  const byIp = res.by_ip || {};
  const tbody = document.querySelector('#ipStatsTable tbody');
  const ips = Object.keys(byIp);
  if (!ips.length) { tbody.innerHTML = '<tr><td colspan="5" style="color:#697386;text-align:center">暂无数据（任务完成后更新）</td></tr>'; return; }
  tbody.innerHTML = ips.map(ip => {
    const stats = byIp[ip];
    const sd = stats.seedance || 0;
    const nb = stats['nano-banana'] || 0;
    const dm = stats.dreamina || 0;
    const total = sd + nb + dm;
    return `<tr><td>${ip}</td><td>${sd}</td><td>${nb}</td><td>${dm}</td><td><strong>${total}</strong></td></tr>`;
  }).join('');
}

async function loadActivity() {
  const res = await api('/api/platform/activity');
  if (!res || !res.ok) return;
  const list = document.getElementById('activityList');
  const items = res.activity || [];
  if (!items.length) { list.innerHTML = '<div style="color:#697386;font-size:13px;">暂无活动</div>'; return; }
  list.innerHTML = items.slice(0,30).map(item => {
    const time = (item.created_at||item.time||'').slice(11,19)||'--:--:--';
    return `<div class="activity-item"><span class="activity-time">${time}</span><span class="activity-app">${item._app||'?'}</span><span class="activity-detail">${escHtml(item.prompt||item.task_type||item.method||'')}</span></div>`;
  }).join('');
}

// === Dreamina ===
let dmMajor = 'image';
let dmMode = 'text2image';
let dmFrameCount = 2;
let dmPollTimers = {};

// Dreamina output dir buttons
(function() {
  const dirInput = document.getElementById('dm-outputDir');
  if (!dirInput) return;
  document.getElementById('dm-chooseOutputBtn')?.addEventListener('click', async () => {
    const res = await api('/dreamina/api/choose-output-dir', 'POST');
    if (res?.path) dirInput.value = res.path;
  });
  document.getElementById('dm-appOutputBtn')?.addEventListener('click', () => {
    dirInput.value = '';
  });
  document.getElementById('dm-desktopOutputBtn')?.addEventListener('click', async () => {
    const res = await api('/dreamina/api/default-output-dir');
    if (res?.path) dirInput.value = res.path;
  });
  document.getElementById('dm-openOutputBtn')?.addEventListener('click', async () => {
    const data = new FormData(); data.set('output_dir', dirInput.value);
    await api('/dreamina/api/open-output-dir', 'POST', data);
  });
  document.getElementById('dm-cleanCacheBtn')?.addEventListener('click', async () => {
    const res = await api('/dreamina/api/cleanup-cache', 'POST');
    if (res) alert(`清理完成：素材 ${res.media_deleted||0} 个，日志 ${res.logs_deleted||0} 个`);
  });
})();

function dmBuildSlots() {
  const ir = document.getElementById('dm-imageRefs');
  const mir = document.getElementById('dm-mmImageRefs');
  const mvr = document.getElementById('dm-mmVideoRefs');
  const mar = document.getElementById('dm-mmAudioRefs');
  if (ir) for (let i=1;i<=9;i++) makeDrop(ir, `ref_image_${i}`, `参考${i}`, 'image/*', 'dm-form');
  if (mir) for (let i=1;i<=9;i++) makeDrop(mir, `mm_image_${i}`, `参考${i}`, 'image/*', 'dm-form');
  if (mvr) for (let i=1;i<=3;i++) makeDrop(mvr, `mm_video_${i}`, `视频${i}`, 'video/*', 'dm-form');
  if (mar) for (let i=1;i<=3;i++) makeDrop(mar, `mm_audio_${i}`, `音频${i}`, 'audio/*', 'dm-form');
  dmBuildFrames();
}

function dmBuildFrames() {
  const container = document.getElementById('dm-framesContainer');
  if (!container) return;
  container.innerHTML = '';
  for (let i=1; i<=dmFrameCount; i++) {
    makeDrop(container, `frame_${i}`, `帧${i}`, 'image/*', 'dm-form');
  }
}

document.getElementById('dm-addFrameBtn')?.addEventListener('click', () => {
  if (dmFrameCount >= 9) return;
  dmFrameCount++;
  document.getElementById('dm-frameCount').textContent = dmFrameCount;
  dmBuildFrames();
});
document.getElementById('dm-removeFrameBtn')?.addEventListener('click', () => {
  if (dmFrameCount <= 2) return;
  dmFrameCount--;
  document.getElementById('dm-frameCount').textContent = dmFrameCount;
  dmBuildFrames();
});

// Major tabs
document.querySelectorAll('#tab-dreamina .major-tabs .tabBtn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#tab-dreamina .major-tabs .tabBtn').forEach(b => b.classList.remove('isActive'));
    btn.classList.add('isActive');
    dmMajor = btn.dataset.major;
    if (dmMajor === 'image') dmMode = document.querySelector('#dm-imageModeSection .sub-tab.isActive')?.dataset.mode || 'text2image';
    else dmMode = document.querySelector('#dm-videoModeSection .sub-tab.isActive')?.dataset.mode || 'frames2video';
    dmUpdateVisibility();
  });
});

document.querySelectorAll('#tab-dreamina .sub-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    const section = btn.closest('#dm-imageModeSection, #dm-videoModeSection');
    section.querySelectorAll('.sub-tab').forEach(b => b.classList.remove('isActive'));
    btn.classList.add('isActive');
    dmMode = btn.dataset.mode;
    dmUpdateVisibility();
  });
});

function dmUpdateVisibility() {
  const isVideo = dmMajor === 'video';
  document.getElementById('dm-imageModeSection').classList.toggle('hidden', isVideo);
  document.getElementById('dm-videoModeSection').classList.toggle('hidden', !isVideo);
  document.getElementById('dm-videoParams').classList.toggle('hidden', !isVideo);
  document.getElementById('dm-multiRefSection').classList.toggle('hidden', dmMode !== 'image2image');
  document.getElementById('dm-framesSection').classList.toggle('hidden', dmMode !== 'frames2video');
  document.getElementById('dm-multimodalSection').classList.toggle('hidden', dmMode !== 'multimodal2video');
  document.getElementById('dm-multiframeSection').classList.toggle('hidden', dmMode !== 'multiframe2video');
}

// Env check
async function dmCheckEnv() {
  const res = await api('/dreamina/api/env/check');
  if (!res || !res.ok) { dmShowSetup('error', '无法连接后端'); return; }
  if (!res.cli_installed) { dmShowSetup('install', '即梦 CLI 未安装'); return; }
  if (!res.logged_in) { dmShowSetup('login', '需要登录即梦账号'); return; }
  dmEnterMain(res);
}

function dmShowSetup(mode, title) {
  document.getElementById('dm-setupView').classList.remove('hidden');
  document.getElementById('dm-mainView').classList.add('hidden');
  document.getElementById('dm-setupTitle').textContent = title;
  document.getElementById('dm-setupBtn').classList.toggle('hidden', mode !== 'install');
  document.getElementById('dm-loginBtn').classList.toggle('hidden', mode !== 'login');
  document.getElementById('dm-loginCancelBtn').classList.add('hidden');
  document.getElementById('dm-setupSpinner').classList.add('hidden');
  if (mode === 'install') document.getElementById('dm-setupBtn').onclick = dmInstall;
  if (mode === 'login') document.getElementById('dm-loginBtn').onclick = dmLogin;
}

async function dmInstall() {
  document.getElementById('dm-setupBtn').classList.add('hidden');
  document.getElementById('dm-setupLog').classList.remove('hidden');
  document.getElementById('dm-setupSpinner').classList.remove('hidden');
  document.getElementById('dm-setupLog').textContent = '';
  const response = await fetch('/dreamina/api/env/install-cli', { method: 'POST' });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n'); buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const data = JSON.parse(line.slice(6));
      if (data.type === 'log') { document.getElementById('dm-setupLog').textContent += data.text + '\n'; }
      else if (data.type === 'done') { document.getElementById('dm-setupSpinner').classList.add('hidden'); if (data.success) dmCheckEnv(); }
    }
  }
}

async function dmLogin() {
  document.getElementById('dm-loginBtn').classList.add('hidden');
  document.getElementById('dm-loginCancelBtn').classList.remove('hidden');
  document.getElementById('dm-setupSpinner').classList.remove('hidden');
  document.getElementById('dm-setupTitle').textContent = '等待授权...';
  const res = await api('/dreamina/api/env/login', 'POST');
  if (res && res.auth_url) document.getElementById('dm-setupDesc').innerHTML = `<a href="${res.auth_url}" target="_blank" style="color:#2673e8;">手动打开授权页</a>`;
  let elapsed = 0;
  window._dmLoginPoll = setInterval(async () => {
    elapsed += 3;
    if (elapsed > 120) { clearInterval(window._dmLoginPoll); dmShowSetup('login', '登录超时'); return; }
    const r = await api('/dreamina/api/env/login-poll');
    if (r && r.logged_in) { clearInterval(window._dmLoginPoll); dmEnterMain(r); }
  }, 3000);
  document.getElementById('dm-loginCancelBtn').onclick = async () => {
    await api('/dreamina/api/env/login-cancel', 'POST');
    clearInterval(window._dmLoginPoll);
    dmShowSetup('login', '登录已取消');
  };
}

function dmEnterMain(envData) {
  document.getElementById('dm-setupView').classList.add('hidden');
  document.getElementById('dm-mainView').classList.remove('hidden');
  const badge = document.getElementById('dm-statusText');
  if (envData?.logged_in) { badge.textContent = '已登录'; badge.className = 'status-badge ok'; }
  if (envData?.credit) document.getElementById('dm-creditText').textContent = String(envData.credit).slice(0,40);
  dmLoadJobs(); dmLoadHistory();
}

// Submit
const dmForm = document.getElementById('dm-form');
const dmSubmit = document.getElementById('dm-submitBtn');
dmForm.addEventListener('submit', async e => {
  e.preventDefault();
  dmSubmit.disabled = true; dmSubmit.textContent = '生成中';
  const data = new FormData(dmForm);
  data.set('mode', dmMode);
  const endpoint = `/dreamina/api/${dmMode}`;
  const res = await api(endpoint, 'POST', data);
  if (!res || res.error) { dmSubmit.disabled=false; dmSubmit.textContent='开始生成'; alert(res?.error||'提交失败'); return; }
  dmSubmit.disabled=false; dmSubmit.textContent='开始生成';
  dmPollJob(res.job_id || res.id);
});

async function dmPollJob(jobId) {
  if (!jobId) return;
  const el = document.getElementById('dm-jobsList');
  const card = document.createElement('div');
  card.className = 'result'; card.id = `dm-job-${jobId}`;
  card.innerHTML = `<div class="meta">Job ${jobId} - polling...</div>`;
  el.prepend(card);
  while (true) {
    const job = await api(`/dreamina/api/jobs/${jobId}`);
    if (!job) break;
    let html = `<div class="meta">Job ${jobId} - ${job.status||'unknown'}</div>`;
    if (job.result_url || job.download_url) {
      const url = job.result_url || job.download_url;
      if (dmMode.includes('video') || dmMode.includes('frame')) html += `<video controls src="${url}" style="width:100%;border-radius:5px;margin-top:6px"></video>`;
      else html += `<img src="${url}" style="width:100%;border-radius:5px;margin-top:6px">`;
      html += `<a href="${url}" download>下载</a>`;
    }
    for (const r of job.results||[]) {
      const url = r.download_url || r.url;
      if (url) html += `<img src="${url}" style="width:100%;border-radius:5px;margin-top:6px"><a href="${url}" download>下载</a>`;
    }
    card.innerHTML = html;
    if (['completed','failed'].includes(job.status)) break;
    await new Promise(r => setTimeout(r, 3000));
  }
  dmLoadHistory();
}

async function dmLoadJobs() {
  const res = await api('/dreamina/api/jobs');
  if (!res) return;
  const running = (res.jobs||[]).filter(j => !['completed','failed'].includes(j.status));
  document.getElementById('dm-runningCount').textContent = running.length ? `${running.length} 进行中` : '';
  for (const j of running) dmPollJob(j.id || j.job_id);
}

async function dmLoadHistory() {
  const res = await api('/dreamina/api/history');
  const list = document.getElementById('dm-historyList');
  if (!res || !res.items?.length) { list.innerHTML = '<div style="color:#697386;font-size:12px">暂无历史</div>'; return; }
  list.innerHTML = (res.items||[]).slice(0,20).map(item => {
    const url = item.cover_url || item.result_url || '';
    return `<div class="result"><div class="meta">${escHtml(item.prompt||item.mode||'')} · ${item.status||''}</div>${url?`<img src="${url}" style="width:100%;border-radius:5px;margin-top:4px">`:''}</div>`;
  }).join('');
}

// Dreamina archive
document.getElementById('dm-archiveSaveBtn')?.addEventListener('click', async () => {
  const name = document.getElementById('dm-archiveName').value.trim();
  if (!name) return;
  const data = new FormData(dmForm); data.set('archive_name', name);
  await api('/dreamina/api/preset', 'POST', data);
  dmLoadArchives();
});
document.getElementById('dm-archiveLoadBtn')?.addEventListener('click', async () => {
  const name = document.getElementById('dm-archiveSelect').value;
  if (!name) return;
  const data = new FormData(); data.set('archive_name', name);
  const res = await api('/dreamina/api/archive/load', 'POST', data);
  if (res && res.values) { for (const [k,v] of Object.entries(res.values)) { const el=dmForm.elements[k]; if(el&&el.type!=='file') el.value=v; } }
});
document.getElementById('dm-archiveDeleteBtn')?.addEventListener('click', async () => {
  const name = document.getElementById('dm-archiveSelect').value;
  if (!name) return;
  const data = new FormData(); data.set('archive_name', name);
  await api('/dreamina/api/archive/delete', 'POST', data);
  dmLoadArchives();
});
async function dmLoadArchives() {
  const res = await api('/dreamina/api/archives');
  const sel = document.getElementById('dm-archiveSelect'); sel.innerHTML='<option value="">选择存档...</option>';
  for (const a of res?.archives||[]) { const o=document.createElement('option'); o.value=a.name; o.textContent=a.name; sel.append(o); }
}

// Topbar actions
document.getElementById('dm-switchAccountBtn')?.addEventListener('click', async () => {
  await api('/dreamina/api/env/logout', 'POST');
  dmShowSetup('login', '请重新登录');
});
document.getElementById('dm-updateCliBtn')?.addEventListener('click', async () => {
  document.getElementById('dm-setupView').classList.remove('hidden');
  document.getElementById('dm-mainView').classList.add('hidden');
  document.getElementById('dm-setupTitle').textContent = '更新中...';
  document.getElementById('dm-setupSpinner').classList.remove('hidden');
  const response = await fetch('/dreamina/api/env/update-cli', { method: 'POST' });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  document.getElementById('dm-setupLog').classList.remove('hidden');
  document.getElementById('dm-setupLog').textContent = '';
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n'); buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const d = JSON.parse(line.slice(6));
      if (d.type === 'log') document.getElementById('dm-setupLog').textContent += d.text + '\n';
      else if (d.type === 'done') { document.getElementById('dm-setupSpinner').classList.add('hidden'); if (d.success) dmCheckEnv(); }
    }
  }
});

// History filter
document.querySelectorAll('#tab-dreamina .filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#tab-dreamina .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    dmLoadHistory();
  });
});

// === Init ===
loadPlatformStatus(); loadStats(); loadActivity();
bindProviderSwitch('sd', '/seedance', sdForm); loadSdArchives();
bindProviderSwitch('nb', '/nano-banana', nbForm); loadNbArchives();
dmBuildSlots(); dmCheckEnv(); dmLoadArchives();
setInterval(loadPlatformStatus, 10000);
setInterval(loadStats, 30000);

})();
