'use strict';

// ============================================================
// Module 1: Mode Detection
// ============================================================
const IN_PORTAL = window.location.pathname.startsWith('/nano-banana/');
const APP_PATH  = IN_PORTAL ? '/nano-banana' : '';

// ============================================================
// Module 2: Utilities
// ============================================================
function _workspaceId() {
  const params = new URLSearchParams(window.location.search);
  let id = params.get('ws');
  if (!id) {
    id = localStorage.getItem('workspace_id');
    if (!id) { id = crypto.randomUUID(); localStorage.setItem('workspace_id', id); }
  }
  return id;
}

async function api(url, method, body) {
  try {
    const opts = { method: method || 'GET', headers: { 'X-Workspace-Id': _workspaceId() } };
    if (body) opts.body = body;
    const res = await fetch(url, opts);
    return await res.json();
  } catch (e) { return null; }
}

function escHtml(s) { return s ? String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : ''; }

// ============================================================
// Module 3: Form Field Helper
// ============================================================
function nbField(name) {
  const form = document.getElementById('nb-form');
  return form?.elements[name] || document.querySelector(`[form="nb-form"][name="${name}"]`) || document.querySelector(`[name="${name}"]`);
}

function clearPreview(drop) {
  drop.classList.remove('hasPreview');
  drop.querySelector('.preview')?.remove();
  const span = drop.querySelector('span');
  if (span) span.textContent = '未上传';
}

function clearAllMediaInputs() {
  document.querySelectorAll('.drop input[type="file"]').forEach(function (input) {
    input.value = '';
    const drop = input.closest('.drop');
    if (drop) clearPreview(drop);
  });
}

// ============================================================
// Module 4: File Drop Helpers
// ============================================================
function wireFileDrop(drop, input) {
  input.addEventListener('change', function () {
    const f = input.files?.[0];
    if (!f) { clearPreview(drop); return; }
    showPreview(drop, input.name, URL.createObjectURL(f), f.name);
  });
  drop.addEventListener('dragover', function (e) { e.preventDefault(); drop.classList.add('isDragging'); });
  drop.addEventListener('dragleave', function () { drop.classList.remove('isDragging'); });
  drop.addEventListener('drop', function (e) {
    e.preventDefault(); drop.classList.remove('isDragging');
    const f = e.dataTransfer?.files?.[0]; if (!f) return;
    const dt = new DataTransfer(); dt.items.add(f); input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function makeDrop(container, name, label) {
  const el = document.createElement('label');
  el.className = 'drop';
  el.textContent = label;
  const input = document.createElement('input');
  input.name = name; input.type = 'file'; input.accept = 'image/*';
  input.setAttribute('form', 'nb-form');
  const span = document.createElement('span');
  span.textContent = '未上传';
  const rmBtn = document.createElement('button');
  rmBtn.className = 'removeMediaBtn'; rmBtn.type = 'button'; rmBtn.textContent = '移除';
  rmBtn.addEventListener('click', function (e) {
    e.preventDefault(); e.stopPropagation();
    input.value = '';
    delete window._currentSavedMedia?.[name];
    clearPreview(el);
  });
  el.append(input, span, rmBtn);
  wireFileDrop(el, input);
  container.appendChild(el);
}

function showPreview(drop, name, url, filename) {
  drop.classList.add('hasPreview');
  drop.querySelector('.preview')?.remove();
  const kind = name && (name.includes('video') ? 'video' : name.includes('audio') ? 'audio' : 'image');
  const tag = kind === 'image' ? 'img' : kind === 'video' ? 'video' : 'audio';
  const media = document.createElement(tag);
  media.className = 'preview'; media.src = url;
  if (kind !== 'image') media.controls = true;
  if (kind !== 'audio') media.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); openPreview(kind || 'image', url); });
  drop.insertBefore(media, drop.querySelector('span'));
  const span = drop.querySelector('span');
  if (span) span.textContent = filename || '已上传';
}

