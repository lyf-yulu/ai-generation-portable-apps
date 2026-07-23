'use strict';

// ============================================================
// MODE DETECTION
// ============================================================
// When accessed through Portal proxy: path starts with /seedance/
// When accessed standalone: path starts with /
const IN_PORTAL = window.location.pathname.startsWith('/seedance/');
const APP_PATH = IN_PORTAL ? '/seedance' : '';

// Lowercased job.status values considered terminal (used to gate poll loops and running-indicator recomputation).
const TERMINAL_STATUSES = new Set(['succeeded', 'success', 'failed', 'fail', 'failure', 'cancelled', 'canceled']);

// ============================================================
// UTILITIES
// ============================================================
function workspaceId() {
  let id = localStorage.getItem('workspace_id');
  if (!id) { id = crypto.randomUUID(); localStorage.setItem('workspace_id', id); }
  return id;
}

function getActiveWorkspaceId() {
  return window._activeWorkspaceId || workspaceId();
}

async function api(url, method, body) {
  try {
    const wsId = getActiveWorkspaceId();
    const sep = url.includes('?') ? '&' : '?';
    const urlWithWs = url + sep + 'ws=' + encodeURIComponent(wsId);
    const headers = { 'X-Workspace-Id': wsId };
    const keyId = localStorage.getItem('portal_key_id_seedance');
    if (keyId) headers['X-Key-Id'] = keyId;
    const opts = { method: method || 'GET', headers };
    if (body) opts.body = body;
    const res = await fetch(urlWithWs, opts);
    return await res.json();
  } catch (e) { return null; }
}

// Status-aware single poll for pollJob's retry logic. Unlike api() — which
// collapses HTTP 404 / 5xx / network-error / bad-JSON all into a single null
// (or, worse, returns a truthy {error:...} body that pollJob mistook for a
// real job and looped on forever showing "unknown") — this distinguishes:
//   {kind:'ok', job}  HTTP 200 + a job object carrying a status field
//   {kind:'gone'}     HTTP 404 — job no longer exists (sub-app restarted and
//                     cleared its in-memory JOBS). Poll stops cleanly.
//   {kind:'error'}    network error / timeout / 5xx / non-JSON — transient,
//                     caller retries with backoff.
async function pollJobOnce(url) {
  try {
    const wsId = getActiveWorkspaceId();
    const sep = url.includes('?') ? '&' : '?';
    const urlWithWs = url + sep + 'ws=' + encodeURIComponent(wsId);
    const headers = { 'X-Workspace-Id': wsId };
    const keyId = localStorage.getItem('portal_key_id_seedance');
    if (keyId) headers['X-Key-Id'] = keyId;
    const res = await fetch(urlWithWs, { method: 'GET', headers });
    if (res.status === 404) return { kind: 'gone' };
    if (!res.ok) return { kind: 'error' };
    const job = await res.json();
    if (!job || typeof job.status === 'undefined') return { kind: 'error' };
    return { kind: 'ok', job };
  } catch (e) {
    return { kind: 'error' };
  }
}

function escHtml(s) {
  return s ? String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : '';
}

// ============================================================
// FILE DROP / PREVIEW HELPERS
// ============================================================
function clearDropMedia(drop, input, name) {
  input.value = '';
  drop.classList.remove('hasPreview');
  drop.querySelector('.preview')?.remove();
  const span = drop.querySelector('span');
  if (span) span.textContent = '未上传';
  const app = window._app_sd;
  if (app && app.savedMedia) delete app.savedMedia[name];
  if (window._currentSavedMedia) delete window._currentSavedMedia[name];
  if (app && typeof app.saveWorkspaceDraft === 'function') app.saveWorkspaceDraft();
}

function ensureRemoveBtn(drop, input, name) {
  // Static drops (first_frame/last_frame in index.html) lack the remove button
  // that makeDrop() injects for dynamically-built ref slots. Add one here so
  // every wired drop gets a working 移除 button on the single wiring path.
  if (drop.querySelector('.removeMediaBtn')) return;
  const rmBtn = document.createElement('button');
  rmBtn.className = 'removeMediaBtn';
  rmBtn.type = 'button';
  rmBtn.textContent = '移除';
  rmBtn.addEventListener('click', e => {
    e.preventDefault();
    e.stopPropagation();
    clearDropMedia(drop, input, name);
  });
  drop.appendChild(rmBtn);
}

