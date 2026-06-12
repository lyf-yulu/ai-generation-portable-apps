(function() {
'use strict';

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

// ---- Workspace identity ----
function workspaceId() {
  const qs = new URLSearchParams(window.location.search);
  if (qs.get("ws")) return qs.get("ws");
  let id = localStorage.getItem("workspace_id");
  if (!id) { id = crypto.randomUUID(); localStorage.setItem("workspace_id", id); }
  return id;
}
function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  opts.headers["X-Workspace-Id"] = workspaceId();
  return fetch(url, opts);
}

let currentMajor = 'image';
let currentMode = 'text2image';
let frameCount = 2;
let pollTimers = {};

// === Init ===
document.addEventListener('DOMContentLoaded', () => {
  checkEnv();
  bindMajorTabs();
  bindSubTabs();
  bindForm();
  bindTopbar();
  bindFilter();
  bindPreview();
  bindArchive();
  bindOutputDir();
  buildUploadSlots();
  buildMultiframeUI();
  bindMultiframeControls();
});

// === Environment Check ===
async function checkEnv() {
  const res = await api('/api/env/check');
  if (!res.ok) { showSetup('error', '无法连接后端'); return; }
  if (!res.cli_installed) { showSetup('install', '即梦 CLI 未安装', '点击下方按钮一键安装即梦 CLI'); return; }
  if (!res.logged_in) { showSetup('login', '需要登录', '点击下方按钮登录你的即梦账号，浏览器将自动打开授权页面'); return; }
  enterMain(res);
}

function showSetup(mode, title, desc) {
  $('#setupView').classList.remove('hidden');
  $('#mainView').classList.add('hidden');
  $('#setupTitle').textContent = title;
  $('#setupDesc').textContent = desc || '';
  $('#setupBtn').classList.toggle('hidden', mode !== 'install');
  $('#loginBtn').classList.toggle('hidden', mode !== 'login');
  $('#loginCancelBtn').classList.add('hidden');
  $('#setupLog').classList.add('hidden');
  $('#setupSpinner').classList.add('hidden');
  if (mode === 'install') $('#setupBtn').onclick = installCli;
  if (mode === 'login') $('#loginBtn').onclick = startLogin;
}

async function installCli() {
  $('#setupBtn').classList.add('hidden');
  $('#setupLog').classList.remove('hidden');
  $('#setupSpinner').classList.remove('hidden');
  $('#setupLog').textContent = '';
  const response = await fetch('/api/env/install-cli', { method: 'POST' });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const data = JSON.parse(line.slice(6));
      if (data.type === 'log') {
        $('#setupLog').textContent += data.text + '\n';
        $('#setupLog').scrollTop = $('#setupLog').scrollHeight;
      } else if (data.type === 'done') {
        $('#setupSpinner').classList.add('hidden');
        if (data.success) checkEnv();
        else {
          $('#setupTitle').textContent = '安装失败';
          $('#setupDesc').textContent = data.error || '请重试';
          $('#setupBtn').classList.remove('hidden');
          $('#setupBtn').textContent = '重试安装';
        }
      }
    }
  }
}

async function startLogin() {
  $('#loginBtn').classList.add('hidden');
  $('#loginCancelBtn').classList.remove('hidden');
  $('#setupSpinner').classList.remove('hidden');
  $('#setupTitle').textContent = '等待授权...';
  $('#setupDesc').textContent = '浏览器即将打开，请在浏览器中完成登录授权';
  const res = await api('/api/env/login', 'POST');
  if (res && res.auth_url) {
    $('#setupDesc').innerHTML = '浏览器已打开授权页面。如果没有弹出，请<a href="' + res.auth_url + '" target="_blank" style="color:#2673e8;">点击此处手动打开</a>';
  }
  pollLogin();
  $('#loginCancelBtn').onclick = async () => {
    await api('/api/env/login-cancel', 'POST');
    showSetup('login', '登录已取消', '点击重新登录');
    clearInterval(window._loginPoll);
  };
}

function pollLogin() {
  let elapsed = 0;
  window._loginPoll = setInterval(async () => {
    elapsed += 3;
    if (elapsed > 120) { clearInterval(window._loginPoll); showSetup('login', '登录超时', '请重试'); return; }
    const res = await api('/api/env/login-poll');
    if (res && res.logged_in) { clearInterval(window._loginPoll); enterMain(res); }
  }, 3000);
}

function enterMain(envData) {
  $('#setupView').classList.add('hidden');
  $('#mainView').classList.remove('hidden');
  updateStatus(envData);
  loadJobs();
  loadHistory();
}