function openPreview(kind, url) {
  var dlg = document.getElementById('previewDialog');
  if (!dlg) return;
  var body = document.getElementById('previewDialogBody');
  if (!body) return;
  body.innerHTML = '';
  var m = document.createElement(kind === 'image' ? 'img' : 'video');
  m.src = url; if (kind === 'video') m.controls = true;
  body.append(m); dlg.showModal();
}

// ============================================================
// Module 5: Image Resize Pipeline
// ============================================================
function appendDisabledResizeValues(data) {
  for (var _i = 0, _arr = ['resize_width', 'resize_height', 'resize_interpolation', 'resize_method', 'resize_condition', 'resize_multiple_of']; _i < _arr.length; _i++) {
    var name = _arr[_i];
    var input = nbField(name);
    if (input) data.set(name, input.value);
  }
}

function targetResizeSize(fileWidth, fileHeight) {
  var wInput = nbField('resize_width');
  var hInput = nbField('resize_height');
  var width = Math.max(1, Number(wInput ? wInput.value : 0) || fileWidth);
  var height = Math.max(1, Number(hInput ? hInput.value : 0) || fileHeight);
  var mInput = nbField('resize_multiple_of');
  var multiple = Math.max(0, Number(mInput ? mInput.value : 0) || 0);
  if (multiple > 1) {
    width = Math.max(multiple, Math.round(width / multiple) * multiple);
    height = Math.max(multiple, Math.round(height / multiple) * multiple);
  }
  var cInput = nbField('resize_condition');
  var condition = cInput ? cInput.value : 'always';
  if (condition === 'only_downscale' && (width >= fileWidth || height >= fileHeight)) return null;
  if (condition === 'only_upscale' && (width <= fileWidth || height <= fileHeight)) return null;
  return { width: width, height: height };
}

async function resizeImageFile(file) {
  var reInput = nbField('resize_enabled');
  if (!reInput || !reInput.checked || !file.type.startsWith('image/')) return file;
  var bitmap = await createImageBitmap(file);
  var target = targetResizeSize(bitmap.width, bitmap.height);
  if (!target) { bitmap.close(); return file; }
  var canvas = document.createElement('canvas');
  canvas.width = target.width;
  canvas.height = target.height;
  var ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = true;
  var riInput = nbField('resize_interpolation');
  ctx.imageSmoothingQuality = (riInput ? riInput.value : 'high');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  var sx = 0, sy = 0, sw = bitmap.width, sh = bitmap.height;
  var dx = 0, dy = 0, dw = canvas.width, dh = canvas.height;
  var rmInput = nbField('resize_method');
  var method = rmInput ? rmInput.value : 'stretch';
  if (method === 'contain' || method === 'cover') {
    var imageRatio = bitmap.width / bitmap.height;
    var targetRatio = canvas.width / canvas.height;
    if (method === 'contain') {
      if (imageRatio > targetRatio) {
        dw = canvas.width;
        dh = Math.round(canvas.width / imageRatio);
      } else {
        dh = canvas.height;
        dw = Math.round(canvas.height * imageRatio);
      }
      dx = Math.round((canvas.width - dw) / 2);
      dy = Math.round((canvas.height - dh) / 2);
    } else {
      if (imageRatio > targetRatio) {
        sw = Math.round(bitmap.height * targetRatio);
        sx = Math.round((bitmap.width - sw) / 2);
      } else {
        sh = Math.round(bitmap.width / targetRatio);
        sy = Math.round((bitmap.height - sh) / 2);
      }
    }
  }
  ctx.drawImage(bitmap, sx, sy, sw, sh, dx, dy, dw, dh);
  var blob = await new Promise(function (resolve) { canvas.toBlob(resolve, 'image/png'); });
  bitmap.close();
  if (!blob) return file;
  var stem = file.name.replace(/\.[^.]+$/, '');
  return new File([blob], stem + '_resized.png', { type: 'image/png' });
}

async function imageUrlToFile(url, filename) {
  var res = await fetch(url);
  var blob = await res.blob();
  return new File([blob], filename || 'image.png', { type: blob.type || 'image/png' });
}