function wireFileDrop(drop, input, name) {
  if (input.dataset.wired) return;
  input.dataset.wired = '1';
  ensureRemoveBtn(drop, input, name);
  input.addEventListener('change', async () => {
    const f = input.files?.[0];
    if (!f) {
      drop.classList.remove('hasPreview');
      drop.querySelector('.preview')?.remove();
      drop.querySelector('span').textContent = '未上传';
      return;
    }
    // Immediate local preview
    const localUrl = URL.createObjectURL(f);
    showPreview(drop, name, localUrl, f.name);
    // Upload to server so the file survives tab switch / refresh / archive save.
    try {
      const fd = new FormData();
      fd.set(input.name, f);
      const res = await api(APP_PATH + '/api/media/upload', 'POST', fd);
      if (res && res.stored) {
        const app = window._app_sd;
        const media = (app && app.savedMedia) || window._currentSavedMedia || {};
        media[input.name] = {
          filename: res.filename,
          mime: res.mime,
          stored: res.stored,
          url: res.url,
        };
        if (app) app.savedMedia = media;
        window._currentSavedMedia = media;
        const serverUrl = res.url.startsWith('/api/') ? APP_PATH + res.url : res.url;
        showPreview(drop, name, serverUrl, res.filename);
        try { URL.revokeObjectURL(localUrl); } catch (e) {}
        if (app && typeof app.saveWorkspaceDraft === 'function') app.saveWorkspaceDraft();
      }
    } catch (e) { /* silent fallback: local blob preview stays */ }
  });
  drop.addEventListener('dragover', e => {
    e.preventDefault();
    drop.classList.add('isDragging');
  });
  drop.addEventListener('dragleave', () => drop.classList.remove('isDragging'));
  drop.addEventListener('drop', e => {
    e.preventDefault();
    drop.classList.remove('isDragging');
    const f = e.dataTransfer?.files?.[0];
    if (!f) return;
    const dt = new DataTransfer();
    dt.items.add(f);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function showPreview(drop, name, url, filename) {
  drop.classList.add('hasPreview');
  drop.querySelector('.preview')?.remove();
  const kind = name.includes('video') ? 'video' : name.includes('audio') ? 'audio' : 'image';
  const media = document.createElement(kind === 'image' ? 'img' : kind);
  media.className = 'preview';
  media.src = url;
  if (kind !== 'image') media.controls = true;
  if (kind !== 'audio') {
    media.addEventListener('click', e => {
      e.preventDefault();
      e.stopPropagation();
      openPreview(kind, url);
    });
  }
  drop.insertBefore(media, drop.querySelector('span'));
  drop.querySelector('span').textContent = filename || '已上传';
}

function openPreview(kind, url) {
  const dlg = document.getElementById('previewDialog');
  const body = document.getElementById('previewDialogBody');
  if (!dlg || !body) return;
  body.innerHTML = '';
  const m = document.createElement(kind === 'image' ? 'img' : 'video');
  m.src = url;
  if (kind === 'video') m.controls = true;
  body.append(m);
  dlg.showModal();
}

function makeDrop(container, name, label, accept, formId) {
  const el = document.createElement('label');
  el.className = 'drop';
  el.textContent = label;
  const input = document.createElement('input');
  input.name = name;
  input.type = 'file';
  input.accept = accept;
  if (formId) input.setAttribute('form', formId);
  const span = document.createElement('span');
  span.textContent = '未上传';
  el.append(input, span);
  wireFileDrop(el, input, name);
  container.appendChild(el);
}

// ============================================================
// PROVIDER CONFIG HELPER
// ============================================================
function providersFromConfig(providers) {
  if (!providers || !Object.keys(providers).length) return null;
  const result = {};
  for (const [id, p] of Object.entries(providers)) {
    if (p && p.hidden) continue;  // operator-disabled provider, skip from UI
    result[id] = {
      base_url: p.base_url || '',
      models: (p.models || []).map(m => {
        if (typeof m === 'string') return { id: m, label: m };
        return { id: m.id || m, label: m.label || m.id || m };
      }),
      hint: p.hint || '',
      label: p.label || id,
      defaults: p.defaults || {},
    };
  }
  return Object.keys(result).length ? result : null;
}

// ============================================================
// FALLBACK PROVIDERS (used if config fails to load)
// ============================================================
const FALLBACK_PROVIDERS = {
  volcengine: {
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    models: [
      { id: 'doubao-seedance-2-0-260128', label: 'doubao-seedance-2-0-260128' },
      { id: 'doubao-seedance-2-0-fast-260128', label: 'doubao-seedance-2-0-fast-260128' },
    ],
    hint: '豆包官方火山方舟 API。本地图/视频/音频参考素材会先上传到公司 TOS bucket，再以预签名 URL 传给方舟。',
    label: '豆包官方 / 火山方舟',
    defaults: {},
  },
};

// ============================================================
// SEEDANCE APP FACTORY (PetiteVue data object)
// ============================================================
function SeedanceApp() {
  const wid = workspaceId();
  const wsKey = 'seedance.workspace.' + wid;
  const urlParams = new URLSearchParams(window.location.search);

  // Resolve workspace id from URL param, falling back to localStorage
  let effectiveWorkspaceId = urlParams.get('ws');
  if (!effectiveWorkspaceId) {
    effectiveWorkspaceId = wid;
  }

  // --- DOM helpers ---
  function field(name) {
    return document.querySelector('#sd-form [name="' + name + '"]')
        || document.querySelector('[form="sd-form"][name="' + name + '"]')
        || document.querySelector('[name="' + name + '"]');
  }

  function mediaKind(name) {
    if (name.includes('video')) return 'video';
    if (name.includes('audio')) return 'audio';
    return 'image';
  }

  function mediaSnapshot(src) {
    return JSON.parse(JSON.stringify(src || {}));
  }

  function clearMediaPreview(drop) {
    if (!drop) return;
    drop.classList.remove('hasPreview');
    drop.querySelector('.preview')?.remove();
    drop.querySelector('span').textContent = '未上传';
  }

  function clearAllMediaPreviews() {
    document.querySelectorAll('.drop').forEach(clearMediaPreview);
  }

  function collectFormValues() {
    const values = {};
    const form = document.getElementById('sd-form');
    if (!form) return values;
    for (const item of form.elements) {
      if (!item.name || item.type === 'file') continue;
      values[item.name] = item.type === 'checkbox' ? (item.checked ? 'on' : '') : item.value;
    }
    return values;
  }

  return {
    // --- Reactive State ---
    inPortal: IN_PORTAL,
    appPath: APP_PATH,
    appStatus: 'unknown',
    providers: {},
    provider: 'volcengine',
    models: [],
    baseUrl: '',
    providerHint: '',
    keyHint: '',
    outputDir: '',
    dirHandle: null,
    autoDownload: false,
    submitting: false,
    statusText: '空闲',
    eventsText: '',
    runtimeTick: 0,
    archives: [],
    selectedArchive: '',
    archiveHint: '',
    optimizing: false,
    optimizedPrompt: '',
    optimizeError: '',
    savedMedia: {},
    wsTab: 'jobs',
    activityRecords: [],
    jobs: [],
    activityCounts: null,
    activityDetail: null,

    // Standalone-specific fields
    customModel: '',
    webSearch: false,
    pollInterval: 10,
    timeout: 3600,
    workspaceName: '默认主题',
    workspaceHint: '',

    // --- Tab bar state (Task 2) ---
    tabs: [],                   // [{id, name, running}]
    activeTabId: 'default',
    editingTabId: null,         // tab id being renamed inline, or null
    _closeConfirmTabId: null,   // tab id that opened the close-confirm modal
    _tabStateCache: {},         // { wsId: {statusText, eventsText, submitting, baseUrl, provider, models, workspaceName} }

    // Internal (non-reactive but accessible)
    _workspaceId: effectiveWorkspaceId,
    _workspaceKey: wsKey,
    _workspaceSaveTimer: 0,

    // ============================================================
    // INIT
    // ============================================================
    async init() {
      window._app_sd = this;
      window._currentSavedMedia = this.savedMedia;
      this.buildUploadSlots();
      this.wireDrops();
      try { await this.loadConfig(); } catch (e) { console.warn('loadConfig failed:', e); }
      try { await this.loadArchives(); } catch (e) { console.warn('loadArchives failed:', e); }

      // --- Tab bar restoration (Task 2) ---
      const raw = localStorage.getItem('seedance.tabs');
      if (raw) {
        try {
          const data = JSON.parse(raw);
          if (data.tabs && data.tabs.length) {
            this.tabs = data.tabs.map(t => ({ id: t.id, name: t.name || '未命名主题', running: false }));
            this.activeTabId = data.activeTabId || data.tabs[0].id;
          }
        } catch (e) {}
      }
      if (!this.tabs.length) {
        const oldWsId = localStorage.getItem('workspace_id') || 'default';
        this.tabs = [{ id: oldWsId, name: this.workspaceName || '未命名主题', running: false }];
        this.activeTabId = oldWsId;
      }
      window._activeWorkspaceId = this.activeTabId;

      this.loadPreset();
      try { await this.loadJobs(); } catch (e) { console.warn('loadJobs failed:', e); }
      setInterval(() => { this.runtimeTick = (this.runtimeTick + 1) % 1e9; }, 1000);

      // Auto-save workspace on any form change
      const form = document.getElementById('sd-form');
      if (form) {
        form.addEventListener('input', () => this.scheduleWorkspaceSave());
        form.addEventListener('change', () => this.scheduleWorkspaceSave());
      }

      // Download links: use blob download to avoid iframe navigation timeout.
      // Native <a download> triggers browser navigation which can time out
      // waiting for the proxy to buffer the entire video file.
      const dlContainer = document.getElementById('sd-results');
      if (dlContainer) {
        dlContainer.addEventListener('click', (e) => {
          const btn = e.target.closest('.dl-btn');
          if (btn) {
            e.preventDefault();
            const u = btn.dataset.url;
            const fn = btn.dataset.filename || 'video';
            if (u) this._blobDownload(u, fn);
            return;
          }
          // Click-to-play: replace the lazy placeholder with a real <video>.
          // Only fires ONE SSL fetch to the portal proxy at a time (per click)
          // — avoids the 21-way concurrent-fetch ERR_TOO_MANY_RETRIES storm.
          const lazy = e.target.closest('.video-lazy');
          if (lazy && lazy.dataset.src) {
            const url = lazy.dataset.src;
            const vid = document.createElement('video');
            vid.controls = true; vid.muted = true; vid.autoplay = true;
            vid.playsInline = true;
            vid.style.maxHeight = '200px';
            vid.style.width = '100%';
            vid.style.borderRadius = '6px';
            vid.src = url;
            lazy.replaceWith(vid);
          }
        });
      }

      // Global 5s tick: refresh jobs list so every tab's green-dot indicator
      // stays fresh, not just the tab that submitted. Skip while the page is
      // hidden to avoid burning cycles when the tab is in the background.
      this._loadJobsTimer = setInterval(() => {
        if (document.visibilityState !== 'hidden') this.loadJobs();
      }, 5000);
    },

    // ============================================================
    // PROVIDER SYSTEM
    // ============================================================
    async loadConfig() {
      const res = await api(APP_PATH + '/api/config');
      if (!res) {
        // Use fallback providers
        this.providers = FALLBACK_PROVIDERS;
        this.applyProvider('volcengine');
        return;
      }

      if (res.config_error) {
        this.providerHint = '供应商配置读取失败：' + (res.config_error.detail || res.config_error.message || '未知错误');
        return;
      }

      const normalized = providersFromConfig(res.providers);
      if (normalized) {
        this.providers = normalized;
        // Provider locked to volcengine — ignore default_provider from config
        // and any localStorage residue. Frontend has no provider switch anyway.
        this.applyProvider('volcengine');
      } else {
        this.providers = FALLBACK_PROVIDERS;
        this.applyProvider('volcengine');
      }
    },

    applyProvider(providerKey, skipDefaults) {
      const cfg = this.providers[providerKey];
      if (!cfg) return;
      this.provider = providerKey;
      this.baseUrl = cfg.base_url || '';
      this.providerHint = cfg.hint || '';
      this.models = cfg.models || [];

      // When restoring a saved draft/preset the form already carries this tab's
      // own resolution / ratio / duration. Re-applying provider defaults here
      // (async, after applyPreset filled the fields) would clobber them back to
      // defaults on every tab switch. skipDefaults keeps provider metadata
      // without overwriting the restored form values.
      if (skipDefaults) return;

      // Apply provider defaults after a tick to let DOM render
      setTimeout(() => {
        const defaults = cfg.defaults || {};
        for (const [k, v] of Object.entries(defaults)) {
          const el = field(k);
          if (!el || el.type === 'file') continue;
          if (el.type === 'checkbox') {
            el.checked = !!v;
          } else if (el.tagName === 'SELECT') {
            if ([...el.options].some(o => o.value === String(v))) el.value = v;
          } else {
            el.value = v;
          }
        }
        // API Key UI removed — provider is locked to volcengine and the key
        // lives in seedance/state/secrets.json on the server.
      });
    },

    // ============================================================
    // ARCHIVES
    // ============================================================
    async loadArchives() {
      const res = await api(APP_PATH + '/api/archives');
      if (res) this.archives = res.archives || [];
      if (this.selectedArchive && !this.archives.some(a => a.name === this.selectedArchive)) {
        this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
      }
    },

    async saveArchive() {
      this.archiveHint = '保存中...';
      const data = new FormData(document.getElementById('sd-form'));
      if (Object.keys(this.savedMedia).length) {
        data.set('saved_media', JSON.stringify(this.savedMedia));
      }
      const res = await api(APP_PATH + '/api/preset', 'POST', data);
      if (res) {
        this.archiveHint = res.archive ? '已保存：' + res.archive : (res.error || '保存失败');
        if (res.media) this.savedMedia = res.media;
        await this.loadArchives();
        this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
      } else {
        this.archiveHint = '保存失败';
      }
    },

    async loadArchive() {
      if (!this.selectedArchive) {
        this.archiveHint = '请选择一个存档';
        return;
      }
      const name = this.selectedArchive;
      if (!this.archives.some(a => a.name === name)) {
        this.archiveHint = '读取失败：存档「' + name + '」已被删除，请重新选择';
        this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
        return;
      }
      const data = new FormData();
      data.set('archive_name', name);
      const res = await api(APP_PATH + '/api/archive/load', 'POST', data);
      if (!res) {
        this.archiveHint = '读取失败';
        return;
      }
      this.applyPreset(res);
      const archiveInput = field('archive_name');
      if (archiveInput) archiveInput.value = name;
      this.archiveHint = '已读取存档：' + name;
    },

    async deleteArchive() {
      if (!this.selectedArchive) {
        this.archiveHint = '请选择一个存档';
        return;
      }
      const name = this.selectedArchive;
      if (!confirm('确定删除存档「' + name + '」？此操作不可恢复。')) return;
      const data = new FormData();
      data.set('archive_name', name);
      const res = await api(APP_PATH + '/api/archive/delete', 'POST', data);
      if (!res || res.ok === false) {
        this.archiveHint = '删除失败：' + (res && res.error ? res.error : '存档可能已被删除或不存在');
        return;
      }
      this.selectedArchive = '';
      await this.loadArchives();
      this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
      this.archiveHint = '已删除：' + name;
    },

    // Prompt Optimizer
    async optimizePrompt() {
      const promptEl = document.querySelector('textarea[name="prompt"][form="sd-form"]');
      const prompt = promptEl ? promptEl.value.trim() : '';
      if (!prompt) {
        this.optimizeError = '请先输入提示词';
        return;
      }
      this.optimizing = true;
      this.optimizedPrompt = '';
      this.optimizeError = '';
      try {
        const res = await api(APP_PATH + '/api/optimize-prompt', 'POST', JSON.stringify({ prompt: prompt }));
        if (!res) {
          this.optimizeError = '优化失败：网络异常，请检查服务是否运行';
        } else if (!res.ok) {
          this.optimizeError = '优化失败：' + (res.error || '未知错误');
        } else {
          this.optimizedPrompt = res.optimized;
        }
      } catch (e) {
        this.optimizeError = '优化失败：' + (e.message || '未知异常');
      }
      this.optimizing = false;
    },
    replacePrompt() {
      const promptEl = document.querySelector('textarea[name="prompt"][form="sd-form"]');
      if (promptEl && this.optimizedPrompt) {
        var text = this.optimizedPrompt;
        // Strategy 1: <PROMPT> XML tag (preferred)
        var m = text.match(/<PROMPT>\s*([\s\S]*?)\s*<\/PROMPT>/);
        // Strategy 2: <OPTIMIZED_PROMPT> XML tag (legacy)
        if (!m) m = text.match(/<OPTIMIZED_PROMPT>\s*([\s\S]*?)\s*<\/OPTIMIZED_PROMPT>/);
        // Strategy 3: Markdown ## 优化后提示词 (fallback)
        if (!m) m = text.match(/##\s*优化后提示词[^\n]*\n+([\s\S]*?)(?=\n+##\s|\n+---\s|\n*$)/);
        promptEl.value = (m ? m[1].trim() : text);
      }
      this.optimizedPrompt = '';
      this.optimizeError = '';
    },
    cancelOptimize() {
      this.optimizedPrompt = '';
      this.optimizeError = '';
    },

    async clearPreset() {
      const res = await api(APP_PATH + '/api/preset/clear', 'POST');
      if (!res) return;
      this.savedMedia = {};
      clearAllMediaPreviews();
      this.archiveHint = '已清空保存配置';
    },

    // ============================================================
    // ACTIVITY
    // ============================================================
    async loadActivity() {
      const res = await api(APP_PATH + '/api/activity');
      if (res) {
        this.activityRecords = res.records || [];
        this.activityCounts = res.counts || null;
      }
      this.activityDetail = null;
    },

    async loadJobs() {
      const res = await api(APP_PATH + '/api/jobs');
      if (res?.jobs) {
        this.jobs = res.jobs;
      } else {
        console.warn('[Seedance] loadJobs 返回异常:', res);
      }
      // Recompute per-tab running flag off the freshly loaded jobs. Drives the
      // green-dot indicator on every tab, not just the one that submitted.
      if (this.tabs && this.tabs.length) {
        this.tabs.forEach(t => {
          t.running = (this.jobs || []).some(j =>
            !TERMINAL_STATUSES.has((j.status || '').toLowerCase()) && j.workspace_id === t.id
          );
        });
      }
    },

    formatRuntime(job) {
      const _ = this.runtimeTick;
      const start = job.started_at || job.submitted_at;
      if (!start) return '';
      const status = (job.status || '').toLowerCase();
      const running = ['queued', 'pending', 'running', 'querying'].includes(status);
      if (running) {
        const sec = Math.max(0, Math.floor(Date.now() / 1000 - start));
        return '已运行 ' + (sec >= 60 ? Math.floor(sec / 60) + '分' + (sec % 60) + '秒' : sec + '秒');
      }
      if (job.finished_at && job.started_at) {
        const sec = Math.max(0, Math.floor(job.finished_at - job.started_at));
        return '耗时 ' + (sec >= 60 ? Math.floor(sec / 60) + '分' + (sec % 60) + '秒' : sec + '秒');
      }
      return '';
    },

    async showDetail(id) {
      const res = await api(APP_PATH + '/api/activity/' + id);
      if (res) this.activityDetail = res;
    },

    restoreActivity() {
      const r = this.activityDetail?.restore;
      if (!r) { alert('该记录无法恢复'); return; }
      this.applyPreset(r);
      if (r.warning) this.archiveHint = r.warning;
      else this.archiveHint = '已从后台记录恢复参数和素材';
      this.wsTab = 'jobs';
    },

    switchJobsTab() {
      this.wsTab = 'jobs';
      this.loadJobs();
    },

    switchActivityTab() {
      this.wsTab = 'activity';
      this.loadActivity();
    },

    // ============================================================
    // OUTPUT DIRECTORY
    // ============================================================
    async chooseOutputDir() {
      // Backend native directory picker
      const res = await api(APP_PATH + '/api/choose-output-dir', 'POST');
      if (res?.path) {
        this.outputDir = res.path;
        this.dirHandle = null;
        return;
      }
      // Browser File System Access API
      if (window.showDirectoryPicker) {
        try {
          this.dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
          this.outputDir = this.dirHandle.name;
          this.statusText = '已选择: ' + this.outputDir;
          return;
        } catch (e) { /* user cancelled */ }
      }
      // Fallback to browser download
      this.autoDownload = true;
      this.outputDir = '浏览器下载';
      if (res?.remote && !window.isSecureContext) {
        this.statusText = '提示：HTTPS 访问可启用目录选择功能';
      }
    },

    async desktopOutput() {
      const res = await api(APP_PATH + '/api/default-output-dir');
      if (res?.path) this.outputDir = res.path;
    },

    async openOutputDir() {
      if (this.dirHandle && !this.outputDir.includes('/')) {
        this.statusText = '文件将保存到 "' + this.outputDir + '"（浏览器限制无法代为打开）';
        return;
      }
      const data = new FormData();
      data.set('output_dir', this.outputDir);
      const res = await api(APP_PATH + '/api/open-output-dir', 'POST', data);
      if (res?.remote) this.statusText = '远程客户端不支持打开服务端目录';
    },

    async cleanCache() {
      const res = await api(APP_PATH + '/api/cleanup-cache', 'POST');
      if (res) {
        alert('清理完成：素材 ' + (res.media_deleted || 0) + ' 个，日志 ' + (res.logs_deleted || 0) + ' 个');
      }
    },

    // ============================================================
    // FORM SUBMISSION
    // ============================================================
    async submit() {
      if (this.submitting) return;
      this.submitting = true;
      this.statusText = '提交中';
      const resultsEl = document.getElementById('sd-results');
      const eventsEl = document.getElementById('sd-events');
      if (resultsEl) resultsEl.innerHTML = '';
      if (eventsEl) eventsEl.textContent = '';
      this.eventsText = '';

      const data = new FormData(document.getElementById('sd-form'));
      if (Object.keys(this.savedMedia).length) {
        data.set('saved_media', JSON.stringify(this.savedMedia));
      }

      let res;
      try {
        res = await api(APP_PATH + '/api/jobs', 'POST', data);
      } finally {
        this.submitting = false;
      }
      if (!res || res.error) {
        this.statusText = res?.error || '提交失败';
        return;
      }
      this.statusText = '已提交，任务在后台运行';
      this.loadJobs();
      this.pollJob(res.job_id);
    },

    async pollJob(jobId) {
      // Record which tab initiated this poll. If the user switches tabs while the
      // job is still running, subsequent state writes must NOT contaminate the
      // now-active tab — they route into _tabStateCache[startWsId] instead.
      const startWsId = this.activeTabId;
      const isActive = () => this.activeTabId === startWsId;
      const cache = () => (this._tabStateCache[startWsId] = this._tabStateCache[startWsId] || {});

      const setStatus = (t) => { if (isActive()) this.statusText = t; else cache().statusText = t; };
      const setEvents = (t) => { if (isActive()) this.eventsText = t; else cache().eventsText = t; };
      const setSubmitting = (v) => { if (isActive()) this.submitting = v; else cache().submitting = v; };
      const setLatestJob = (job) => { cache()._latestJob = job; };

      // Transient-failure tolerance. A single failed poll (proxy timeout, wifi
      // blip, sub-app 5xx) used to `break` and permanently abandon the watcher,
      // leaving a finished result invisible until manual resubmit. Now we retry
      // with backoff and only give up after MAX_FAILS consecutive failures
      // (~2min at the 10s backoff cap). A 404 ('gone' — sub-app restarted and
      // cleared JOBS) exits cleanly instead of looping forever on "unknown".
      const MAX_FAILS = 15;
      let consecutiveFails = 0;

      while (true) {
        const r = await pollJobOnce(APP_PATH + '/api/jobs/' + jobId);
        if (r.kind === 'gone') {
          // Job no longer exists server-side (restart). Refresh the jobs list so
          // any completed result recorded in activity can still surface there.
          setStatus('任务已失效(服务可能重启过),请查看活动记录或重新提交');
          break;
        }
        if (r.kind === 'error') {
          consecutiveFails++;
          if (consecutiveFails >= MAX_FAILS) {
            setStatus('网络不稳定,已停止刷新 · 稍后可重新提交');
            break;
          }
          // Exponential backoff capped at 10s, starting from the 2.5s cadence.
          const wait = Math.min(10000, 2500 * Math.pow(1.5, consecutiveFails - 1));
          await new Promise(res => setTimeout(res, wait));
          continue;
        }
        consecutiveFails = 0;
        const job = r.job;
        setStatus((job.status || 'unknown') + ' ' + (job.done || 0) + '/' + (job.total || 0));
        setEvents((job.events || []).map(e => '[' + (e.time || '') + '] ' + (e.message || '')).join('\n'));
        setLatestJob(job);

        if (isActive()) {
          this._renderJobToDom(job);
        }

        if (TERMINAL_STATUSES.has((job.status || '').toLowerCase())) {
          // Preserved terminal-status behavior from original pollJob:
          //   - job.status === 'succeeded' + dirHandle → saveToClient
          //   - job.status === 'succeeded' + autoDownload → triggerDownloads
          // Note: saveToClient/triggerDownloads still key on the literal 'succeeded'
          // (not the whole TERMINAL_STATUSES set) so failed/cancelled jobs never
          // trigger downloads. dirHandle/autoDownload are tab-scoped via
          // saveCurrentTabState/loadTargetTabState, but here we intentionally read
          // this.dirHandle/this.autoDownload at terminal time — matching the
          // original behavior of using whatever is live now (side-effects still
          // fire even if the user has since switched tabs).
          if (job.status === 'succeeded' && this.dirHandle) {
            await this.saveToClient(job);
          } else if (job.status === 'succeeded' && this.autoDownload) {
            this.triggerDownloads(job);
          }
          setSubmitting(false);
          setStatus('空闲');
          break;
        }
        await new Promise(r => setTimeout(r, 2500));
      }
      // Clear status + submitting on ALL exit paths (terminal AND null-break).
      // The terminal branch above already sets these for clarity, but this
      // catches the `if (!job) break;` early exit that otherwise leaves stale
      // progress text and a locked submit button.
      setStatus('空闲');
      setSubmitting(false);
      // Original pollJob always refreshed the jobs list on exit — keep that.
      this.loadJobs();
    },

    // Extracted from pollJob so that both live polling (from pollJob) and
    // tab-switch rehydration (from loadTargetTabState) can rebuild the DOM
    // from a job snapshot. Structure must match the original pollJob output
    // verbatim so downstream click handlers (._blobDownload via .dl-btn) still
    // work.
    _renderJobToDom(job) {
      const resultsEl = document.getElementById('sd-results');
      const eventsEl = document.getElementById('sd-events');
      if (!resultsEl && !eventsEl) return;

      if (eventsEl) {
        eventsEl.textContent = (job.events || []).map(e => '[' + (e.time || '') + '] ' + (e.message || '')).join('\n');
      }

      if (resultsEl) {
        const recentEvents = (job.events || []).slice(-8);
        const eventsHtml = recentEvents.length
          ? recentEvents.map(e =>
              '<div style="font-size:11px;color:#d1e0ff;padding:2px 0">'
              + '<span style="color:#697386">' + escHtml(e.time) + '</span> '
              + escHtml(e.message)
              + '</div>'
            ).join('')
          : '<div style="color:#697386;font-size:11px">等待服务器响应...</div>';
        resultsEl.innerHTML =
          '<article class="result" style="border-color:#4f46e5;background:#101828;color:#e2e8f0;grid-column:1/-1">'
          + '<div class="meta" style="color:#818cf8;font-weight:600;margin-bottom:6px">'
          + escHtml(job.status) + ' · ' + (job.done || 0) + '/' + (job.total || 0)
          + (job.errors?.[0] ? ' ' + escHtml(job.errors[0]) : '')
          + '</div>'
          + eventsHtml
          + '</article>';

        for (const r of job.results || []) {
          const url = APP_PATH + (r.download_url || '');
          // Lazy: render a click-to-play placeholder instead of <video preload="metadata">.
          // 20+ videos in one job otherwise fire 20+ concurrent SSL fetches
          // through the portal proxy → Chrome's per-host cap and portal's
          // buffer-then-forward path combine into ERR_TOO_MANY_RETRIES.
          resultsEl.innerHTML +=
            '<article class="result">'
            + '<div class="video-lazy" data-src="' + url + '" tabindex="0" role="button" aria-label="播放视频"'
            + ' style="max-height:200px;min-height:120px;background:#0f172a;border:1px solid #334155;border-radius:6px;'
            + 'display:flex;align-items:center;justify-content:center;cursor:pointer;position:relative">'
            + '<div style="display:flex;flex-direction:column;align-items:center;gap:6px;color:#94a3b8">'
            + '<div style="width:40px;height:40px;border-radius:50%;background:#334155;display:flex;align-items:center;justify-content:center;font-size:18px;color:#e2e8f0">▶</div>'
            + '<div style="font-size:11px">点击加载视频</div>'
            + '</div></div>'
            + '<a href="' + url + '" class="dl-btn" data-url="' + url + '" data-filename="' + escHtml(r.filename || 'video') + '">下载</a>'
            + '<div class="meta">Run ' + (r.index || '') + ' · ' + (r.task_id || '') + '</div>'
            + '</article>';
        }

        for (const err of job.errors || []) {
          resultsEl.innerHTML += '<article class="result" style="color:#ef4444">' + escHtml(err) + '</article>';
        }
      }
    },

    async saveToClient(job) {
      try {
        const files = [];
        for (const r of job.results || []) {
          if (r.download_url) files.push({ url: APP_PATH + r.download_url, filename: r.filename || 'video' });
        }
        for (const { url, filename } of files) {
          const resp = await fetch(url);
          const blob = await resp.blob();
          const fh = await this.dirHandle.getFileHandle(filename, { create: true });
          const w = await fh.createWritable();
          await w.write(blob);
          await w.close();
        }
        if (files.length) this.statusText = '已保存 ' + files.length + ' 个文件到 ' + this.outputDir;
      } catch (e) {
        console.warn('saveToClient failed:', e);
      }
    },

    triggerDownloads(job) {
      const urls = [];
      for (const r of job.results || []) {
        if (r.download_url) urls.push({ url: APP_PATH + r.download_url, filename: r.filename || 'video' });
      }
      for (const { url, filename } of urls) {
        this._blobDownload(url, filename);
      }
      if (urls.length) this.statusText = '已下载 ' + urls.length + ' 个文件';
    },

    async _blobDownload(url, filename) {
      // fetch → blob → <a download> dodges the self-signed-cert trap (Chrome's
      // download manager re-validates out of page context and rejects our LAN
      // cert). Cost: whole file into memory, no native progress — so we stream
      // the response and render our own progress bar (window._dlProgress).
      const bar = window._dlProgress ? window._dlProgress.start(filename) : null;
      try {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const blob = bar ? await bar.readBlob(resp) : await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
        if (bar) bar.done();
      } catch (e) {
        if (bar) bar.fail();
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.target = '_blank';
        a.rel = 'noopener';
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      }
    },

    // ============================================================
    // UPLOAD SLOTS
    // ============================================================
    buildUploadSlots() {
      const ir = document.getElementById('sd-imageRefs');
      const vr = document.getElementById('sd-videoRefs');
      const ar = document.getElementById('sd-audioRefs');
      if (ir) {
        for (let i = 1; i <= 9; i++) makeDrop(ir, 'ref_image_' + i, '@ref_image' + i, 'image/*', 'sd-form');
      }
      if (vr) {
        for (let i = 1; i <= 3; i++) makeDrop(vr, 'ref_video_' + i, '参考视频 ' + i, 'video/*', 'sd-form');
      }
      if (ar) {
        for (let i = 1; i <= 3; i++) makeDrop(ar, 'ref_audio_' + i, '参考音频 ' + i, 'audio/*', 'sd-form');
      }
    },

    wireDrops() {
      setTimeout(() => {
        document.querySelectorAll('#sd-app .drop').forEach(drop => {
          const input = drop.querySelector('input[type="file"]');
          if (input && !input.dataset.wired) {
            wireFileDrop(drop, input, input.name);
          }
        });
      }, 0);
    },

    // ============================================================
    // APPLY PRESET (archive load / activity restore / config load)
    // ============================================================
    applyPreset(preset) {
      if (!preset) return;
      clearAllMediaPreviews();
      const values = preset.values || {};

      for (const [name, value] of Object.entries(values)) {
        // API key is server-managed; never restore from saved draft.
        if (name === 'api_key') continue;
        // Provider is locked to volcengine; ignore stale saved values.
        // skipDefaults: the draft's own resolution/ratio/duration were/are being
        // applied in this same loop; don't reset them to provider defaults.
        if (name === 'provider') {
          this.applyProvider('volcengine', true);
          continue;
        }
        if (name === 'base_url') { this.baseUrl = value; continue; }
        if (name === 'output_dir') { this.outputDir = value; continue; }
        if (name === 'custom_model') { this.customModel = value; continue; }
        if (name === 'web_search') { this.webSearch = ['1', 'true', 'yes', 'on'].includes(String(value).toLowerCase()); continue; }
        if (name === 'poll_interval') { this.pollInterval = Number(value) || 10; continue; }
        if (name === 'timeout') { this.timeout = Number(value) || 3600; continue; }
        if (name === 'workspace_name') { this.workspaceName = value || '默认主题'; continue; }

        // For other fields, set DOM directly
        const el = field(name);
        if (!el) continue;
        if (el.type === 'checkbox') {
          el.checked = ['1', 'true', 'yes', 'on'].includes(String(value).toLowerCase());
        } else if (el.type !== 'file') {
          el.value = value;
        }
      }

      // Handle saved media
      if (preset.media) {
        this.savedMedia = mediaSnapshot(preset.media);
        for (const [name, item] of Object.entries(preset.media)) {
          const el = field(name);
          const drop = el?.closest('.drop');
          if (drop && item.url) {
            const mediaUrl = item.url.startsWith('/api/') ? APP_PATH + item.url : item.url;
            showPreview(drop, name, mediaUrl, item.filename || '已上传');
          }
        }
      } else {
        this.savedMedia = {};
      }

      const mediaCount = Object.keys(this.savedMedia).length;
      if (mediaCount) this.archiveHint = '已读取保存配置：' + mediaCount + ' 个素材';
    },

    // ============================================================
    // WORKSPACE SYSTEM
    // ============================================================
    scheduleWorkspaceSave() {
      clearTimeout(this._workspaceSaveTimer);
      this._workspaceSaveTimer = setTimeout(() => this.saveWorkspaceDraft(), 500);
    },

    async saveWorkspaceDraft() {
      const payload = {
        name: this.workspaceName.trim() || '默认主题',
        values: collectFormValues(),
        media: mediaSnapshot(this.savedMedia),
        saved_at: Date.now(),
      };
      // Key must track activeTabId so each tab's draft stays isolated.
      // Using a fixed key computed at init caused all tabs to overwrite one another.
      const key = 'seedance.workspace.' + this.activeTabId;
      localStorage.setItem(key, JSON.stringify(payload));
      this.workspaceHint = '已保存草稿：' + payload.name;
      return payload;
    },

    loadWorkspaceDraft() {
      if (this._workspaceId !== 'default' && this._workspaceId) {
        this.workspaceName = '主题 ' + this._workspaceId.slice(0, 6);
        this.workspaceHint = '当前是独立主题页，可与其它主题并发提交';
      } else {
        this.workspaceName = '默认主题';
        this.workspaceHint = '默认主题会读取当前保存配置';
      }

      // Key must track activeTabId — see saveWorkspaceDraft.
      const key = 'seedance.workspace.' + this.activeTabId;
      const raw = localStorage.getItem(key);
      if (!raw) return false;
      try {
        const draft = JSON.parse(raw);
        if (draft.name) this.workspaceName = draft.name;
        this.applyPreset({ values: draft.values || {}, media: draft.media || {} });
        this.workspaceHint = '已读取主题草稿：' + this.workspaceName;
        return true;
      } catch (e) {
        return false;
      }
    },

    loadPreset() {
      // Try workspace draft first
      if (this.loadWorkspaceDraft()) return;

      // Fall back to API preset
      if (this._workspaceId === 'default' || !this._workspaceId) {
        // Will load after init via the async pattern
        this._loadApiPreset();
      }
    },

    async _loadApiPreset() {
      const res = await api(APP_PATH + '/api/preset');
      if (res) {
        this.applyPreset(res);
      }
    },

    isWorkspaceMode() {
      return this._workspaceId !== 'default' && !!this._workspaceId;
    },

    // ============================================================
    // TAB BAR METHODS (Task 2)
    // ============================================================
    saveTabsToLocalStorage() {
      localStorage.setItem('seedance.tabs', JSON.stringify({
        tabs: this.tabs.map(t => ({ id: t.id, name: t.name })),
        activeTabId: this.activeTabId,
      }));
    },

    newTab() {
      this.saveCurrentTabState();
      const id = 'ws-' + Date.now() + '-' + Math.random().toString(16).slice(2, 7);
      this.tabs.push({ id, name: '未命名主题', running: false });
      this.activeTabId = id;
      window._activeWorkspaceId = id;
      this.workspaceName = '';
      this.savedMedia = {};
      const form = document.querySelector('#sd-form');
      if (form) form.reset();
      // form.reset() clears file inputs' .files but not the preview <img>/<video>
      // that showPreview() manually injected into each .drop — mirror the cleanup
      // applyPreset() already does so the new tab starts truly blank.
      clearAllMediaPreviews();
      this.statusText = '空闲';
      this.eventsText = '';
      this.submitting = false;
      this.saveTabsToLocalStorage();
      setTimeout(() => this._scrollActiveTabIntoView(), 0);
    },

    switchTab(id) {
      if (id === this.activeTabId || this.editingTabId) return;
      this.saveCurrentTabState();
      this.activeTabId = id;
      window._activeWorkspaceId = id;
      this.loadTargetTabState();
      this.saveTabsToLocalStorage();
      setTimeout(() => this._scrollActiveTabIntoView(), 0);
    },

    startEditTab(id) { this.editingTabId = id; },

    finishEditTab(id, name) {
      const trimmed = (name || '').trim() || '未命名主题';
      const tab = this.tabs.find(t => t.id === id);
      if (tab) {
        tab.name = trimmed;
        if (id === this.activeTabId) this.workspaceName = trimmed;
        if (typeof this.saveWorkspaceDraft === 'function') this.saveWorkspaceDraft();
        this.saveTabsToLocalStorage();
      }
      this.editingTabId = null;
    },

    closeTab(id) {
      const tab = this.tabs.find(t => t.id === id);
      if (!tab || this.tabs.length <= 1) return;
      if (tab.running) { this._closeConfirmTabId = id; return; }
      this._forceCloseTab(id);
    },

    _forceCloseTab(id) {
      const idx = this.tabs.findIndex(t => t.id === id);
      if (idx < 0 || this.tabs.length <= 1) return;
      this.tabs.splice(idx, 1);
      localStorage.removeItem('seedance.workspace.' + id);
      delete this._tabStateCache[id];
      if (this.activeTabId === id) {
        this.activeTabId = this.tabs[Math.max(0, idx - 1)].id;
        window._activeWorkspaceId = this.activeTabId;
        this.loadTargetTabState();
      }
      this.saveTabsToLocalStorage();
    },

    saveCurrentTabState() {
      const wsId = this.activeTabId;
      if (typeof this.saveWorkspaceDraft === 'function') this.saveWorkspaceDraft();
      // Preserve any fields already set by pollJob (notably _latestJob) so tab
      // switch → return still has a snapshot to re-render.
      this._tabStateCache[wsId] = {
        ...(this._tabStateCache[wsId] || {}),
        statusText: this.statusText,
        eventsText: this.eventsText,
        submitting: this.submitting,
        baseUrl: this.baseUrl,
        provider: this.provider,
        models: this.models ? JSON.parse(JSON.stringify(this.models)) : [],
        workspaceName: this.workspaceName,
      };
    },

    loadTargetTabState() {
      const wsId = this.activeTabId;
      const cache = this._tabStateCache[wsId] || {};
      this.statusText = cache.statusText || '空闲';
      this.eventsText = cache.eventsText || '';
      this.submitting = cache.submitting || false;
      if (cache.baseUrl !== undefined) this.baseUrl = cache.baseUrl;
      if (cache.provider !== undefined) this.provider = cache.provider;
      if (cache.models !== undefined) this.models = cache.models;
      if (cache.workspaceName !== undefined) this.workspaceName = cache.workspaceName;
      const form = document.querySelector('#sd-form');
      if (form) form.reset();
      this.savedMedia = {};
      if (typeof this.loadPreset === 'function') this.loadPreset();

      // If a background pollJob stashed a job snapshot for this tab, replay it
      // into the DOM. Otherwise clear any stale DOM left by the previous tab.
      if (cache._latestJob) {
        this._renderJobToDom(cache._latestJob);
      } else {
        const resultsEl = document.getElementById('sd-results');
        const eventsEl = document.getElementById('sd-events');
        if (resultsEl) resultsEl.innerHTML = '';
        if (eventsEl) eventsEl.textContent = '';
      }
    },

    _scrollActiveTabIntoView() {
      const el = document.querySelector('.app-tab.active');
      if (el && el.scrollIntoView) el.scrollIntoView({ inline: 'nearest', block: 'nearest' });
    },
  };
}

// ============================================================
// PREVIEW DIALOG — vanilla JS (outside PetiteVue scope)
// ============================================================
document.addEventListener('DOMContentLoaded', function () {
  var dlg = document.getElementById('previewDialog');
  var closeBtn = document.getElementById('closePreviewBtn');
  if (closeBtn) {
    closeBtn.addEventListener('click', function () { if (dlg) dlg.close(); });
  }
  if (dlg) {
    dlg.addEventListener('click', function (e) {
      if (e.target === dlg) dlg.close();
    });
  }
});

// SeedanceApp must be globally accessible for PetiteVue v-scope
window.SeedanceApp = SeedanceApp;

// Mount PetiteVue — initializes v-scope and @vue:mounted directives
PetiteVue.createApp({ SeedanceApp }).mount();

// === Download progress bar (shared, self-contained) ===================
// blob-download reads the whole file into browser memory with no native
// progress UI. This overlay reads the response as a stream and shows a
// bottom-of-screen bar ("已下载 42.0 / 180.0 MB") so users don't think it hung.
// Injects its own DOM+CSS on first use; concurrent downloads each get a row.
(function () {
  if (window._dlProgress) return;
  var MB = 1024 * 1024;
  var container = null;
  function ensureContainer() {
    if (container) return container;
    var style = document.createElement('style');
    style.textContent =
      '#_dlProgWrap{position:fixed;left:16px;bottom:16px;z-index:99999;display:flex;flex-direction:column;gap:8px;pointer-events:none}' +
      '#_dlProgWrap .dlp{background:#17191f;color:#e2e8f0;border-radius:8px;padding:10px 12px;min-width:240px;max-width:340px;box-shadow:0 4px 16px rgba(0,0,0,.35);font-size:12px;pointer-events:auto}' +
      '#_dlProgWrap .dlp .name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:6px}' +
      '#_dlProgWrap .dlp .track{height:6px;background:#2d3340;border-radius:3px;overflow:hidden}' +
      '#_dlProgWrap .dlp .fill{height:100%;width:0;background:#3b82f6;transition:width .15s ease}' +
      '#_dlProgWrap .dlp .txt{margin-top:5px;color:#94a3b8;font-size:11px}' +
      '#_dlProgWrap .dlp.done .fill{background:#22c55e}' +
      '#_dlProgWrap .dlp.fail .fill{background:#ef4444}';
    document.head.appendChild(style);
    container = document.createElement('div');
    container.id = '_dlProgWrap';
    document.body.appendChild(container);
    return container;
  }
  function fmt(bytes) { return (bytes / MB).toFixed(1); }
  window._dlProgress = {
    start: function (filename) {
      var wrap = ensureContainer();
      var row = document.createElement('div');
      row.className = 'dlp';
      row.innerHTML =
        '<div class="name">⬇ ' + (filename || '下载中') + '</div>' +
        '<div class="track"><div class="fill"></div></div>' +
        '<div class="txt">准备中…</div>';
      wrap.appendChild(row);
      var fill = row.querySelector('.fill');
      var txt = row.querySelector('.txt');
      var removed = false;
      function remove(delay) {
        if (removed) return; removed = true;
        setTimeout(function () { if (row.parentNode) row.parentNode.removeChild(row); }, delay);
      }
      return {
        readBlob: async function (resp) {
          var total = Number(resp.headers.get('Content-Length')) || 0;
          if (!resp.body || !resp.body.getReader) { txt.textContent = '下载中…'; return await resp.blob(); }
          var reader = resp.body.getReader();
          var chunks = [];
          var received = 0;
          for (;;) {
            var r = await reader.read();
            if (r.done) break;
            chunks.push(r.value);
            received += r.value.length;
            if (total) {
              var pct = Math.min(100, received / total * 100);
              fill.style.width = pct.toFixed(1) + '%';
              txt.textContent = '已下载 ' + fmt(received) + ' / ' + fmt(total) + ' MB (' + pct.toFixed(0) + '%)';
            } else {
              txt.textContent = '已下载 ' + fmt(received) + ' MB';
            }
          }
          return new Blob(chunks);
        },
        done: function () {
          row.classList.add('done');
          fill.style.width = '100%';
          txt.textContent = '完成';
          remove(1200);
        },
        fail: function () {
          row.classList.add('fail');
          txt.textContent = '下载出错，已尝试直接下载';
          remove(2500);
        },
      };
    },
  };
})();