function updateStatus(data) {
  const badge = $('#statusText');
  if (data && data.logged_in) { badge.textContent = '已登录'; badge.className = 'status-badge ok'; }
  else { badge.textContent = '未登录'; badge.className = 'status-badge error'; }
  if (data && data.credit) {
    const c = typeof data.credit === 'string' ? data.credit : JSON.stringify(data.credit);
    $('#creditText').textContent = c.length > 60 ? c.slice(0, 60) + '...' : c;
  }
}

// === Major Tabs ===
function bindMajorTabs() {
  $$('.major-tabs .tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.major-tabs .tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentMajor = btn.dataset.major;
      if (currentMajor === 'image') {
        currentMode = $('#imageModeSection .sub-tab.active').dataset.mode;
      } else {
        currentMode = $('#videoModeSection .sub-tab.active').dataset.mode;
      }
      updateFormVisibility();
    });
  });
}

function bindSubTabs() {
  $$('.sub-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      const section = btn.closest('#imageModeSection, #videoModeSection');
      section.querySelectorAll('.sub-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentMode = btn.dataset.mode;
      updateFormVisibility();
    });
  });
}

function updateFormVisibility() {
  const isVideo = currentMajor === 'video';
  $('#imageModeSection').classList.toggle('hidden', isVideo);
  $('#videoModeSection').classList.toggle('hidden', !isVideo);
  $('#videoParams').classList.toggle('hidden', !isVideo);
  $('#multiRefSection').classList.toggle('hidden', currentMode !== 'image2image');
  $('#framesSection').classList.toggle('hidden', currentMode !== 'frames2video');
  $('#multimodalSection').classList.toggle('hidden', currentMode !== 'multimodal2video');
  $('#multiframeSection').classList.toggle('hidden', currentMode !== 'multiframe2video');
  $('#modelVersionGroup')?.classList.toggle('hidden', currentMode === 'multiframe2video');
}

// === Upload Slots ===
function buildUploadSlots() {
  const imageRefs = $('#imageRefs');
  const mmImageRefs = $('#mmImageRefs');
  const mmVideoRefs = $('#mmVideoRefs');
  const mmAudioRefs = $('#mmAudioRefs');

  for (let i = 1; i <= 9; i++) {
    imageRefs.appendChild(makeDrop(`ref_image_${i}`, `参考${i}`, 'image/*'));
    mmImageRefs.appendChild(makeDrop(`ref_image_${i}`, `参考${i}`, 'image/*'));
  }
  for (let i = 1; i <= 3; i++) {
    mmVideoRefs.appendChild(makeDrop(`ref_video_${i}`, `视频${i}`, 'video/*'));
    mmAudioRefs.appendChild(makeDrop(`ref_audio_${i}`, `音频${i}`, 'audio/*'));
  }

  wireAllDrops();
}

function makeDrop(name, label, accept) {
  const el = document.createElement('label');
  el.className = 'drop';
  el.textContent = label;
  const input = document.createElement('input');
  input.type = 'file';
  input.name = name;
  input.accept = accept;
  input.hidden = true;
  const span = document.createElement('span');
  span.textContent = '未上传';
  el.appendChild(input);
  el.appendChild(span);
  return el;
}

function wireAllDrops() {
  $$('.drop input[type="file"]').forEach(input => {
    input.addEventListener('change', () => {
      const drop = input.closest('.drop');
      const file = input.files?.[0];
      if (!file) { clearDropPreview(drop); return; }
      renderDropPreview(drop, input.accept, file);
    });
  });
}

function renderDropPreview(drop, accept, file) {
  drop.classList.add('has-preview');
  drop.querySelector('.preview')?.remove();
  const kind = accept.includes('video') ? 'video' : accept.includes('audio') ? 'audio' : 'image';
  const media = document.createElement(kind === 'image' ? 'img' : kind);
  media.className = 'preview';
  media.src = URL.createObjectURL(file);
  if (kind !== 'image') media.controls = true;
  if (kind === 'image') {
    media.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); openPreview(media.src); });
  }
  drop.appendChild(media);
  drop.querySelector('span').textContent = file.name;
}

function clearDropPreview(drop) {
  drop.classList.remove('has-preview');
  drop.querySelector('.preview')?.remove();
  drop.querySelector('span').textContent = '未上传';
}

// === Multiframe UI ===
function buildMultiframeUI() {
  renderFrames();
}