// ============================================================
// Module 6: Media URL Helper
// ============================================================
function resolveMediaUrl(url) {
  if (url && url.startsWith('/api/')) return APP_PATH + url;
  return url;
}

// ============================================================
// Module 7: Provider Models (fallback)
// ============================================================
var FALLBACK_PROVIDERS = {
  t8star: { label: 'T8Star Images API', base_url: 'https://ai.t8star.org', models: [{ id: 'nano-banana-2', label: 'nano-banana-2' }, { id: 'gemini-3.1-flash-image-preview', label: 'gemini-3.1-flash-image-preview' }] },
  gemini: { label: 'Chiyun', base_url: 'https://chiyun.work', models: [{ id: 'banana2-ssvip', label: 'banana2-ssvip' }, { id: 'nano-banana2[2K]-base', label: 'nano-banana2[2K]-base' }, { id: 'gpt-image-2', label: 'gpt-image-2' }] },
};

// ============================================================
// Module 8: NanoBananaApp Factory
// ============================================================
function NanoBananaApp() {
  return {
    // ---- 8a. State Properties ----

    isStandalone: !IN_PORTAL,
    appStatus: 'unknown',

    // Provider / API
    providers: {},
    provider: 't8star',
    models: [],
    baseUrl: 'https://ai.t8star.org',
    providerHint: '',
    keyHint: '',
    outputDir: '',
    dirHandle: null,
    autoDownload: false,

    // Submission
    submitting: false,
    statusText: '空闲',
    eventsText: '',

    // Archives
    archives: [],
    selectedArchive: '',
    archiveHint: '',

    // Saved media (reference images from archive)
    savedMedia: {},

    // Workspace tabs
    wsTab: 'jobs',

    // Activity
    activityRecords: [],
    activityCounts: null,
    activityDetail: null,

    // Workspace system (standalone)
    workspaceId: '',
    workspaceName: '',
    workspaceHint: '',

    // Resize toggle
    resizeEnabled: false,

    // ---- 8b. init() ----

    async init() {
      var self = this;
      window._app_nb = self;
      window._currentSavedMedia = self.savedMedia;

      // Workspace init
      self.workspaceId = _workspaceId();
      self.workspaceName = '默认主题';
      self.isStandalone = !IN_PORTAL;

      // Build upload slots (before config loads — synchronous DOM)
      self.buildUploadSlots();
      self.wireDrops();

      // Load server config
      try { await self.loadConfig(); } catch (e) { console.warn('loadConfig failed:', e); }

      // Legacy: also try raw /api/config (standalone path)
      if (!Object.keys(self.providers).length) {
        try {
          var fallbackRes = await fetch(APP_PATH + '/api/config');
          if (fallbackRes.ok) await self.loadConfigFromResponse(fallbackRes);
        } catch (e) { /* ignore */ }
      }

      // Fallback providers if all else fails
      if (!Object.keys(self.providers).length) {
        self.providers = FALLBACK_PROVIDERS;
        self.applyProvider(self.provider);
      }

      // Load archives
      try { self.loadArchives(); } catch (e) { console.warn('loadArchives failed:', e); }

      // Load workspace or server preset
      try { self.loadInitialPreset(); } catch (e) { console.warn('loadPreset failed:', e); }

      // Resize state initial sync
      self.updateResizeState();

      // Download links: use blob download to avoid iframe navigation timeout
      var dlContainer = document.getElementById('nb-results');
      if (dlContainer) {
        dlContainer.addEventListener('click', function (e) {
          var btn = e.target.closest('.dl-btn');
          if (!btn) return;
          e.preventDefault();
          var u = btn.dataset.url;
          var fn = btn.dataset.filename || 'image';
          if (u) self._blobDownload(u, fn);
        });
      }
    },

    // ---- 8c. loadConfig / applyProvider ----

    async loadConfig() {
      var res = await api(APP_PATH + '/api/config');
      if (!res || !res.providers) return;
      await this.loadConfigFromResponse({ ok: true, json: function () { return Promise.resolve(res); } });
    },

    async loadConfigFromResponse(response) {
      var data;
      try { data = await response.json(); } catch (e) { return; }
      if (!data || !data.providers) return;
      this.providers = data.providers;
      var defaultP = data.default_provider || Object.keys(data.providers)[0];
      var sel = document.querySelector('#nb-form select[name="provider"]');
      if (sel && sel.value !== defaultP && data.providers[sel.value]) {
        // Keep current provider if valid, else use default
        defaultP = sel.value;
      }
      this.applyProvider(defaultP);
      // Ensure select syncs
      var self = this;
      setTimeout(function () {
        var s = document.querySelector('#nb-form select[name="provider"]');
        if (s && s.value !== defaultP) s.value = defaultP;
        if (data.providers[defaultP]) self.applyProvider(defaultP);
      }, 0);
      this.keyHint = data.has_key ? '已检测到 key: ' + (data.masked_key || '') : '未检测到本地 key';
    },

    applyProvider(provider) {
      var cfg = this.providers[provider];
      if (!cfg) return;
      this.provider = provider;
      this.baseUrl = cfg.base_url || '';
      this.providerHint = cfg.hint || '';
      this.models = cfg.models || [];
      var self = this;
      setTimeout(function () {
        var defaults = cfg.defaults || {};
        for (var k in defaults) {
          if (!Object.prototype.hasOwnProperty.call(defaults, k)) continue;
          var v = defaults[k];
          var el = document.querySelector('#nb-form [name="' + k + '"]');
          if (!el || el.type === 'file') continue;
          if (el.type === 'checkbox') el.checked = !!v;
          else if (el.tagName === 'SELECT') {
            var opts = el.options;
            var found = false;
            for (var i = 0; i < opts.length; i++) { if (opts[i].value === String(v)) { found = true; break; } }
            if (found) el.value = v;
          } else el.value = v;
        }
        self.updateResizeState();
      });
    },

    // ---- 8d. buildUploadSlots / wireDrops ----

    buildUploadSlots() {
      var ir = document.getElementById('nb-imageRefs');
      if (ir) {
        ir.innerHTML = '';
        for (var i = 1; i <= 14; i++) {
          makeDrop(ir, 'image_' + i, 'Image ' + i);
        }
      }
    },

    wireDrops() {
      var self = this;
      setTimeout(function () {
        document.querySelectorAll('#nb-app .drop').forEach(function (drop) {
          var input = drop.querySelector('input[type="file"]');
          if (input && !input.dataset.wired) {
            input.dataset.wired = '1';
            // makeDrop already calls wireFileDrop for basic change/drag/drop wiring.
            // Add savedMedia cleanup on remove button.
            var rmBtn = drop.querySelector('.removeMediaBtn');
            if (rmBtn) {
              rmBtn.addEventListener('click', function (e) {
                e.preventDefault(); e.stopPropagation();
                input.value = '';
                delete self.savedMedia[input.name];
                clearPreview(drop);
              });
            }
          }
        });
      }, 0);
    },

    // ---- 8e. submit / pollJob / result display ----

    async submit() {
      var self = this;
      self.submitting = true;
      self.statusText = '提交中';
      var resultsEl = document.getElementById('nb-results');
      var eventsEl = document.getElementById('nb-events');
      if (resultsEl) resultsEl.innerHTML = '';
      if (eventsEl) eventsEl.textContent = '';

      // Auto-save workspace draft before submit
      if (self.isStandalone) {
        try { self.saveWorkspaceDraft(); } catch (e) { /* ignore */ }
      }

      var data = await self.formDataWithSavedMedia({ resizeImages: true });
      var res = await api(APP_PATH + '/api/jobs', 'POST', data);
      if (!res || res.error) {
        self.submitting = false;
        self.statusText = (res && res.error) || '提交失败';
        return;
      }
      await self.pollJob(res.job_id);
      self.submitting = false;
    },

    async pollJob(jobId) {
      var self = this;
      var resultsEl = document.getElementById('nb-results');
      while (true) {
        var job = await api(APP_PATH + '/api/jobs/' + jobId);
        if (!job) break;
        self.statusText = (job.status || '') + ' ' + (job.done || 0) + '/' + (job.total || 0);
        self.eventsText = (job.events || []).map(function (e) { return '[' + (e.time || '') + '] ' + (e.message || ''); }).join('\n');
        if (resultsEl) {
          var eventsList = (job.events || []).slice(-8).map(function (e) {
            return '<div style="font-size:11px;color:#d1e0ff;padding:2px 0"><span style="color:#697386">' + escHtml(e.time) + '</span> ' + escHtml(e.message) + '</div>';
          }).join('');
          resultsEl.innerHTML = '<article class="result" style="border-color:#4f46e5;background:#101828;color:#e2e8f0;grid-column:1/-1">' +
            '<div class="meta" style="color:#818cf8;font-weight:600;margin-bottom:6px">' + escHtml(job.status) + ' · ' + (job.done || 0) + '/' + (job.total || 0) + ' ' + escHtml((job.errors && job.errors[0]) || '') + '</div>' +
            (eventsList || '<div style="color:#697386;font-size:11px">等待服务器响应...</div>') +
            '</article>';
          for (var ri = 0; ri < (job.results || []).length; ri++) {
            var r = job.results[ri];
            for (var ii = 0; ii < (r.images || []).length; ii++) {
              var img = r.images[ii];
              var url = APP_PATH + img.download_url;
              var safeFn = escHtml(img.filename);
              resultsEl.innerHTML += '<article class="result"><img src="' + url + '" style="width:100%;max-height:180px;object-fit:contain;border-radius:6px;cursor:zoom-in" onclick="openPreview(\'image\',\'' + url + '\')"><a href="' + url + '" class="dl-btn" data-url="' + url + '" data-filename="' + safeFn + '">下载</a><div class="meta">Run ' + r.index + '</div></article>';
            }
          }
          for (var ei = 0; ei < (job.errors || []).length; ei++) {
            resultsEl.innerHTML += '<article class="result" style="color:#ef4444">' + escHtml(job.errors[ei]) + '</article>';
          }
        }
        if (job.status === 'succeeded' || job.status === 'failed') {
          if (job.status === 'succeeded' && self.dirHandle) {
            await self.saveToClient(job);
          } else if (job.status === 'succeeded' && self.autoDownload) {
            self.triggerDownloads(job);
          }
          break;
        }
        await new Promise(function (r) { setTimeout(r, 2500); });
      }
      self.statusText = '空闲';
      // Refresh activity list
      try { self.loadActivity(); } catch (e) { /* ignore */ }
    },

    // ---- 8f. saveToClient / triggerDownloads / _blobDownload ----

    async saveToClient(job) {
      try {
        var files = [];
        for (var ri = 0; ri < (job.results || []).length; ri++) {
          var r = job.results[ri];
          for (var ii = 0; ii < (r.images || []).length; ii++) {
            var img = r.images[ii];
            if (img.download_url) files.push({ url: APP_PATH + img.download_url, filename: img.filename });
          }
        }
        for (var fi = 0; fi < files.length; fi++) {
          var f = files[fi];
          var resp = await fetch(f.url);
          var blob = await resp.blob();
          var fh = await this.dirHandle.getFileHandle(f.filename, { create: true });
          var w = await fh.createWritable();
          await w.write(blob);
          await w.close();
        }
        if (files.length) this.statusText = '已保存 ' + files.length + ' 个文件到 ' + this.outputDir;
      } catch (e) {
        console.warn('saveToClient failed:', e);
      }
    },

    triggerDownloads(job) {
      var urls = [];
      for (var ri = 0; ri < (job.results || []).length; ri++) {
        var r = job.results[ri];
        for (var ii = 0; ii < (r.images || []).length; ii++) {
          var img = r.images[ii];
          if (img.download_url) urls.push({ url: APP_PATH + img.download_url, filename: img.filename });
        }
      }
      for (var ui = 0; ui < urls.length; ui++) {
        this._blobDownload(urls[ui].url, urls[ui].filename);
      }
      if (urls.length) this.statusText = '已下载 ' + urls.length + ' 个文件';
    },

    async _blobDownload(url, filename) {
      try {
        var resp = await fetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        var blob = await resp.blob();
        var blobUrl = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = blobUrl; a.download = filename; a.style.display = 'none';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(blobUrl); }, 1000);
      } catch (e) {
        console.warn('blob download failed:', filename, e);
        var a2 = document.createElement('a');
        a2.href = url; a2.download = filename; a2.target = '_blank';
        a2.style.display = 'none';
        document.body.appendChild(a2); a2.click(); document.body.removeChild(a2);
      }
    },

    // ---- 8g. Output directory methods ----

    async chooseOutputDir() {
      var res = await api(APP_PATH + '/api/choose-output-dir', 'POST');
      if (res && res.path) { this.outputDir = res.path; this.dirHandle = null; return; }
      if (window.showDirectoryPicker) {
        try {
          this.dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
          this.outputDir = this.dirHandle.name;
          this.statusText = '已选择: ' + this.outputDir;
          return;
        } catch (e) { /* user cancelled */ }
      }
      this.autoDownload = true;
      this.outputDir = '浏览器下载';
      if (res && res.remote && !window.isSecureContext) {
        this.statusText = '提示：HTTPS 访问可启用目录选择功能';
      }
    },

    async desktopOutput() {
      var res = await api(APP_PATH + '/api/default-output-dir');
      if (res && res.path) this.outputDir = res.path;
    },

    async openOutputDir() {
      if (this.dirHandle && !this.outputDir.includes('/')) {
        this.statusText = '文件将保存到 "' + this.outputDir + '"（浏览器限制无法代为打开）';
        return;
      }
      var data = new FormData(); data.set('output_dir', this.outputDir);
      var res = await api(APP_PATH + '/api/open-output-dir', 'POST', data);
      if (res && res.remote) this.statusText = '远程客户端不支持打开服务端目录';
    },

    async cleanCache() {
      var res = await api(APP_PATH + '/api/cleanup-cache', 'POST');
      if (res) alert('清理完成：素材 ' + (res.media_deleted || 0) + ' 个，日志 ' + (res.logs_deleted || 0) + ' 个');
    },

    // ---- 8h. Archives CRUD ----

    async loadArchives() {
      var res = await api(APP_PATH + '/api/archives');
      this.archives = (res && res.archives) || [];
      if (this.selectedArchive && !this.archives.some(function(a) { return a.name === this.selectedArchive; }, this)) {
        this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
      }
    },

    async saveArchive() {
      var data = await this.formDataWithSavedMedia({});
      if (this.savedMedia && Object.keys(this.savedMedia).length) {
        data.set('saved_media', JSON.stringify(this.savedMedia));
      }
      var res = await api(APP_PATH + '/api/preset', 'POST', data);
      this.archiveHint = (res && res.archive) ? '已保存: ' + res.archive : ((res && res.error) || '保存失败');
      if (res && res.media) this.savedMedia = res.media;
      window._currentSavedMedia = this.savedMedia;
      await this.loadArchives();
      this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
    },

    async loadArchive() {
      if (!this.selectedArchive) return;
      var name = this.selectedArchive;
      if (!this.archives.some(function(a) { return a.name === name; })) {
        this.archiveHint = '读取失败：存档「' + name + '」已被删除，请重新选择';
        this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
        return;
      }
      var data = new FormData(); data.set('archive_name', name);
      var res = await api(APP_PATH + '/api/archive/load', 'POST', data);
      if (!res) return;
      this.applyPreset(res);
      this.archiveHint = '已读取: ' + name;
    },

    async deleteArchive() {
      if (!this.selectedArchive) return;
      var name = this.selectedArchive;
      if (!confirm('确定删除存档「' + name + '」？此操作不可恢复。')) return;
      var data = new FormData(); data.set('archive_name', name);
      var res = await api(APP_PATH + '/api/archive/delete', 'POST', data);
      if (res && res.ok === false) {
        this.archiveHint = '删除失败：' + (res.error || '存档可能已被删除或不存在');
        return;
      }
      this.selectedArchive = '';
      await this.loadArchives();
      this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
      this.archiveHint = '已删除：' + name;
    },

    // ---- 8i. Activity methods ----

    async loadActivity() {
      var res = await api(APP_PATH + '/api/activity');
      this.activityRecords = (res && res.records) || [];
      this.activityCounts = (res && res.counts) || null;
      this.activityDetail = null;
    },

    async showDetail(id) {
      var res = await api(APP_PATH + '/api/activity/' + id);
      if (res) this.activityDetail = res;
    },

    restoreActivity() {
      var r = this.activityDetail && this.activityDetail.restore;
      if (!r) { alert('该记录无法恢复'); return; }
      this.applyPreset(r);
      if (r.values && r.values.provider && this.providers[r.values.provider]) {
        this.applyProvider(r.values.provider);
      }
      this.wsTab = 'jobs';
    },

    // ---- 8j. Preset / workspace methods ----

    applyPreset(preset) {
      clearAllMediaInputs();
      var values = (preset && preset.values) || {};
      for (var k in values) {
        if (!Object.prototype.hasOwnProperty.call(values, k)) continue;
        var v = values[k];
        var el = nbField(k);
        if (!el) continue;
        if (el.type === 'checkbox') {
          el.checked = ['1', 'true', 'yes', 'on'].includes(String(v).toLowerCase());
        } else if (el.type !== 'file') {
          el.value = v;
        }
      }
      // Sync reactive state for known v-model fields
      if (values.output_dir !== undefined) this.outputDir = values.output_dir;
      if (values.base_url !== undefined) this.baseUrl = values.base_url;
      if (values.workspace_name !== undefined) this.workspaceName = values.workspace_name;

      // Update provider if needed
      if (values.provider && this.providers[values.provider]) {
        this.applyProvider(values.provider);
      }

      // Update resize state
      this.updateResizeState();

      // Restore saved media
      var media = (preset && preset.media) || {};
      this.savedMedia = {};
      window._currentSavedMedia = this.savedMedia;
      for (var n in media) {
        if (!Object.prototype.hasOwnProperty.call(media, n)) continue;
        var item = media[n];
        this.savedMedia[n] = item;
        var inp = nbField(n);
        var drop = inp && inp.closest('.drop');
        if (drop && item.url) {
          showPreview(drop, n, resolveMediaUrl(item.url), item.filename);
        }
      }
      var count = Object.keys(this.savedMedia).length;
      if (count) this.archiveHint = '已读取保存配置：' + count + ' 张图';
    },

    async clearPreset() {
      var res = await api(APP_PATH + '/api/preset/clear', 'POST');
      if (!res) return;
      this.savedMedia = {};
      window._currentSavedMedia = this.savedMedia;
      document.querySelectorAll('.drop').forEach(function (d) { clearPreview(d); });
      this.archiveHint = '已清空当前读取配置';
    },

    async loadInitialPreset() {
      // In standalone mode, prefer workspace draft
      if (this.isStandalone && this.loadWorkspaceDraft()) return;

      // Otherwise load server preset
      var res = await fetch(APP_PATH + '/api/preset', { headers: { 'X-Workspace-Id': _workspaceId() } });
      if (res.ok) {
        var data = await res.json();
        this.applyPreset(data);
      }
    },

    // ---- Workspace System ----

    collectWorkspaceValues() {
      var form = document.getElementById('nb-form');
      if (!form) return {};
      var values = {};
      for (var i = 0; i < form.elements.length; i++) {
        var item = form.elements[i];
        if (!item.name || item.type === 'file') continue;
        values[item.name] = item.type === 'checkbox' ? (item.checked ? 'on' : '') : item.value;
      }
      return values;
    },

    mediaSnapshot(src) {
      src = src || this.savedMedia;
      return JSON.parse(JSON.stringify(src || {}));
    },

    localWorkspaceSnapshot() {
      return {
        name: this.workspaceName || '默认主题',
        values: this.collectWorkspaceValues(),
        media: this.mediaSnapshot(),
        saved_at: Date.now(),
      };
    },

    async saveWorkspaceDraft() {
      try {
        var payload = this.localWorkspaceSnapshot();
        var key = 'nano-banana.workspace.' + this.workspaceId;
        localStorage.setItem(key, JSON.stringify(payload));
        this.workspaceHint = '已保存草稿：' + (payload.name || '');
      } catch (e) {
        this.workspaceHint = '保存草稿失败';
      }
    },

    loadWorkspaceDraft() {
      var key = 'nano-banana.workspace.' + this.workspaceId;
      this.workspaceHint = '当前是独立主题页，可与其它主题并发提交';
      var raw = localStorage.getItem(key);
      if (!raw) return false;
      try {
        var draft = JSON.parse(raw);
        this.workspaceName = draft.name || this.workspaceName;
        this.applyPreset({ values: draft.values || {}, media: draft.media || {} });
        this.workspaceHint = '已读取主题草稿：' + (this.workspaceName || '');
        return true;
      } catch (e) {
        return false;
      }
    },

    newWorkspace() {
      var id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      var url = new URL(window.location.href);
      url.searchParams.set('ws', id);
      window.open(url.toString(), '_blank');
    },

    async duplicateWorkspace() {
      var id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      var current = this.localWorkspaceSnapshot();
      current.name = (this.workspaceName || '主题') + ' 副本';
      var key = 'nano-banana.workspace.' + id;
      localStorage.setItem(key, JSON.stringify(current));
      var url = new URL(window.location.href);
      url.searchParams.set('ws', id);
      window.open(url.toString(), '_blank');
    },

    // ---- 8k. Resize state / form data helpers ----

    updateResizeState() {
      var self = this;
      var reInput = nbField('resize_enabled');
      self.resizeEnabled = reInput ? reInput.checked : false;
      var controls = document.querySelector('.resizeControls');
      if (controls) {
        controls.classList.toggle('isDisabled', !self.resizeEnabled);
        controls.querySelectorAll('input, select').forEach(function (el) {
          el.disabled = !self.resizeEnabled;
        });
      }
    },

    async formDataWithSavedMedia(options) {
      options = options || {};
      var form = document.getElementById('nb-form');
      if (!form) return new FormData();
      var data = new FormData(form);
      appendDisabledResizeValues(data);
      var savedForBackend = {};
      for (var k in this.savedMedia) {
        if (Object.prototype.hasOwnProperty.call(this.savedMedia, k)) {
          savedForBackend[k] = this.savedMedia[k];
        }
      }
      if (options.resizeImages) {
        var reInput = nbField('resize_enabled');
        var resizeEnabled = reInput ? reInput.checked : false;
        if (resizeEnabled) {
          for (var i = 1; i <= 14; i++) {
            var name = 'image_' + i;
            var input = nbField(name);
            var file = (input && input.files && input.files[0]) || null;
            if (!file && savedForBackend[name]) {
              file = await imageUrlToFile(resolveMediaUrl(savedForBackend[name].url), savedForBackend[name].filename);
            }
            if (!file) continue;
            var resized = await resizeImageFile(file);
            data.set(name, resized, resized.name);
            delete savedForBackend[name];
          }
        }
      }
      data.set('saved_media', JSON.stringify(savedForBackend));
      return data;
    },

    // ---- 8l. Preview dialog ----

    closePreview() {
      var dlg = document.getElementById('previewDialog');
      if (dlg) dlg.close();
    },

    onPreviewDialogClick(e) {
      if (e.target === e.currentTarget) e.target.close();
    },
  };
}

// ============================================================
// Module 9: Mount PetiteVue
// ============================================================
window.NanoBananaApp = NanoBananaApp;
PetiteVue.createApp({ NanoBananaApp }).mount();

// ============================================================
// Module 10: DOMContentLoaded — additional wiring
// ============================================================
document.addEventListener('DOMContentLoaded', function () {
  // Close preview dialog on Escape key
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var dlg = document.getElementById('previewDialog');
      if (dlg && dlg.open) dlg.close();
    }
  });
});
