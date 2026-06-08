(function() {
'use strict';

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

let currentTab = 'text2image';
let uploadedFile = null;
let pollTimers = {};

// === Init ===
document.addEventListener('DOMContentLoaded', () => {
  checkEnv();
  bindTabs();
  bindForm();
  bindUpload();
  bindTopbar();
  bindFilter();
  bindPreview();
});

// === Environment Check ===
async function checkEnv() {
  const res = await api('/api/env/check');
  if (!res.ok) {
    showSetup('error', '无法连接后端');
    return;
  }
  if (!res.cli_installed) {
    showSetup('install', '即梦 CLI 未安装', '点击下方按钮一键安装即梦 CLI');
    return;
  }
  if (!res.logged_in) {
    showSetup('login', '需要登录', '点击下方按钮登录你的即梦账号，浏览器将自动打开授权页面');
    return;
  }
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

  if (mode === 'install') {
    $('#setupBtn').onclick = installCli;
  }
  if (mode === 'login') {
    $('#loginBtn').onclick = startLogin;
  }
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
        if (data.success) {
          checkEnv();
        } else {
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
    $('#setupDesc').innerHTML = '浏览器已打开授权页面。如果没有弹出，请<a href="' + res.auth_url + '" target="_blank" style="color:var(--accent);">点击此处手动打开</a>';
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
    if (elapsed > 120) {
      clearInterval(window._loginPoll);
      showSetup('login', '登录超时', '请重试');
      return;
    }
    const res = await api('/api/env/login-poll');
    if (res && res.logged_in) {
      clearInterval(window._loginPoll);
      enterMain(res);
    }
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
  if (data && data.logged_in) {
    badge.textContent = '已登录';
    badge.className = 'status-badge ok';
  } else {
    badge.textContent = '未登录';
    badge.className = 'status-badge error';
  }
  if (data && data.credit) {
    const c = typeof data.credit === 'string' ? data.credit : JSON.stringify(data.credit);
    $('#creditText').textContent = c.length > 60 ? c.slice(0, 60) + '...' : c;
  }
}

// === Tabs ===
function bindTabs() {
  $$('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentTab = btn.dataset.tab;
      updateFormVisibility();
    });
  });
}

function updateFormVisibility() {
  const isImg2img = currentTab === 'image2image';
  const isImg2video = currentTab === 'image2video';
  const isVideo = currentTab === 'text2video' || currentTab === 'image2video';

  $('#imageUploadGroup').classList.toggle('hidden', !isImg2img && !isImg2video);
  $('#videoParams').classList.toggle('hidden', !isVideo);
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
  if (!prompt) { alert('请输入 Prompt'); return; }

  const btn = $('#submitBtn');
  btn.disabled = true;
  btn.textContent = '提交中...';

  try {
    let res;
    const needsFile = currentTab === 'image2image' || currentTab === 'image2video';

    if (needsFile && uploadedFile) {
      const formData = new FormData();
      formData.append('prompt', prompt);
      formData.append('ratio', $('#ratio').value);
      formData.append('resolution_type', $('#resolution_type').value);
      formData.append('duration', $('#duration').value);
      formData.append('video_resolution', $('#video_resolution').value);
      formData.append('image', uploadedFile);
      res = await fetch(`/api/${currentTab}`, { method: 'POST', body: formData });
      res = await res.json();
    } else if (needsFile && !uploadedFile) {
      alert('请上传参考图');
      return;
    } else {
      res = await api(`/api/${currentTab}`, 'POST', {
        prompt,
        ratio: $('#ratio').value,
        resolution_type: $('#resolution_type').value,
        duration: $('#duration').value,
        video_resolution: $('#video_resolution').value,
      });
    }

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

// === Upload ===
function bindUpload() {
  const area = $('#uploadArea');
  const input = $('#fileInput');

  area.addEventListener('click', () => input.click());
  area.addEventListener('dragover', e => { e.preventDefault(); area.classList.add('dragover'); });
  area.addEventListener('dragleave', () => area.classList.remove('dragover'));
  area.addEventListener('drop', e => {
    e.preventDefault();
    area.classList.remove('dragover');
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', () => { if (input.files.length) handleFile(input.files[0]); });
}

function handleFile(file) {
  uploadedFile = file;
  const preview = $('#uploadPreview');
  preview.src = URL.createObjectURL(file);
  preview.classList.remove('hidden');
  $('.upload-hint').textContent = file.name;
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

  if (!all.length) {
    list.innerHTML = '<p style="color:var(--text-dim);font-size:13px;">暂无任务</p>';
    return;
  }
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

  let errorHtml = '';
  if (job.status === 'failed' && job.error) {
    errorHtml = `<div class="job-error">${escHtml(job.error.slice(0, 200))}</div>`;
  }

  let actionsHtml = '';
  if (job.status === 'failed' && job.retryable) {
    actionsHtml = `<div class="job-actions"><button class="btn-retry" data-job="${job.job_id}">重试</button></div>`;
  }

  return `<div class="job-card">
    <div class="job-card-header">
      <span class="job-type">${typeLabel(job.task_type)}</span>
      <span class="job-status ${job.status}">${statusLabel(job.status)}</span>
    </div>
    <div class="job-prompt">${escHtml(job.params?.prompt || '')}</div>
    <div class="job-time">${job.created_at || ''}</div>
    ${resultHtml}${errorHtml}${actionsHtml}
  </div>`;
}

function bindRetryButtons() {
  $$('.btn-retry').forEach(btn => {
    btn.addEventListener('click', async () => {
      const jobId = btn.dataset.job;
      const res = await api(`/api/jobs/${jobId}/retry`, 'POST');
      if (res && res.ok) {
        startPollingJob(res.job_id);
        loadJobs();
      }
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

  if (!filtered.length) {
    list.innerHTML = '<p style="color:var(--text-dim);font-size:13px;">暂无历史记录</p>';
    return;
  }
  list.innerHTML = filtered.map(renderJobCard).join('');
  bindThumbClicks();
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
      $('#setupDesc').innerHTML = '浏览器已打开授权页面。如果没有弹出，请<a href="' + res.auth_url + '" target="_blank" style="color:var(--accent);">点击此处手动打开</a>';
    } else {
      $('#setupTitle').textContent = '等待授权...';
      $('#setupDesc').textContent = '请在浏览器中完成登录';
    }
    pollLogin();

    $('#loginCancelBtn').onclick = async () => {
      await api('/api/env/login-cancel', 'POST');
      checkEnv();
    };
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
    content.innerHTML = `<img src="${src}" alt="preview">`;
  }
  $('#previewDialog').showModal();
}

// === Helpers ===
async function api(url, method, body) {
  try {
    const opts = { method: method || 'GET' };
    if (body) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    return await res.json();
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function typeLabel(t) {
  const map = { text2image: '文生图', image2image: '图生图', text2video: '文生视频', image2video: '图生视频' };
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