function bindMultiframeControls() {
  $('#addFrameBtn').addEventListener('click', () => {
    if (frameCount >= 9) return;
    frameCount++;
    $('#frameCount').textContent = frameCount;
    renderFrames();
  });
  $('#removeFrameBtn').addEventListener('click', () => {
    if (frameCount <= 2) return;
    frameCount--;
    $('#frameCount').textContent = frameCount;
    renderFrames();
  });
}

function renderFrames() {
  const container = $('#framesContainer');
  container.innerHTML = '';
  for (let i = 1; i <= frameCount; i++) {
    const item = document.createElement('div');
    item.className = 'frame-item';
    item.innerHTML = `<div class="frame-item-header">帧 ${i}</div>`;
    const drop = makeDrop(`frame_${i}`, `上传图片`, 'image/*');
    item.appendChild(drop);
    if (i < frameCount) {
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'transition-input';
      input.name = `transition_prompt_${i}`;
      input.placeholder = `帧${i} → 帧${i+1} 过渡描述`;
      item.appendChild(input);
    }
    container.appendChild(item);
  }
  wireAllDrops();
}

// === Form Submit ===
function bindForm() {
  $('#genForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    await submitJob();
  });
}

async function submitJob() {
  const prompt = $('#prompt').value.trim();
  if (!prompt && currentMode !== 'multiframe2video') { alert('请输入 Prompt'); return; }

  const btn = $('#submitBtn');
  btn.disabled = true;
  btn.textContent = '提交中...';

  try {
    const formData = new FormData();
    formData.append('prompt', prompt);
    formData.append('ratio', $('#ratio').value);
    formData.append('resolution_type', $('#resolution_type').value);
    formData.append('duration', $('#duration').value);
    formData.append('video_resolution', $('#video_resolution').value);
    formData.append('model_version', $('#model_version').value);
    formData.append('repeat_count', $('#repeat_count').value);
    formData.append('concurrency', $('#concurrency').value);
    formData.append('output_name', $('#outputName').value);
    formData.append('output_dir', $('#outputDir').value);

    if (currentMode === 'image2image') {
      collectFiles(formData, '#imageRefs');
    } else if (currentMode === 'frames2video') {
      collectFrameFiles(formData);
    } else if (currentMode === 'multimodal2video') {
      collectFiles(formData, '#mmImageRefs');
      collectFiles(formData, '#mmVideoRefs');
      collectFiles(formData, '#mmAudioRefs');
    } else if (currentMode === 'multiframe2video') {
      collectFiles(formData, '#framesContainer');
      collectTransitionPrompts(formData);
    }

    const response = await apiFetch(`/api/${currentMode}`, { method: 'POST', body: formData });
    const res = await response.json();

    if (res.ok) {
      startPollingJob(res.job_id);
      loadJobs();
    } else {
      alert(res.error || '提交失败');
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '开始生成';
  }
}

function collectFiles(formData, containerSelector) {
  const container = $(containerSelector);
  if (!container) return;
  container.querySelectorAll('input[type="file"]').forEach(input => {
    if (input.files && input.files[0]) {
      formData.append(input.name, input.files[0]);
    }
  });
}

function collectFrameFiles(formData) {
  const first = $('#firstFrameDrop input[type="file"]');
  const last = $('#lastFrameDrop input[type="file"]');
  if (first?.files?.[0]) formData.append('first_frame', first.files[0]);
  if (last?.files?.[0]) formData.append('last_frame', last.files[0]);
}

function collectTransitionPrompts(formData) {
  $$('.transition-input').forEach(input => {
    if (input.value.trim()) {
      formData.append(input.name, input.value.trim());
    }
  });
}

// === Jobs Polling ===
function startPollingJob(jobId) {
  if (pollTimers[jobId]) return;
  pollTimers[jobId] = setInterval(async () => {
    const res = await api(`/api/jobs/${jobId}`);
    if (!res || !res.ok) return;
    const job = res.job;
    if (job.status === 'completed' || job.status === 'failed') {
      clearInterval(pollTimers[jobId]);
      delete pollTimers[jobId];
      loadHistory();
    }
    renderJobs();
  }, 3000);
}

async function loadJobs() {
  const res = await api('/api/jobs');
  if (res && res.ok) renderJobsList(res.jobs);
}

function renderJobsList(jobs) {
  const list = $('#jobsList');
  const active = jobs.filter(j => j.status === 'pending' || j.status === 'running' || j.status === 'querying');
  const recent = jobs.filter(j => j.status === 'completed' || j.status === 'failed').slice(-10).reverse();
  const all = [...active, ...recent];
  $('#runningCount').textContent = active.length ? `${active.length} 进行中` : '';
  if (!all.length) { list.innerHTML = '<p style="color:#697386;font-size:13px;">暂无任务</p>'; return; }
  list.innerHTML = all.map(renderJobCard).join('');
  bindRetryButtons();
  bindThumbClicks();
}

async function renderJobs() { await loadJobs(); }

function renderJobCard(job) {
  let resultHtml = '';
  if (job.status === 'completed' && job.result) {
    const files = job.result.files || [];
    resultHtml = '<div class="job-result">' + files.map(f => {
      const ext = f.split('.').pop().toLowerCase();
      if (['mp4','webm','mov'].includes(ext)) {
        return `<video class="result-thumb" src="/${f}" data-src="/${f}" muted></video>`;
      }
      return `<img class="result-thumb" src="/${f}" data-src="/${f}" alt="result">`;
    }).join('') + '</div>';
  }
  let progressHtml = '';
  if (job.total > 1 && (job.status === 'running' || job.status === 'pending')) {
    const pct = job.total ? Math.round((job.done || 0) / job.total * 100) : 0;
    progressHtml = `<div class="job-progress">${job.done || 0}/${job.total} 完成<div class="job-progress-bar"><div class="job-progress-bar-fill" style="width:${pct}%"></div></div></div>`;
  }
  let eventsHtml = '';
  if (job.events && job.events.length > 0) {
    const recent = job.events.slice(-3);
    eventsHtml = '<div class="job-events">' + recent.map(e => `<div>${e.time} ${escHtml(e.message)}</div>`).join('') + '</div>';
  }
  let errorHtml = '';
  if (job.status === 'failed' && job.error) {
    errorHtml = `<div class="job-error">${escHtml(job.error.slice(0, 200))}</div>`;
  }
  let actionsHtml = '';
  if (job.status === 'failed' && job.retryable) {
    actionsHtml = `<div class="job-actions"><button class="btn-retry" data-job="${job.job_id}">重试</button></div>`;
  }
  let templateBtn = '';
  if (job.status === 'completed') {
    templateBtn = `<div class="job-actions"><button class="btn-template" data-job="${job.job_id}">存为模板</button></div>`;
  }
  let cliLogHtml = '';
  const logs = job.cli_logs || [];
  if (logs.length) {
    const logId = 'cli-log-' + job.job_id;
    const logBody = logs.map(l => {
      const cmdDisp = escHtml(l.command || '');
      const outDisp = escHtml((l.stdout || '').slice(0, 800));
      const errDisp = l.stderr ? escHtml(l.stderr.slice(0, 300)) : '';
      return `<div style="margin-bottom:6px"><div style="color:#a78bfa">$ ${cmdDisp}</div><div style="color:#6ee7b7">exitcode: ${l.returncode}</div>${outDisp ? `<div style="color:#e2e8f0;white-space:pre-wrap;word-break:break-all">${outDisp}</div>` : ''}${errDisp ? `<div style="color:#fca5a5">${errDisp}</div>` : ''}</div>`;
    }).join('');
    cliLogHtml = `<div style="margin-top:6px"><span style="cursor:pointer;color:#818cf8;user-select:none;font-size:12px" onclick="var el=document.getElementById('${logId}');el.style.display=el.style.display==='none'?'block':'none'">CLI 详情 ▾</span><div id="${logId}" style="display:none;margin-top:4px;background:#1e1b2e;color:#e2e8f0;font-family:monospace;font-size:11px;padding:8px;border-radius:6px;max-height:240px;overflow:auto">${logBody}</div></div>`;
  }
  return `<div class="job-card">
    <div class="job-card-header">
      <span class="job-type">${typeLabel(job.task_type)}</span>
      <span class="job-status ${job.status}">${statusLabel(job.status)}</span>
    </div>
    <div class="job-prompt">${escHtml(job.params?.prompt || '')}</div>
    <div class="job-time">${job.created_at || ''}</div>
    ${progressHtml}${eventsHtml}${resultHtml}${errorHtml}${actionsHtml}${templateBtn}${cliLogHtml}
  </div>`;
}

function bindRetryButtons() {
  $$('.btn-retry').forEach(btn => {
    btn.addEventListener('click', async () => {
      const res = await api(`/api/jobs/${btn.dataset.job}/retry`, 'POST');
      if (res && res.ok) { startPollingJob(res.job_id); loadJobs(); }
    });
  });
  $$('.btn-template').forEach(btn => {
    btn.addEventListener('click', async () => {
      const name = prompt('输入存档名称:');
      if (!name) return;
      const res = await api('/api/archive/from-history', 'POST', { job_id: btn.dataset.job, archive_name: name });
      if (res && res.ok) { refreshArchiveList(); alert('已保存为模板'); }
      else alert(res?.error || '保存失败');
    });
  });
}

function bindThumbClicks() {
  $$('.result-thumb').forEach(el => {
    el.addEventListener('click', () => openPreview(el.dataset.src));
  });
}

// === History ===
async function loadHistory() {
  const res = await api('/api/history');
  if (res && res.ok) renderHistory(res.history);
}

function renderHistory(items) {
  const list = $('#historyList');
  const filter = $('.filter-btn.active')?.dataset.filter || 'all';
  let filtered = items.slice().reverse();
  if (filter === 'image') filtered = filtered.filter(i => i.task_type?.includes('image'));
  if (filter === 'video') filtered = filtered.filter(i => i.task_type?.includes('video'));
  filtered = filtered.slice(0, 50);
  if (!filtered.length) { list.innerHTML = '<p style="color:#697386;font-size:13px;">暂无历史记录</p>'; return; }
  list.innerHTML = filtered.map(renderJobCard).join('');
  bindThumbClicks();
  bindRetryButtons();
}

function bindFilter() {
  $$('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadHistory();
    });
  });
}

