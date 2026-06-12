'use strict';

// === Utilities ===
async function api(url, method, body) {
  try {
    const opts = { method: method || 'GET' };
    if (body) opts.body = body;
    const res = await fetch(url, opts);
    return await res.json();
  } catch (e) { return null; }
}

function escHtml(s) { return s ? s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : ''; }

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
  rmBtn.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); input.value = ''; el.classList.remove('hasPreview'); el.querySelector('.preview')?.remove(); span.textContent = '未上传'; });
  el.append(input, span, rmBtn);
  wireFileDrop(el, input, name);
  container.appendChild(el);
}

function wireFileDrop(drop, input, name) {
  input.addEventListener('change', () => {
    const f = input.files?.[0];
    if (!f) { drop.classList.remove('hasPreview'); drop.querySelector('.preview')?.remove(); drop.querySelector('span').textContent = '未上传'; return; }
    showPreview(drop, name, URL.createObjectURL(f), f.name);
  });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('isDragging'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('isDragging'));
  drop.addEventListener('drop', e => {
    e.preventDefault(); drop.classList.remove('isDragging');
    const f = e.dataTransfer?.files?.[0]; if (!f) return;
    const dt = new DataTransfer(); dt.items.add(f); input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
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

// === Tab Switching (vanilla) ===
document.querySelectorAll('.app-tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.app-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
}));

document.getElementById('closePreviewBtn').addEventListener('click', () => document.getElementById('previewDialog').close());
document.getElementById('previewDialog').addEventListener('click', e => { if (e.target.id === 'previewDialog') e.target.close(); });

// === Shared GenApp factory (Seedance / Nano Banana) ===
function GenApp(prefix, appPath, mediaType) {
  return {
    appStatus: 'unknown',
    providers: {},
    provider: 't8star',
    models: [],
    baseUrl: 'https://ai.t8star.org',
    providerHint: '',
    keyHint: '',
    outputDir: '',
    dirHandle: null,
    autoDownload: false,
    submitting: false,
    statusText: '空闲',
    eventsText: '',
    archives: [],
    selectedArchive: '',
    archiveHint: '',
    savedMedia: {},
    wsTab: 'jobs',
    activityRecords: [],
    activityCounts: null,
    activityDetail: null,

    async init() {
      await this.loadConfig();
      this.loadArchives();
      this.buildUploadSlots();
      this.wireDrops();
    },

    async loadConfig() {
      const res = await api(`${appPath}/api/config`);
      if (!res?.providers) return;
      this.providers = res.providers;
      const defaultP = res.default_provider || Object.keys(res.providers)[0];
      this.applyProvider(defaultP);
      // PetiteVue may not sync v-model on <select> after dynamic options change — force it
      const sel = document.querySelector(`#${prefix}-form select[name="provider"]`);
      if (sel) {
        if (sel.value !== defaultP) sel.value = defaultP;
        // Also try after PetiteVue's next render cycle
        setTimeout(() => { if (sel.value !== defaultP) sel.value = defaultP; }, 0);
        setTimeout(() => { if (sel.value !== defaultP) sel.value = defaultP; }, 100);
      }
      this.keyHint = res.has_key ? `已检测到 key: ${res.masked_key}` : '未检测到本地 key';
    },

    applyProvider(provider) {
      const cfg = this.providers[provider];
      if (!cfg) return;
      this.provider = provider;
      this.baseUrl = cfg.base_url || '';
      this.providerHint = cfg.hint || '';
      this.models = cfg.models || [];
      setTimeout(() => {
        const defaults = cfg.defaults || {};
        for (const [k, v] of Object.entries(defaults)) {
          const el = document.querySelector(`#${prefix}-form [name="${k}"]`);
          if (!el || el.type === 'file') continue;
          if (el.type === 'checkbox') el.checked = !!v;
          else if (el.tagName === 'SELECT') {
            // Only set if the option exists (PetiteVue may not have rendered yet)
            if ([...el.options].some(o => o.value === String(v))) el.value = v;
          } else el.value = v;
        }
      });
    },

    async submit() {
      this.submitting = true;
      this.statusText = '提交中';
      const resultsEl = document.getElementById(`${prefix}-results`);
      const eventsEl = document.getElementById(`${prefix}-events`);
      if (resultsEl) resultsEl.innerHTML = '';
      if (eventsEl) eventsEl.textContent = '';
      const data = new FormData(document.getElementById(`${prefix}-form`));
      const res = await api(`${appPath}/api/jobs`, 'POST', data);
      if (!res || res.error) {
        this.submitting = false;
        this.statusText = res?.error || '提交失败';
        return;
      }
      await this.pollJob(res.job_id);
      this.submitting = false;
    },

    async pollJob(jobId) {
      const resultsEl = document.getElementById(`${prefix}-results`);
      while (true) {
        const job = await api(`${appPath}/api/jobs/${jobId}`);
        if (!job) break;
        this.statusText = `${job.status} ${job.done || 0}/${job.total || 0}`;
        this.eventsText = (job.events || []).map(e => `[${e.time}] ${e.message}`).join('\n');
        if (resultsEl) {
          const eventsList = (job.events || []).slice(-8).map(e => `<div style="font-size:11px;color:#d1e0ff;padding:2px 0"><span style="color:#697386">${escHtml(e.time)}</span> ${escHtml(e.message)}</div>`).join('');
          resultsEl.innerHTML = `<article class="result" style="border-color:#4f46e5;background:#101828;color:#e2e8f0;grid-column:1/-1">
            <div class="meta" style="color:#818cf8;font-weight:600;margin-bottom:6px">${escHtml(job.status)} · ${job.done || 0}/${job.total || 0} ${escHtml(job.errors?.[0] || '')}</div>
            ${eventsList || '<div style="color:#697386;font-size:11px">等待服务器响应...</div>'}
          </article>`;
          if (mediaType === 'video') {
            for (const r of job.results || []) {
              const url = `${appPath}${r.download_url}`;
              resultsEl.innerHTML += `<article class="result"><video controls src="${url}" style="max-height:200px"></video><a href="${url}" download="${r.filename}">下载</a><div class="meta">Run ${r.index} · ${r.task_id || ''}</div></article>`;
            }
          } else {
            for (const r of job.results || []) {
              for (const img of r.images || []) {
                const url = `${appPath}${img.download_url}`;
                resultsEl.innerHTML += `<article class="result"><img src="${url}" style="width:100%;max-height:180px;object-fit:contain;border-radius:6px;cursor:zoom-in" onclick="openPreview('image','${url}')"><a href="${url}" download="${img.filename}">下载</a><div class="meta">Run ${r.index}</div></article>`;
              }
            }
          }
          for (const err of job.errors || []) {
            resultsEl.innerHTML += `<article class="result" style="color:#ef4444">${escHtml(err)}</article>`;
          }
        }
        if (['succeeded', 'failed'].includes(job.status)) {
          if (job.status === 'succeeded' && this.dirHandle) {
            await this.saveToClient(job, mediaType);
          } else if (job.status === 'succeeded' && this.autoDownload) {
            this.triggerDownloads(job, mediaType);
          }
          break;
        }
        await new Promise(r => setTimeout(r, 2500));
      }
      this.statusText = '空闲';
    },

    async saveToClient(job, type) {
      try {
        const files = [];
        if (type === 'video') {
          for (const r of job.results || []) {
            if (r.download_url) files.push({ url: `${appPath}${r.download_url}`, filename: r.filename });
          }
        } else {
          for (const r of job.results || []) {
            for (const img of r.images || []) {
              if (img.download_url) files.push({ url: `${appPath}${img.download_url}`, filename: img.filename });
            }
          }
        }
        for (const { url, filename } of files) {
          const resp = await fetch(url);
          const blob = await resp.blob();
          const fh = await this.dirHandle.getFileHandle(filename, { create: true });
          const w = await fh.createWritable();
          await w.write(blob);
          await w.close();
        }
        if (files.length) this.statusText = `已保存 ${files.length} 个文件到 ${this.outputDir}`;
      } catch (e) {
        console.warn('saveToClient failed:', e);
      }
    },

    triggerDownloads(job, type) {
      const urls = [];
      if (type === 'video') {
        for (const r of job.results || []) {
          if (r.download_url) urls.push({ url: `${appPath}${r.download_url}`, filename: r.filename });
        }
      } else {
        for (const r of job.results || []) {
          for (const img of r.images || []) {
            if (img.download_url) urls.push({ url: `${appPath}${img.download_url}`, filename: img.filename });
          }
        }
      }
      for (const { url, filename } of urls) {
        const a = document.createElement('a');
        a.href = url; a.download = filename; a.style.display = 'none';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
      }
      if (urls.length) this.statusText = `已下载 ${urls.length} 个文件`;
    },

    async chooseOutputDir() {
      // 1. 后端系统原生目录选择器 — 返回真实路径，支持"打开目录"
      const res = await api(`${appPath}/api/choose-output-dir`, 'POST');
      if (res?.path) { this.outputDir = res.path; return; }
      // 2. 浏览器 File System Access API（Chrome, 不带真实路径, 不支持打开目录）
      if (window.showDirectoryPicker) {
        try {
          this.dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
          this.outputDir = this.dirHandle.name;
          return;
        } catch (e) { /* user cancelled */ }
      }
      // 3. 兜底：浏览器下载
      this.autoDownload = true;
      this.outputDir = '浏览器下载';
    },
    async desktopOutput() {
      const res = await api(`${appPath}/api/default-output-dir`);
      if (res?.path) this.outputDir = res.path;
    },
    async openOutputDir() {
      if (this.dirHandle && !this.outputDir.includes('/')) return;  // FSA API 无真实路径，无法打开
      const data = new FormData(); data.set('output_dir', this.outputDir);
      await api(`${appPath}/api/open-output-dir`, 'POST', data);
    },
    async cleanCache() {
      const res = await api(`${appPath}/api/cleanup-cache`, 'POST');
      if (res) alert(`清理完成：素材 ${res.media_deleted || 0} 个，日志 ${res.logs_deleted || 0} 个`);
    },

    async loadArchives() {
      const res = await api(`${appPath}/api/archives`);
      this.archives = res?.archives || [];
    },
    async saveArchive() {
      const data = new FormData(document.getElementById(`${prefix}-form`));
      if (this.savedMedia && Object.keys(this.savedMedia).length) {
        data.set('saved_media', JSON.stringify(this.savedMedia));
      }
      const res = await api(`${appPath}/api/preset`, 'POST', data);
      this.archiveHint = res?.archive ? `已保存: ${res.archive}` : (res?.error || '保存失败');
      if (res?.media) this.savedMedia = res.media;
      this.loadArchives();
    },
    async loadArchive() {
      if (!this.selectedArchive) return;
      const data = new FormData(); data.set('archive_name', this.selectedArchive);
      const res = await api(`${appPath}/api/archive/load`, 'POST', data);
      if (res?.values) {
        for (const [k, v] of Object.entries(res.values)) {
          const el = document.querySelector(`#${prefix}-form [name="${k}"]`);
          if (el && el.type !== 'file') {
            if (el.type === 'checkbox') el.checked = ['on', 'true', '1'].includes(v);
            else el.value = v;
          }
        }
      }
      if (res?.media) {
        this.savedMedia = res.media;
        for (const [name, item] of Object.entries(res.media)) {
          const el = document.querySelector(`#${prefix}-form [name="${name}"]`);
          const drop = el?.closest('.drop');
          if (drop && item.url) {
            showPreview(drop, name, item.url, item.filename);
          }
        }
      } else {
        this.savedMedia = {};
      }
      this.archiveHint = `已读取: ${this.selectedArchive}`;
    },
    async deleteArchive() {
      if (!this.selectedArchive) return;
      const data = new FormData(); data.set('archive_name', this.selectedArchive);
      await api(`${appPath}/api/archive/delete`, 'POST', data);
      this.loadArchives();
    },

    async loadActivity() {
      const res = await api(`${appPath}/api/activity`);
      this.activityRecords = res?.records || [];
      this.activityCounts = res?.counts || null;
      this.activityDetail = null;
    },
    async showDetail(id) {
      const res = await api(`${appPath}/api/activity/${id}`);
      if (res) this.activityDetail = res;
    },
    restoreActivity() {
      const r = this.activityDetail?.restore;
      if (!r) { alert('该记录无法恢复'); return; }
      for (const [k, v] of Object.entries(r.values || {})) {
        if (k === 'output_dir') { this.outputDir = v; continue; }
        const el = document.querySelector(`#${prefix}-form [name="${k}"]`);
        if (el && el.type !== 'file') {
          if (el.type === 'checkbox') el.checked = ['on', 'true', '1'].includes(v);
          else el.value = v;
        }
      }
      if (r.values?.provider) this.applyProvider(r.values.provider);
      if (r.values?.base_url) this.baseUrl = r.values.base_url;
      if (r.media) {
        this.savedMedia = r.media;
        for (const [name, item] of Object.entries(r.media)) {
          const el = document.querySelector(`#${prefix}-form [name="${name}"]`);
          const drop = el?.closest('.drop');
          if (drop && item.url) {
            showPreview(drop, name, item.url, item.filename);
          }
        }
      }
      this.wsTab = 'jobs';
    },

    buildUploadSlots() {},
    wireDrops() {
      setTimeout(() => {
        document.querySelectorAll(`#tab-${prefix === 'sd' ? 'seedance' : 'nb'} .drop`).forEach(drop => {
          const input = drop.querySelector('input[type="file"]');
          if (input && !input.dataset.wired) { input.dataset.wired = '1'; wireFileDrop(drop, input, input.name); }
        });
      }, 0);
    }
  };
}

function SeedanceApp() {
  const app = GenApp('sd', '/seedance', 'video');
  app.buildUploadSlots = function () {
    const ir = document.getElementById('sd-imageRefs');
    const vr = document.getElementById('sd-videoRefs');
    const ar = document.getElementById('sd-audioRefs');
    if (ir) for (let i = 1; i <= 9; i++) makeDrop(ir, `ref_image_${i}`, `@ref_image${i}`, 'image/*', 'sd-form');
    if (vr) for (let i = 1; i <= 3; i++) makeDrop(vr, `ref_video_${i}`, `参考视频${i}`, 'video/*', 'sd-form');
    if (ar) for (let i = 1; i <= 3; i++) makeDrop(ar, `ref_audio_${i}`, `参考音频${i}`, 'audio/*', 'sd-form');
  };
  return app;
}

function NanoBananaApp() {
  const app = GenApp('nb', '/nano-banana', 'image');
  app.buildUploadSlots = function () {
    const ir = document.getElementById('nb-imageRefs');
    if (ir) for (let i = 1; i <= 14; i++) makeDrop(ir, `image_${i}`, `Image ${i}`, 'image/*', 'nb-form');
  };
  return app;
}

// === Dreamina App ===
function DreaminaApp() {
  return {
    appStatus: 'unknown',
    setupMode: 'checking',
    setupTitle: '环境检测中...',
    setupDesc: '',
    setupLog: '',
    setupSpinner: true,
    loginInProgress: false,
    loggedIn: false,
    credit: '',
    outputDir: '',
    dirHandle: null,
    autoDownload: false,
    major: 'image',
    mode: 'text2image',
    imageSub: 'text2image',
    videoSub: 'frames2video',
    frameCount: 2,
    submitting: false,
    wsTab: 'jobs',
    runningCount: 0,
    historyFilter: 'all',
    archives: [],
    selectedArchive: '',
    archiveName: '',
    accounts: [],
    activeAccount: null,
    dispatchMode: 'manual',
    accountLoginId: null,
    accountLoginUrl: null,

    async init() {
      window._dmRestore = (jobId) => this.restoreFromHistory(jobId);
      await this.checkEnv();
      this.buildSlots();
      if (this.loggedIn && this.accounts.length) this.refreshAllAccounts();
    },

    async refreshAllAccounts() {
      for (const acc of this.accounts) {
        if (acc.logged_in) {
          api(`/dreamina/api/accounts/${acc.id}/refresh`, 'POST');
        }
      }
      setTimeout(() => this.loadAccounts(), 2000);
    },

    async checkEnv() {
      this.setupMode = 'checking';
      this.setupTitle = '环境检测中...';
      this.setupSpinner = true;
      for (let i = 0; i < 5; i++) {
        const res = await api('/dreamina/api/env/check');
        if (res?.ok) {
          this.setupSpinner = false;
          if (!res.cli_installed) { this.setupMode = 'install'; this.setupTitle = '即梦 CLI 未安装'; return; }
          if (res.accounts) {
            this.accounts = res.accounts.accounts || [];
            this.activeAccount = res.accounts.active_account;
            this.dispatchMode = res.accounts.dispatch_mode || 'manual';
          }
          const hasLoggedIn = this.accounts.some(a => a.logged_in);
          if (!res.logged_in && !hasLoggedIn) { this.setupMode = 'login'; this.setupTitle = '需要登录即梦账号'; return; }
          this.setupMode = null;
          this.loggedIn = true;
          this.appStatus = 'ready';
          if (res.credit) this.credit = String(res.credit).slice(0, 40);
          this.loadJobs();
          this.loadHistory();
          this.loadArchives();
          return;
        }
        await new Promise(r => setTimeout(r, 2000));
      }
      this.setupSpinner = false;
      this.setupMode = 'error';
      this.setupTitle = '无法连接后端';
    },

    async installCli() {
      this.setupSpinner = true;
      this.setupLog = '';
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
          const d = JSON.parse(line.slice(6));
          if (d.type === 'log') this.setupLog += d.text + '\n';
          else if (d.type === 'done') { this.setupSpinner = false; if (d.success) this.checkEnv(); }
        }
      }
    },

    async startLogin() {
      this.loginInProgress = true;
      this.setupSpinner = true;
      this.setupTitle = '等待授权...';
      const res = await api('/dreamina/api/env/login', 'POST');
      if (res?.auth_url) this.setupDesc = `<a href="${res.auth_url}" target="_blank" style="color:#2673e8;">手动打开授权页</a>`;
      let elapsed = 0;
      const poll = setInterval(async () => {
        elapsed += 3;
        if (elapsed > 120) { clearInterval(poll); this.loginInProgress = false; this.setupSpinner = false; this.setupTitle = '登录超时'; return; }
        const r = await api('/dreamina/api/env/login-poll');
        if (r?.logged_in) { clearInterval(poll); this.loginInProgress = false; this.setupMode = null; this.loggedIn = true; this.appStatus = 'ready'; this.loadJobs(); this.loadHistory(); this.loadArchives(); }
      }, 3000);
    },

    async cancelLogin() {
      await api('/dreamina/api/env/login-cancel', 'POST');
      this.loginInProgress = false;
      this.setupSpinner = false;
      this.setupTitle = '登录已取消';
      this.setupMode = 'login';
    },

    async switchAccount() {
      this.setupMode = 'login';
      this.setupTitle = '切换账号中...';
      this.setupSpinner = true;
      await api('/dreamina/api/env/logout', 'POST');
      this.startLogin();
    },

    // === Account Management ===

    async loadAccounts() {
      const res = await api('/dreamina/api/accounts');
      if (res?.ok) {
        this.accounts = res.accounts || [];
        this.activeAccount = res.active_account;
        this.dispatchMode = res.dispatch_mode || 'manual';
      }
    },

    async addAccount(name) {
      const res = await api('/dreamina/api/accounts', 'POST', JSON.stringify({ name: name || '' }));
      if (res?.ok) {
        this.accounts.push(res.account);
        if (!this.activeAccount) this.activeAccount = res.account.id;
        this.loginAccount(res.account.id);
      }
    },

    async loginAccount(accId) {
      this.accountLoginId = accId;
      const res = await api(`/dreamina/api/accounts/${accId}/login`, 'POST');
      if (res?.auth_url) {
        const acc = this.accounts.find(a => a.id === accId);
        const name = acc?.name || accId;
        this.accountLoginUrl = { id: accId, url: res.auth_url, name };
      }
      let elapsed = 0;
      const poll = setInterval(async () => {
        elapsed += 3;
        if (elapsed > 120) { clearInterval(poll); this.accountLoginId = null; this.accountLoginUrl = null; return; }
        const r = await api(`/dreamina/api/accounts/${accId}/login-poll`);
        if (r?.logged_in) {
          clearInterval(poll);
          this.accountLoginId = null;
          this.accountLoginUrl = null;
          await this.loadAccounts();
          if (!this.loggedIn) {
            this.setupMode = null;
            this.loggedIn = true;
            this.appStatus = 'ready';
            this.loadJobs();
            this.loadHistory();
            this.loadArchives();
          }
        }
      }, 3000);
    },

    async logoutAccount(accId) {
      await api(`/dreamina/api/accounts/${accId}/logout`, 'POST');
      await this.loadAccounts();
    },

    async refreshAccount(accId) {
      await api(`/dreamina/api/accounts/${accId}/refresh`, 'POST');
      await this.loadAccounts();
    },

    async deleteAccount(accId) {
      if (!confirm('确认删除该账号？')) return;
      await api(`/dreamina/api/accounts/${accId}/delete`, 'POST');
      await this.loadAccounts();
    },

    async setActiveAccount(accId) {
      await api('/dreamina/api/accounts/active', 'POST', JSON.stringify({ account_id: accId }));
      this.activeAccount = accId;
    },

    async setDispatchMode(mode) {
      await api('/dreamina/api/dispatch-mode', 'POST', JSON.stringify({ mode }));
      this.dispatchMode = mode;
    },

    getActiveAccountName() {
      const acc = this.accounts.find(a => a.id === this.activeAccount);
      return acc ? acc.name : '未选择';
    },

    async updateCli() {
      this.setupMode = 'install';
      this.setupTitle = '更新中...';
      this.installCli();
    },

    async submit() {
      this.submitting = true;
      const data = new FormData(document.getElementById('dm-form'));
      data.set('mode', this.mode);
      const res = await api(`/dreamina/api/${this.mode}`, 'POST', data);
      if (!res || res.error) { this.submitting = false; alert(res?.error || '提交失败'); return; }
      this.submitting = false;
      this.pollJob(res.job_id || res.id);
    },

    async pollJob(jobId) {
      if (!jobId) return;
      const el = document.getElementById('dm-jobsList');
      const card = document.createElement('div');
      card.className = 'result';
      const cardId = 'card-' + jobId.slice(0, 8);
      card.id = cardId;
      card.style.cssText = 'border-color:#4f46e5;background:#101828;color:#e2e8f0;grid-column:1/-1';
      card.innerHTML = `<div class="meta">Job ${jobId.slice(0, 8)} - 提交中...</div>`;
      el.prepend(card);
      while (true) {
        const res = await api(`/dreamina/api/jobs/${jobId}`);
        if (!res) break;
        const job = res.job || res;
        const events = (job.events || []).slice(-6).map(e => `<div style="font-size:11px;color:#d1e0ff;padding:2px 0"><span style="color:#697386">${escHtml(e.time)}</span> ${escHtml(e.message)}</div>`).join('');
        let html = `<div class="meta" style="color:#818cf8;font-weight:600;margin-bottom:6px">${job.task_type || ''} · ${job.status || 'unknown'} · ${job.done || 0}/${job.total || 0}</div>`;
        if (events) html += events;
        else html += '<div style="color:#697386;font-size:11px">等待服务器响应...</div>';
        if (job.status === 'failed') html += `<div class="meta" style="color:#ef4444">${escHtml(job.error || '生成失败')}</div>`;
        const allFiles = [];
        for (const r of job.results || []) { if (r.files) allFiles.push(...r.files); }
        if (job.result?.files) allFiles.push(...job.result.files);
        html += this.renderFiles(allFiles);
        card.innerHTML = html;
        if (['completed', 'failed'].includes(job.status)) {
          card.style.cssText = '';
          card.id = '';
          if (job.status === 'completed' && this.dirHandle && allFiles.length) {
            await this.saveDreaminaToClient(allFiles);
          } else if (job.status === 'completed' && this.autoDownload && allFiles.length) {
            this.triggerDreaminaDownloads(allFiles);
          }
          break;
        }
        await new Promise(r => setTimeout(r, 3000));
      }
      this.loadHistory();
    },

    async saveDreaminaToClient(files) {
      try {
        let saved = 0;
        for (const f of files) {
          const url = '/dreamina/' + f.replace(/^\//, '');
          const filename = f.split('/').pop();
          const resp = await fetch(url);
          const blob = await resp.blob();
          const fh = await this.dirHandle.getFileHandle(filename, { create: true });
          const w = await fh.createWritable();
          await w.write(blob);
          await w.close();
          saved++;
        }
        if (saved) this.statusText = `已保存 ${saved} 个文件到 ${this.outputDir}`;
      } catch (e) {
        console.warn('saveDreaminaToClient failed:', e);
      }
    },

    triggerDreaminaDownloads(files) {
      for (const f of files) {
        const url = '/dreamina/' + f.replace(/^\//, '');
        const filename = f.split('/').pop();
        const a = document.createElement('a');
        a.href = url; a.download = filename; a.style.display = 'none';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
      }
      this.statusText = `已下载 ${files.length} 个文件`;
    },

    renderFiles(files) {
      if (!files?.length) return '';
      return files.map(f => {
        const url = '/dreamina/' + f.replace(/^\//, '');
        const name = f.split('/').pop();
        if (/\.(mp4|mov|webm|avi)$/i.test(f)) return `<video controls src="${url}" style="width:100%;max-height:200px;border-radius:5px;margin-top:6px"></video><a href="${url}" download="${name}">下载</a>`;
        return `<img src="${url}" style="width:100%;max-height:180px;object-fit:contain;border-radius:5px;margin-top:6px;cursor:zoom-in" onclick="openPreview('image','${url}')"><a href="${url}" download="${name}">下载</a>`;
      }).join('');
    },

    async loadJobs() {
      const res = await api('/dreamina/api/jobs');
      if (!res) return;
      const jobs = res.jobs || [];
      const running = jobs.filter(j => !['completed', 'failed'].includes(j.status));
      this.runningCount = running.length;
      for (const j of running) this.pollJob(j.job_id || j.id);
    },

    async loadHistory() {
      const res = await api('/dreamina/api/history');
      const list = document.getElementById('dm-historyList');
      if (!list) return;
      const items = res?.history || [];
      const filtered = items.slice().reverse().filter(item => {
        if (this.historyFilter === 'all') return true;
        const tt = item.task_type || '';
        return this.historyFilter === 'video' ? tt.includes('video') || tt.includes('frame') : tt.includes('image');
      });
      if (!filtered.length) { list.innerHTML = '<div style="color:#697386;font-size:12px">暂无历史</div>'; return; }
      list.innerHTML = filtered.slice(0, 30).map(item => {
        const files = item.result?.files || [];
        const thumb = files[0] ? '/dreamina/' + files[0].replace(/^\//, '') : '';
        const isVid = thumb && /\.(mp4|mov|webm)$/i.test(thumb);
        const prompt = item.params?.prompt || '';
        const status = item.status || '';
        const statusColor = status === 'completed' ? '#10b981' : status === 'failed' ? '#ef4444' : '#697386';
        let preview = '';
        if (thumb) {
          if (isVid) preview = `<video src="${thumb}" style="width:100%;max-height:120px;border-radius:5px;margin-top:4px" preload="metadata"></video>`;
          else preview = `<img src="${thumb}" style="width:100%;max-height:120px;object-fit:contain;border-radius:5px;margin-top:4px;cursor:zoom-in" onclick="event.stopPropagation();openPreview('image','${thumb}')">`;
        }
        const logs = item.cli_logs || [];
        const logId = 'cli-log-' + item.job_id;
        let logSection = '';
        if (logs.length) {
          const logHtml = logs.map(l => {
            const cmdDisp = escHtml(l.command || '');
            const outDisp = escHtml((l.stdout || '').slice(0, 800));
            const errDisp = l.stderr ? escHtml(l.stderr.slice(0, 300)) : '';
            return `<div style="margin-bottom:6px"><div style="color:#a78bfa">$ ${cmdDisp}</div><div style="color:#6ee7b7">exitcode: ${l.returncode}</div>${outDisp ? `<div style="color:#e2e8f0;white-space:pre-wrap;word-break:break-all">${outDisp}</div>` : ''}${errDisp ? `<div style="color:#fca5a5">${errDisp}</div>` : ''}</div>`;
          }).join('');
          logSection = `<div style="margin-top:4px"><span class="meta" style="cursor:pointer;color:#818cf8;user-select:none" onclick="event.stopPropagation();var el=document.getElementById('${logId}');el.style.display=el.style.display==='none'?'block':'none'">CLI 详情 ▾</span><div id="${logId}" style="display:none;margin-top:4px;background:#1e1b2e;color:#e2e8f0;font-family:monospace;font-size:11px;padding:8px;border-radius:6px;max-height:240px;overflow:auto">${logHtml}</div></div>`;
        }
        return `<div class="result" style="cursor:pointer" onclick="window._dmRestore('${item.job_id}')">
          <div class="meta"><span style="color:${statusColor}">${escHtml(status)}</span> · ${escHtml(item.task_type || '')} · ${escHtml((item.created_at || '').slice(5, 16))}</div>
          <div class="meta" style="margin-top:2px">${escHtml(prompt.slice(0, 60))}${prompt.length > 60 ? '...' : ''}</div>
          ${preview}
          ${files.length > 1 ? `<div class="meta" style="margin-top:2px">共 ${files.length} 个文件</div>` : ''}
          ${logSection}
        </div>`;
      }).join('');
    },

    async restoreFromHistory(jobId) {
      const res = await api('/dreamina/api/history');
      const items = res?.history || [];
      const item = items.find(i => i.job_id === jobId);
      if (!item?.params) return;
      const params = item.params;
      if (params.mode) {
        const isVideo = params.mode.includes('video') || params.mode.includes('frame');
        this.major = isVideo ? 'video' : 'image';
        this.mode = params.mode;
        if (isVideo) this.videoSub = params.mode;
        else this.imageSub = params.mode;
      }
      for (const [k, v] of Object.entries(params)) {
        if (k === 'mode') continue;
        if (k === 'output_dir') { this.outputDir = v; continue; }
        const el = document.querySelector(`#dm-form [name="${k}"]`);
        if (el && el.type !== 'file') el.value = v || '';
      }
      this.wsTab = 'jobs';
    },

    async chooseOutputDir() {
      const res = await api('/dreamina/api/choose-output-dir', 'POST');
      if (res?.path) { this.outputDir = res.path; return; }
      if (window.showDirectoryPicker) {
        try {
          this.dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
          this.outputDir = this.dirHandle.name;
          return;
        } catch (e) { /* user cancelled */ }
      }
      this.autoDownload = true;
      this.outputDir = '浏览器下载';
    },
    async desktopOutput() {
      const res = await api('/dreamina/api/default-output-dir');
      if (res?.path) this.outputDir = res.path;
    },
    async openOutputDir() {
      if (this.dirHandle && !this.outputDir.includes('/')) return;
      const data = new FormData(); data.set('output_dir', this.outputDir);
      await api('/dreamina/api/open-output-dir', 'POST', data);
    },
    async cleanCache() {
      const res = await api('/dreamina/api/cleanup-cache', 'POST');
      if (res) alert(`清理完成：素材 ${res.media_deleted || 0} 个，日志 ${res.logs_deleted || 0} 个`);
    },

    async loadArchives() {
      const res = await api('/dreamina/api/archives');
      this.archives = res?.archives || [];
    },
    async saveArchive() {
      if (!this.archiveName) return;
      const data = new FormData(document.getElementById('dm-form'));
      data.set('archive_name', this.archiveName);
      await api('/dreamina/api/preset', 'POST', data);
      this.loadArchives();
    },
    async loadArchive() {
      if (!this.selectedArchive) return;
      const data = new FormData(); data.set('archive_name', this.selectedArchive);
      const res = await api('/dreamina/api/archive/load', 'POST', data);
      if (res?.values) {
        for (const [k, v] of Object.entries(res.values)) {
          const el = document.querySelector(`#dm-form [name="${k}"]`);
          if (el && el.type !== 'file') el.value = v;
        }
      }
    },
    async deleteArchive() {
      if (!this.selectedArchive) return;
      const data = new FormData(); data.set('archive_name', this.selectedArchive);
      await api('/dreamina/api/archive/delete', 'POST', data);
      this.loadArchives();
    },

    addFrame() { if (this.frameCount < 9) { this.frameCount++; this.rebuildFrames(); } },
    removeFrame() { if (this.frameCount > 2) { this.frameCount--; this.rebuildFrames(); } },
    rebuildFrames() {
      const c = document.getElementById('dm-framesContainer');
      if (!c) return;
      c.innerHTML = '';
      for (let i = 1; i <= this.frameCount; i++) makeDrop(c, `frame_${i}`, `帧${i}`, 'image/*', 'dm-form');
    },

    buildSlots() {
      setTimeout(() => {
        const ir = document.getElementById('dm-imageRefs');
        const mir = document.getElementById('dm-mmImageRefs');
        const mvr = document.getElementById('dm-mmVideoRefs');
        const mar = document.getElementById('dm-mmAudioRefs');
        if (ir) for (let i = 1; i <= 9; i++) makeDrop(ir, `ref_image_${i}`, `参考${i}`, 'image/*', 'dm-form');
        if (mir) for (let i = 1; i <= 9; i++) makeDrop(mir, `mm_image_${i}`, `参考${i}`, 'image/*', 'dm-form');
        if (mvr) for (let i = 1; i <= 3; i++) makeDrop(mvr, `mm_video_${i}`, `视频${i}`, 'video/*', 'dm-form');
        if (mar) for (let i = 1; i <= 3; i++) makeDrop(mar, `mm_audio_${i}`, `音频${i}`, 'audio/*', 'dm-form');
        this.rebuildFrames();
        document.querySelectorAll('#tab-dreamina .drop').forEach(drop => {
          const input = drop.querySelector('input[type="file"]');
          if (input && !input.dataset.wired) { input.dataset.wired = '1'; wireFileDrop(drop, input, input.name); }
        });
      });
    }
  };
}

// Global bridge for onclick in innerHTML
window._dmRestore = null;
window.openPreview = openPreview;

// === Stats App ===
function StatsApp() {
  return {
    todayJobs: 0,
    todayRequests: 0,
    byApp: {},
    byIp: {},
    recentActivity: [],

    async init() {
      this.loadStats();
      this.loadActivity();
      this.loadPlatformStatus();
      setInterval(() => this.loadPlatformStatus(), 10000);
      setInterval(() => this.loadStats(), 30000);
    },

    async loadPlatformStatus() {
      const res = await api('/api/platform/status');
      if (!res?.ok) return;
      document.getElementById('lanInfo').textContent = `LAN: http://${res.lan_ip}:${res.portal_port}`;
      document.getElementById('barStats').textContent = `今日: ${this.todayJobs} jobs`;
      for (const app of res.apps || []) {
        const mapping = { seedance: 'sd', 'nano-banana': 'nb', dreamina: 'dm' };
        const prefix = mapping[app.name];
        if (prefix) {
          const panels = document.querySelectorAll(`#tab-${app.name === 'nano-banana' ? 'nb' : app.name}`);
        }
      }
    },

    async loadStats() {
      const res = await api('/api/platform/stats');
      if (!res?.ok) return;
      this.todayJobs = res.today_jobs || 0;
      this.todayRequests = res.today_requests || 0;
      this.byApp = res.by_app || {};
      this.byIp = res.by_ip || {};
      document.getElementById('barStats').textContent = `今日: ${this.todayJobs} jobs`;
    },

    async loadActivity() {
      const res = await api('/api/platform/activity');
      if (!res?.ok) return;
      this.recentActivity = (res.activity || []).slice(0, 30);
    }
  };
}

// === Mount ===
PetiteVue.createApp({
  SeedanceApp,
  NanoBananaApp,
  DreaminaApp,
  StatsApp,
  openPreview
}).mount();