// === Output Dir ===
function bindOutputDir() {
  $('#chooseOutputBtn').addEventListener('click', async () => {
    const res = await api('/api/choose-output-dir', 'POST');
    if (res?.path) $('#outputDir').value = res.path;
  });
  $('#desktopOutputBtn').addEventListener('click', async () => {
    const res = await api('/api/default-output-dir');
    if (res?.path) $('#outputDir').value = res.path;
  });
  $('#openOutputBtn').addEventListener('click', async () => {
    const dir = $('#outputDir').value.trim() || 'outputs';
    await api('/api/open-output-dir', 'POST',
      new URLSearchParams({ output_dir: dir }).toString(),
      { 'Content-Type': 'application/x-www-form-urlencoded' });
  });
}

// === Topbar ===
function bindTopbar() {
  $('#switchAccountBtn').addEventListener('click', async () => {
    if (!confirm('确定切换账号？将登出当前账号并重新授权。')) return;
    $('#setupView').classList.remove('hidden');
    $('#mainView').classList.add('hidden');
    $('#setupTitle').textContent = '切换账号中...';
    $('#setupDesc').textContent = '正在登出当前账号并发起新的登录...';
    $('#setupBtn').classList.add('hidden');
    $('#loginBtn').classList.add('hidden');
    $('#loginCancelBtn').classList.remove('hidden');
    $('#setupSpinner').classList.remove('hidden');
    $('#setupLog').classList.add('hidden');
    const res = await api('/api/env/switch-account', 'POST');
    if (res && res.auth_url) {
      $('#setupTitle').textContent = '等待新账号授权...';
      $('#setupDesc').innerHTML = '浏览器已打开授权页面。如果没有弹出，请<a href="' + res.auth_url + '" target="_blank" style="color:#2673e8;">点击此处手动打开</a>';
    } else {
      $('#setupTitle').textContent = '等待授权...';
      $('#setupDesc').textContent = '请在浏览器中完成登录';
    }
    pollLogin();
    $('#loginCancelBtn').onclick = async () => { await api('/api/env/login-cancel', 'POST'); checkEnv(); };
  });

  $('#cleanCacheBtn').addEventListener('click', async () => {
    if (!confirm('确定清理上传缓存？')) return;
    const res = await api('/api/cache/clean', 'POST');
    if (res && res.ok) alert(`已清理 ${res.removed_uploads} 个缓存文件`);
  });

  $('#updateCliBtn').addEventListener('click', async () => {
    if (!confirm('确定更新即梦 CLI？')) return;
    $('#setupView').classList.remove('hidden');
    $('#mainView').classList.add('hidden');
    $('#setupTitle').textContent = '正在更新 CLI...';
    $('#setupDesc').textContent = '';
    $('#setupBtn').classList.add('hidden');
    $('#loginBtn').classList.add('hidden');
    $('#setupLog').classList.remove('hidden');
    $('#setupSpinner').classList.remove('hidden');
    $('#setupLog').textContent = '';
    const response = await fetch('/api/env/update-cli', { method: 'POST' });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = JSON.parse(line.slice(6));
        if (data.type === 'log') {
          $('#setupLog').textContent += data.text + '\n';
          $('#setupLog').scrollTop = $('#setupLog').scrollHeight;
        } else if (data.type === 'done') {
          $('#setupSpinner').classList.add('hidden');
          setTimeout(() => checkEnv(), 1000);
        }
      }
    }
  });
}

// === Preview Dialog ===
function bindPreview() {
  $('#closePreview').addEventListener('click', () => $('#previewDialog').close());
  $('#previewDialog').addEventListener('click', e => {
    if (e.target === $('#previewDialog')) $('#previewDialog').close();
  });
}

function openPreview(src) {
  const ext = src.split('.').pop().toLowerCase();
  const content = $('#previewContent');
  if (['mp4','webm','mov'].includes(ext)) {
    content.innerHTML = `<video src="${src}" controls autoplay style="max-width:85vw;max-height:85vh;"></video>`;
  } else {
    content.innerHTML = `<img src="${src}" alt="preview" style="max-width:85vw;max-height:85vh;">`;
  }
  $('#previewDialog').showModal();
}

// === Archive ===
function bindArchive() {
  $('#archiveSaveBtn').addEventListener('click', async () => {
    const name = $('#archiveName').value.trim();
    if (!name) { alert('请输入存档名称'); return; }
    const formData = new FormData($('#genForm'));
    formData.append('archive_name', name);
    const response = await fetch('/api/preset', { method: 'POST', body: formData });
    const res = await response.json();
    if (res.ok) { refreshArchiveList(); $('#archiveName').value = ''; }
    else alert(res.error || '保存失败');
  });

  $('#archiveLoadBtn').addEventListener('click', async () => {
    const name = $('#archiveSelect').value;
    if (!name) { alert('请选择存档'); return; }
    const res = await api('/api/archive/load', 'POST', { name });
    if (res && res.ok) { applyPreset(res); alert('已加载存档'); }
    else alert(res?.error || '加载失败');
  });

  $('#archiveDeleteBtn').addEventListener('click', async () => {
    const name = $('#archiveSelect').value;
    if (!name) { alert('请选择存档'); return; }
    if (!confirm(`确定删除存档「${name}」？`)) return;
    const res = await api('/api/archive/delete', 'POST', { name });
    if (res && res.ok) refreshArchiveList();
    else alert(res?.error || '删除失败');
  });

  refreshArchiveList();
}

async function refreshArchiveList() {
  const res = await api('/api/archives');
  if (!res || !res.ok) return;
  const select = $('#archiveSelect');
  select.innerHTML = '<option value="">选择存档...</option>';
  for (const a of res.archives) {
    const opt = document.createElement('option');
    opt.value = a.name;
    opt.textContent = a.name;
    select.appendChild(opt);
  }
}

function applyPreset(data) {
  const values = data.values || {};
  if (values.prompt) $('#prompt').value = values.prompt;
  if (values.ratio) $('#ratio').value = values.ratio;
  if (values.resolution_type) $('#resolution_type').value = values.resolution_type;
  if (values.duration) $('#duration').value = values.duration;
  if (values.video_resolution) $('#video_resolution').value = values.video_resolution;
  if (values.model_version) $('#model_version').value = values.model_version;
  if (values.repeat_count) $('#repeat_count').value = values.repeat_count;
  if (values.concurrency) $('#concurrency').value = values.concurrency;
  if (values.output_name) $('#outputName').value = values.output_name;
  if (values.output_dir) $('#outputDir').value = values.output_dir;
}

// === Helpers ===
async function api(url, method, body, headers) {
  try {
    const opts = { method: method || 'GET' };
    if (body) {
      if (typeof body === 'string') { opts.body = body; }
      else { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(body); }
    }
    if (headers) Object.assign(opts.headers || (opts.headers = {}), headers);
    const res = await fetch(url, opts);
    return await res.json();
  } catch (e) { return { ok: false, error: e.message }; }
}

function typeLabel(t) {
  const map = { text2image: '文生图', image2image: '图生图', text2video: '文生视频', image2video: '图生视频', frames2video: '首尾帧', multimodal2video: '全能参考', multiframe2video: '智能多帧' };
  return map[t] || t;
}

function statusLabel(s) {
  const map = { pending: '等待中', running: '生成中', completed: '已完成', failed: '失败', querying: '查询中' };
  return map[s] || s;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

})();
