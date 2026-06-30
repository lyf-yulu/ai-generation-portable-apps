'use strict';

// === Utilities ===
function workspaceId() {
  let id = localStorage.getItem('workspace_id');
  if (!id) { id = crypto.randomUUID(); localStorage.setItem('workspace_id', id); }
  return id;
}

async function api(url, method, body) {
  try {
    const opts = { method: method || 'GET', headers: { 'X-Workspace-Id': workspaceId() } };
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
    archiveHint: '',
    accounts: [],
    activeAccount: null,
    dispatchMode: 'manual',
    accountLoginId: null,
    accountLoginUrl: null,
    isAdmin: false,
    historyLimit: 8,

    async init() {
      window._dmApp = this;
      window._dmRestore = (jobId) => this.restoreFromHistory(jobId);
      const me = await api('/api/auth/me');
      this.isAdmin = me?.role === 'admin';
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

    async renameAccount(accId) {
      const acc = this.accounts.find(a => a.id === accId);
      const name = prompt('输入新名称', acc?.name || '');
      if (!name || !name.trim()) return;
      const res = await api(`/dreamina/api/accounts/${accId}/rename`, 'POST', JSON.stringify({ name: name.trim() }));
      if (res?.ok) await this.loadAccounts();
    },

    async deleteAccount(accId) {
      if (!confirm('确认删除该账号？')) return;
      await api(`/dreamina/api/accounts/${accId}/delete`, 'POST');
      await this.loadAccounts();
    },

    async setActiveAccount(accId) {
      const res = await api('/dreamina/api/accounts/active', 'POST', JSON.stringify({ account_id: accId }));
      if (!res?.ok) { alert(res?.error || '切换账号失败'); this.loadAccounts(); return; }
      this.activeAccount = accId;
    },

    async setDispatchMode(mode) {
      const res = await api('/dreamina/api/dispatch-mode', 'POST', JSON.stringify({ mode }));
      if (!res?.ok) { alert(res?.error || '设置调度模式失败'); return; }
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
        this._blobDownload(url, filename);
      }
      this.statusText = `已下载 ${files.length} 个文件`;
    },

    async _blobDownload(url, filename) {
      try {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
      } catch (e) {
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

    renderFiles(files) {
      if (!files?.length) return '';
      return files.map(f => {
        const url = '/dreamina/' + f.replace(/^\//, '');
        const name = f.split('/').pop();
        const blobClick = `window._dmApp._blobDownload('${url}','${name}');return false`;
        if (/\.(mp4|mov|webm|avi)$/i.test(f)) return `<video controls src="${url}" style="width:100%;max-height:200px;border-radius:5px;margin-top:6px"></video><a href="${url}" download="${name}" onclick="${blobClick}">下载</a>`;
        return `<img src="${url}" style="width:100%;max-height:180px;object-fit:contain;border-radius:5px;margin-top:6px;cursor:zoom-in" onclick="openPreview('image','${url}')"><a href="${url}" download="${name}" onclick="${blobClick}">下载</a>`;
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
      const limit = this.historyLimit || 8;
      const visible = filtered.slice(0, limit);
      const cardsHtml = visible.map(item => {
        const files = item.result?.files || [];
        const thumb = files[0] ? '/dreamina/' + files[0].replace(/^\//, '') : '';
        const isVid = thumb && /\.(mp4|mov|webm)$/i.test(thumb);
        const prompt = item.params?.prompt || '';
        const status = item.status || '';
        const statusColor = status === 'completed' ? '#10b981' : status === 'failed' ? '#ef4444' : '#697386';
        let preview = '';
        if (thumb) {
          if (isVid) preview = `<video src="${thumb}" style="width:100%;max-height:120px;border-radius:5px;margin-top:4px" preload="none"></video>`;
          else preview = `<img src="${thumb}" loading="lazy" style="width:100%;max-height:120px;object-fit:contain;border-radius:5px;margin-top:4px;cursor:zoom-in" onclick="event.stopPropagation();openPreview('image','${thumb}')">`;
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
      const remaining = filtered.length - visible.length;
      const moreBtn = remaining > 0
        ? `<button type="button" style="width:100%;margin-top:8px;padding:8px;background:#1e293b;border:1px solid #334155;border-radius:6px;color:#e2e8f0;cursor:pointer;font-size:12px" onclick="window._dmApp.loadMoreHistory()">加载更多 (${remaining})</button>`
        : '';
      list.innerHTML = cardsHtml + moreBtn;
    },

    loadMoreHistory() {
      this.historyLimit = (this.historyLimit || 8) + 8;
      this.loadHistory();
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
      if (res?.path) { this.outputDir = res.path; this.dirHandle = null; return; }
      if (window.showDirectoryPicker) {
        try {
          this.dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
          this.outputDir = this.dirHandle.name;
          this.statusText = `已选择: ${this.outputDir}`;
          return;
        } catch (e) { /* user cancelled */ }
      }
      this.autoDownload = true;
      this.outputDir = '浏览器下载';
      if (res?.remote && !window.isSecureContext) {
        this.statusText = '提示：HTTPS 访问可启用目录选择功能';
      }
    },
    async desktopOutput() {
      const res = await api('/dreamina/api/default-output-dir');
      if (res?.path) this.outputDir = res.path;
    },
    async openOutputDir() {
      if (this.dirHandle && !this.outputDir.includes('/')) {
        this.statusText = `文件将保存到 "${this.outputDir}"（浏览器限制无法代为打开）`;
        return;
      }
      const data = new FormData(); data.set('output_dir', this.outputDir);
      const res = await api('/dreamina/api/open-output-dir', 'POST', data);
      if (res?.remote) this.statusText = '远程客户端不支持打开服务端目录';
    },
    async cleanCache() {
      const res = await api('/dreamina/api/cleanup-cache', 'POST');
      if (res) alert(`清理完成：素材 ${res.media_deleted || 0} 个，日志 ${res.logs_deleted || 0} 个`);
    },

    async loadArchives() {
      try {
        const res = await api('/dreamina/api/archives');
        this.archives = res?.archives || [];
        if (this.selectedArchive && !this.archives.some(a => a.name === this.selectedArchive)) {
          this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
        }
      } catch (e) {
        this.archiveHint = '加载存档列表失败：' + (e.message || '网络异常');
        console.error('[Dreamina] loadArchives error:', e);
      }
    },
    async saveArchive() {
      if (!this.archiveName) { this.archiveHint = '请输入存档名称'; return; }
      const name = this.archiveName;
      this.archiveHint = '保存中...';
      try {
        const data = new FormData(document.getElementById('dm-form'));
        data.set('archive_name', name);
        const res = await api('/dreamina/api/preset', 'POST', data);
        if (!res) {
          this.archiveHint = '保存失败：网络异常，请检查服务是否运行';
          return;
        }
        if (res.ok === false) {
          this.archiveHint = '保存失败：' + (res.error || '未知错误');
          return;
        }
        await this.loadArchives();
        this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
        this.archiveName = '';
        this.archiveHint = '已保存：' + name;
      } catch (e) {
        this.archiveHint = '保存失败：' + (e.message || '未知异常');
        console.error('[Dreamina] saveArchive error:', e);
      }
    },
    async loadArchive() {
      if (!this.selectedArchive) { this.archiveHint = '请先选择要读取的存档'; return; }
      const name = this.selectedArchive;
      if (!this.archives.some(a => a.name === name)) {
        this.archiveHint = '读取失败：存档「' + name + '」已被删除，请重新选择';
        this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
        return;
      }
      this.archiveHint = '读取中...';
      try {
        const data = new FormData(); data.set('archive_name', name);
        const res = await api('/dreamina/api/archive/load', 'POST', data);
        if (!res) {
          this.archiveHint = '读取失败：网络异常，请检查服务是否运行';
          return;
        }
        if (res.ok === false || !res.values) {
          this.archiveHint = '读取失败：' + (res.error || '存档不存在、已损坏或无可恢复数据');
          return;
        }
        let count = 0;
        for (const [k, v] of Object.entries(res.values)) {
          const el = document.querySelector(`#dm-form [name="${k}"]`);
          if (el && el.type !== 'file') { el.value = v; count++; }
        }
        this.archiveHint = '已读取：' + name + (count ? '（恢复 ' + count + ' 项参数）' : '');
      } catch (e) {
        this.archiveHint = '读取失败：' + (e.message || '未知异常');
        console.error('[Dreamina] loadArchive error:', e);
      }
    },
    async deleteArchive() {
      if (!this.selectedArchive) { this.archiveHint = '请先选择要删除的存档'; return; }
      const name = this.selectedArchive;
      if (!confirm('确定删除存档「' + name + '」？此操作不可恢复。')) return;
      this.archiveHint = '删除中...';
      try {
        const data = new FormData(); data.set('archive_name', name);
        const res = await api('/dreamina/api/archive/delete', 'POST', data);
        if (!res) {
          this.archiveHint = '删除失败：网络异常，请检查服务是否运行';
          return;
        }
        if (res.ok === false) {
          this.archiveHint = '删除失败：' + (res.error || '存档可能已被删除或不存在');
          return;
        }
        this.selectedArchive = '';
        await this.loadArchives();
        this.selectedArchive = this.archives.length > 0 ? this.archives[0].name : '';
        this.archiveHint = '已删除：' + name;
      } catch (e) {
        this.archiveHint = '删除失败：' + (e.message || '未知异常');
        console.error('[Dreamina] deleteArchive error:', e);
      }
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
    byUser: {},
    recentActivity: [],
    isAdmin: false,
    users: [],
    newUser: { username: '', password: '', role: 'user' },
    creating: false,
    userHint: '',
    userHintOk: true,
    signupEnabled: true,
    signupBusy: false,
    // History / day-picker
    selectedDate: '',          // YYYY-MM-DD; default today
    daySnapshot: null,         // { date, total_jobs, total_requests, by_app, by_user }
    historyDays: 7,            // 7 / 30 / 90
    history: { dates: [], users: {} },
    // Date-range query + CSV export
    rangeStart: '',            // YYYY-MM-DD
    rangeEnd: '',              // YYYY-MM-DD
    rangeData: { dates: [], users: {} },
    rangeLoading: false,
    rangeError: '',
    exporting: false,
    // Admin-only: company-wide volcengine-portrait key (no plaintext returned)
    portraitKey: {
      api_key: '', access_key: '', secret_key: '',
      has_api_key: false, has_access_key: false, has_secret_key: false,
      saving: false, hint: '', hintOk: true,
    },

    fmtDmStat(s) {
      if (!s) return '—';
      const parts = [];
      if (s.images) parts.push(s.images + '张');
      if (s.seconds) parts.push(s.seconds + 's');
      return parts.join(' / ') || '—';
    },

    async init() {
      const me = await api('/api/auth/me');
      this.isAdmin = me?.role === 'admin';
      // Default day-picker to today (local date string)
      const today = new Date();
      const tz = today.getTimezoneOffset() * 60000;
      this.selectedDate = new Date(today - tz).toISOString().slice(0, 10);
      // Default range: last 7 days ending today
      const weekAgo = new Date(today.getTime() - 6 * 86400000);
      this.rangeEnd = this.selectedDate;
      this.rangeStart = new Date(weekAgo - tz).toISOString().slice(0, 10);
      this.loadStats();
      this.loadActivity();
      this.loadPlatformStatus();
      this.loadDay();
      this.loadHistory();
      this.loadRange();
      if (this.isAdmin) {
        this.loadUsers();
        this.loadSignupStatus();
        this.loadPortraitKey();
      }
      setInterval(() => this.loadPlatformStatus(), 10000);
      setInterval(() => this.loadStats(), 30000);
    },

    async loadSignupStatus() {
      const r = await fetch('/api/auth/first-run');
      const d = await r.json().catch(() => null);
      if (d?.ok) this.signupEnabled = !!d.signup_enabled;
    },

    async toggleSignup() {
      this.signupBusy = true;
      const res = await api('/api/auth/signup-toggle', 'POST', JSON.stringify({ enabled: !this.signupEnabled }));
      this.signupBusy = false;
      if (res?.ok) this.signupEnabled = !!res.signup_enabled;
      else alert(res?.error || '切换失败');
    },

    async loadPortraitKey() {
      const res = await api('/api/platform/portrait-key');
      if (!res?.ok) return;
      this.portraitKey.has_api_key = !!res.has_api_key;
      this.portraitKey.has_access_key = !!res.has_access_key;
      this.portraitKey.has_secret_key = !!res.has_secret_key;
    },

    async savePortraitKey() {
      const payload = {};
      if (this.portraitKey.api_key.trim()) payload.api_key = this.portraitKey.api_key.trim();
      if (this.portraitKey.access_key.trim()) payload.access_key = this.portraitKey.access_key.trim();
      if (this.portraitKey.secret_key.trim()) payload.secret_key = this.portraitKey.secret_key.trim();
      if (!Object.keys(payload).length) {
        this.portraitKey.hint = '至少需要填写一项才能保存';
        this.portraitKey.hintOk = false;
        return;
      }
      this.portraitKey.saving = true;
      this.portraitKey.hint = '';
      const res = await api('/api/platform/portrait-key', 'POST', JSON.stringify(payload));
      this.portraitKey.saving = false;
      if (res?.ok) {
        this.portraitKey.api_key = '';
        this.portraitKey.access_key = '';
        this.portraitKey.secret_key = '';
        this.portraitKey.has_api_key = !!res.has_api_key;
        this.portraitKey.has_access_key = !!res.has_access_key;
        this.portraitKey.has_secret_key = !!res.has_secret_key;
        this.portraitKey.hint = '已保存,即时生效(无需重启)';
        this.portraitKey.hintOk = true;
      } else {
        this.portraitKey.hint = (res && res.error) ? '保存失败:' + res.error : '保存失败';
        this.portraitKey.hintOk = false;
      }
    },

    async loadPlatformStatus() {
      const res = await api('/api/platform/status');
      if (!res?.ok) return;
      document.getElementById('lanInfo').textContent = `LAN: ${location.protocol}//${res.lan_ip}:${res.portal_port}`;
      document.getElementById('barStats').textContent = `今日: ${this.todayJobs} jobs`;
    },

    async loadStats() {
      const res = await api('/api/platform/stats');
      if (!res?.ok) return;
      this.todayJobs = res.today_jobs || 0;
      this.todayRequests = res.today_requests || 0;
      this.byApp = res.by_app || {};
      this.byUser = res.by_user || {};
      document.getElementById('barStats').textContent = `今日: ${this.todayJobs} jobs`;
    },

    async loadActivity() {
      const res = await api('/api/platform/activity');
      if (!res?.ok) return;
      this.recentActivity = (res.activity || []).slice(0, 30);
    },

    async loadDay() {
      if (!this.selectedDate) return;
      const res = await api('/api/platform/stats/day?date=' + encodeURIComponent(this.selectedDate));
      if (!res?.ok) { this.daySnapshot = null; return; }
      this.daySnapshot = res;
    },

    async loadHistory() {
      const res = await api('/api/platform/stats/history?days=' + this.historyDays);
      if (!res?.ok) return;
      this.history = { dates: res.dates || [], users: res.users || {} };
    },

    async loadRange() {
      if (!this.rangeStart || !this.rangeEnd) return;
      this.rangeError = '';
      this.rangeLoading = true;
      const url = '/api/platform/stats/range'
        + '?start=' + encodeURIComponent(this.rangeStart)
        + '&end=' + encodeURIComponent(this.rangeEnd);
      const res = await api(url);
      this.rangeLoading = false;
      if (!res?.ok) {
        this.rangeError = res?.error || '加载失败';
        this.rangeData = { dates: [], users: {} };
        return;
      }
      this.rangeData = { dates: res.dates || [], users: res.users || {} };
    },

    async exportRange() {
      if (!this.rangeStart || !this.rangeEnd) return;
      this.exporting = true;
      try {
        const url = '/api/platform/stats/export'
          + '?start=' + encodeURIComponent(this.rangeStart)
          + '&end=' + encodeURIComponent(this.rangeEnd);
        // 自签 HTTPS 下走 fetch+Blob，避免下载管理器拒绝；anchor download 在生产路径有冲突。
        const r = await fetch(url, { credentials: 'same-origin' });
        if (!r.ok) {
          alert('导出失败 HTTP ' + r.status);
          return;
        }
        const blob = await r.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = `usage-${this.rangeStart}-${this.rangeEnd}.csv`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(objUrl), 1000);
      } catch (e) {
        alert('导出失败: ' + (e?.message || e));
      } finally {
        this.exporting = false;
      }
    },

    rangeUserTotals(user) {
      // Sum images + seconds across all apps in the current rangeData.
      const apps = this.rangeData?.users?.[user] || {};
      let images = 0, seconds = 0;
      for (const a in apps) {
        const s = apps[a];
        images += (s.images || []).reduce((x, y) => x + (y || 0), 0);
        seconds += (s.seconds || []).reduce((x, y) => x + (y || 0), 0);
      }
      return { images, seconds };
    },

    setHistoryDays(n) {
      this.historyDays = n;
      this.loadHistory();
    },

    // Pick the metric series most relevant to each subapp.
    // seedance / volcengine-portrait: seconds (video)
    // nano-banana: images
    // dreamina: images + seconds elementwise (mixed media)
    pickAppValues(stats, app) {
      if (!stats) return [];
      if (app === 'nano-banana') return stats.images || [];
      if (app === 'dreamina') {
        const a = stats.images || [];
        const b = stats.seconds || [];
        const n = Math.max(a.length, b.length);
        const out = [];
        for (let i = 0; i < n; i++) out.push((a[i] || 0) + (b[i] || 0));
        return out;
      }
      return stats.seconds || [];
    },

    appUnit(app) {
      if (app === 'nano-banana') return '张';
      if (app === 'dreamina') return '张+秒';
      return '秒';
    },

    appColor(app) {
      return {
        'seedance': '#2563eb',
        'nano-banana': '#10b981',
        'dreamina': '#a855f7',
        'volcengine-portrait': '#f59e0b',
      }[app] || '#64748b';
    },

    appLabel(app) {
      return {
        'seedance': 'Seedance',
        'nano-banana': 'Nano Banana',
        'dreamina': 'Dreamina',
        'volcengine-portrait': '人像生成',
      }[app] || app;
    },

    svgSpark(values, app) {
      const w = 160, h = 44;
      if (!values || !values.length) return '';
      const color = this.appColor(app);
      const max = Math.max(1, ...values);
      const last = values[values.length - 1];
      const step = w / Math.max(1, values.length - 1);
      const pts = values.map((v, i) =>
        (i * step).toFixed(1) + ',' +
        (h - (v / max) * (h - 8) - 4).toFixed(1)
      ).join(' ');
      const lastX = (w - 2).toFixed(1);
      const lastY = (h - (last / max) * (h - 8) - 4).toFixed(1);
      return (
        '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
          '<polyline fill="none" stroke="' + color + '" stroke-width="1.5" points="' + pts + '"/>' +
          '<circle cx="' + lastX + '" cy="' + lastY + '" r="2" fill="' + color + '"/>' +
        '</svg>'
      );
    },

    sumSeries(values) {
      if (!values || !values.length) return 0;
      return values.reduce((a, b) => a + (b || 0), 0);
    },

    async loadUsers() {
      const res = await api('/api/users');
      if (res?.ok) this.users = res.users || [];
    },

    async createUser() {
      if (!this.newUser.username || !this.newUser.password) return;
      if (this.newUser.password.length < 6) {
        this.userHint = '密码至少 6 位'; this.userHintOk = false; return;
      }
      this.creating = true; this.userHint = '';
      const res = await api('/api/auth/create-user', 'POST', JSON.stringify(this.newUser));
      this.creating = false;
      if (res?.ok) {
        this.userHint = `已创建：${this.newUser.username}`; this.userHintOk = true;
        this.newUser = { username: '', password: '', role: 'user' };
        await this.loadUsers();
      } else {
        this.userHint = res?.error || '创建失败'; this.userHintOk = false;
      }
    },

    async setRole(u, role) {
      if (role === u.role) return;
      const res = await api('/api/users/' + u.id, 'POST', JSON.stringify({ role }));
      if (res?.ok) await this.loadUsers();
    },

    async toggleEnabled(u) {
      const res = await api('/api/users/' + u.id, 'POST', JSON.stringify({ enabled: !u.enabled }));
      if (res?.ok) await this.loadUsers();
    },

    async resetPassword(u) {
      const pw = prompt(`为 ${u.username} 设置新密码（≥6位）：`);
      if (!pw) return;
      if (pw.length < 6) { alert('密码至少 6 位'); return; }
      const res = await api('/api/users/' + u.id, 'POST', JSON.stringify({ password: pw }));
      if (res?.ok) alert('密码已重置');
      else alert(res?.error || '重置失败');
    }
  };
}

// === Keys App ===
function KeysApp() {
  return {
    keys: [],
    form: { name: '', provider: 't8star', key: '', note: '' },
    saving: false,
    hint: '',
    hintOk: true,

    async init() {
      await this.loadKeys();
    },

    async loadKeys() {
      const res = await api('/api/keys');
      if (res?.ok) this.keys = res.keys || [];
    },

    async addKey() {
      if (!this.form.name || !this.form.key) return;
      this.saving = true; this.hint = '';
      const res = await api('/api/keys', 'POST', JSON.stringify(this.form));
      this.saving = false;
      if (res?.ok) {
        this.hint = '已添加：' + this.form.name; this.hintOk = true;
        this.form = { name: '', provider: 't8star', key: '', note: '' };
        await this.loadKeys();
      } else {
        this.hint = res?.error || '添加失败'; this.hintOk = false;
      }
    },

    async deleteKey(id) {
      if (!confirm('确定删除该密钥？')) return;
      const res = await api('/api/keys/' + id, 'DELETE');
      if (res?.ok) await this.loadKeys();
    },

    async copyKey(id, name) {
      const res = await api('/api/keys/' + id + '/reveal');
      if (!res?.ok || !res.key) {
        this.hint = '获取失败：' + (res?.error || '未知错误'); this.hintOk = false;
        return;
      }
      try {
        await navigator.clipboard.writeText(res.key);
        this.hint = '已复制：' + name; this.hintOk = true;
      } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = res.key;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        let ok = false;
        try { ok = document.execCommand('copy'); } catch (_) {}
        document.body.removeChild(ta);
        if (ok) { this.hint = '已复制：' + name; this.hintOk = true; }
        else { this.hint = '复制失败，浏览器拒绝访问剪贴板'; this.hintOk = false; }
      }
    }
  };
}

// === Volcengine Portrait App ===
function VolcenginePortraitApp() {
  const appPath = '/volcengine-portrait';

  function vpApi(url, method, body) {
    // No API key headers — volcengine-portrait uses a company-wide key configured
    // by admin via /api/platform/portrait-key, applied server-side at fallback time.
    const headers = { 'X-Workspace-Id': workspaceId() };
    if (typeof body === 'string') headers['Content-Type'] = 'application/json';
    const opts = { method: method || 'GET', headers };
    if (body) opts.body = body;
    const m = method || 'GET';
    const reqPreview = typeof body === 'string' ? body.slice(0, 500)
      : (body instanceof FormData ? '[FormData]' : '');
    return fetch(url, opts).then(r => {
      return r.json().then(data => {
        const respStr = JSON.stringify(data).slice(0, 500);
        if (this.addDebugLog) {
          this.addDebugLog(m, url, r.status, reqPreview, respStr);
        }
        if (!r.ok) {
          return { ok: false, error: data.error || ('HTTP ' + r.status), detail: respStr };
        }
        return data;
      });
    }).catch(e => {
      if (this.addDebugLog) {
        this.addDebugLog(m, url, 0, reqPreview, e.message);
      }
      return { ok: false, error: 'Network error', detail: e.message };
    });
  }

  return {
    statusText: '空闲',
    appPath,  // exposed to petite-vue templates (used in index.html for download urls)

    // Unified state (merges virtual + real)
    groupName: '', groupId: '', creatingGroup: false,
    groups: [],
    assetGroupId: '', selectedFile: '', uploading: false, uploadMsg: '', uploadError: false,
    renamingGroup: false, renameGroupName: '', renamingSaving: false,
    renamingAssetId: '', renameAssetName: '',
    assets: [],
    assetName: '',
    genAssetId: '', extraAssetIds: [], extraFiles: [],
    prompt: '', duration: 12, resolution: '720p', ratio: '16:9', repeat: 1,
    submitting: false, events: '', results: [], jobs: [],
    runtimeTick: 0,
    outputDir: '', outputDirInput: '', showOutputDirInput: false,
    savingOutputDir: false, outputDirMsg: '', outputDirOk: true,

    // Debug log
    debugLogs: [],
    debugVisible: false,
    addDebugLog(method, url, status, reqBody, respBody) {
      this.debugLogs.unshift({
        time: new Date().toLocaleTimeString(),
        method, url, status, reqBody, respBody
      });
      if (this.debugLogs.length > 100) this.debugLogs.pop();
    },
    clearDebugLogs() { this.debugLogs = []; },
    toggleDebug() { this.debugVisible = !this.debugVisible; },

    async init() {
      window._vpApp = this;
      this.loadGroups();
      this.loadJobs();
      this.loadOutputDir();
      setInterval(() => { this.runtimeTick = (this.runtimeTick + 1) % 1e9; }, 1000);
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

    // === Groups ===
    async createGroup() {
      this.creatingGroup = true;
      const body = {};
      if (this.groupName.trim()) body.name = this.groupName.trim();
      const res = await vpApi.call(this, `${appPath}/api/virtual/groups`, 'POST', JSON.stringify(body));
      if (res?.ok) {
        this.groupId = res.group_id;
        this.assetGroupId = res.group_id;
        this.uploadMsg = '组创建成功: ' + res.group_id;
        this.uploadError = false;
      } else {
        this.uploadMsg = (res?.error || '创建失败') + (res?.detail ? ' — ' + res.detail.slice(0, 120) : '');
        this.uploadError = true;
      }
      this.creatingGroup = false;
    },

    async loadGroups() {
      const res = await vpApi.call(this, `${appPath}/api/virtual/groups`);
      if (res?.ok) this.groups = res.groups || [];
    },

    async deleteGroup(id) {
      if (!id) return;
      // 先确认组内资产数 — 火山方舟不允许删非空组，前端阻止误删
      const probeUrl = `${appPath}/api/virtual/assets?group_ids=${encodeURIComponent(id)}&page_size=1`;
      const probe = await vpApi.call(this, probeUrl);
      if (!probe?.ok) {
        alert('无法确认组内资产数量，请刷新重试');
        return;
      }
      const total = (probe.total_count != null) ? probe.total_count : (probe.assets?.length || 0);
      if (total > 0) {
        alert(`该组下还有 ${total} 个资产，请先逐个删除`);
        return;
      }
      if (!confirm('确认删除组？')) return;
      const res = await vpApi.call(this, `${appPath}/api/virtual/groups/${id}`, 'DELETE');
      if (res?.ok || (res?.error && res.error !== 'Network error')) {
        this.assetGroupId = '';
        this.groupId = '';
        this.assets = [];
        this.loadGroups();
      }
    },

    onGroupChange() {
      if (this.assetGroupId) {
        this.loadAssets();
      } else {
        this.assets = [];
      }
    },

    groupNameFor(id) {
      const g = this.groups.find(x => x.group_id === id);
      return g ? (g.name || g.group_id) : (id || '');
    },
    startRenameGroup() {
      this.renameGroupName = this.groupNameFor(this.assetGroupId);
      this.renamingGroup = true;
    },
    async saveGroupRename() {
      const name = (this.renameGroupName || '').trim();
      if (!name || !this.assetGroupId) return;
      this.renamingSaving = true;
      const res = await vpApi.call(this, `${appPath}/api/virtual/groups/${this.assetGroupId}`, 'POST', JSON.stringify({ name }));
      if (res?.ok) {
        this.renamingGroup = false;
        this.loadGroups();
      } else {
        this.uploadMsg = (res?.error || '重命名失败') + (res?.detail ? ' — ' + res.detail.slice(0, 120) : '');
        this.uploadError = true;
      }
      this.renamingSaving = false;
    },
    startRenameAsset(a) {
      this.renamingAssetId = a.asset_id;
      this.renameAssetName = a.file_name || '';
    },
    async saveAssetRename(asset_id) {
      const name = (this.renameAssetName || '').trim();
      if (!name) return;
      const res = await vpApi.call(this, `${appPath}/api/virtual/assets/${asset_id}`,
                                    'POST', JSON.stringify({ name }));
      if (res?.ok) {
        this.renamingAssetId = '';
        this.loadAssets();
      } else {
        this.uploadMsg = '重命名失败：' + (res?.error || 'unknown') + (res?.detail ? ' — ' + res.detail.slice(0, 120) : '');
        this.uploadError = true;
      }
    },
    addExtraAsset(ev) {
      const aid = ev.target.value;
      if (aid && !this.extraAssetIds.includes(aid)) {
        this.extraAssetIds.push(aid);
      }
      ev.target.value = '';
    },
    removeExtraAsset(aid) {
      this.extraAssetIds = this.extraAssetIds.filter(x => x !== aid);
    },
    assetNameFor(aid) {
      const a = this.assets.find(x => x.asset_id === aid);
      return a ? (a.file_name || aid) : aid;
    },

    onFileSelect() {
      const f = document.getElementById('vp-file')?.files?.[0];
      this.selectedFile = f ? f.name : '';
      // 资产名预填：若 input 为空则用文件名（去扩展名）兜底，用户可改
      if (f && !this.assetName) {
        this.assetName = f.name.replace(/\.[^.]+$/, '');
      }
    },

    // === Assets ===
    async uploadAsset() {
      const el = document.getElementById('vp-file');
      const file = el?.files?.[0];
      if (!file) {
        this.uploadMsg = '请先选择要上传的文件';
        this.uploadError = true;
        return;
      }
      if (!this.assetGroupId) {
        this.uploadMsg = '请先选择或创建人像组';
        this.uploadError = true;
        return;
      }
      this.uploading = true; this.uploadMsg = '';
      const fd = new FormData();
      fd.append('group_id', this.assetGroupId);
      fd.append('file', file);
      const nameForUpload = (this.assetName || '').trim() || file.name.replace(/\.[^.]+$/, '');
      fd.append('name', nameForUpload);
      const res = await vpApi.call(this, `${appPath}/api/virtual/assets`, 'POST', fd);
      if (res?.ok) {
        this.uploadMsg = '资产创建成功: ' + res.asset_id;
        this.uploadError = false;
        this.assetName = '';
        this.selectedFile = '';
        const fileInput = document.getElementById('vp-file');
        if (fileInput) fileInput.value = '';
        this.loadAssets();
      }
      else { this.uploadMsg = (res?.error || '上传失败') + (res?.detail ? ' — ' + res.detail.slice(0, 120) : ''); this.uploadError = true; }
      this.uploading = false;
    },

    async loadAssets() {
      let url = `${appPath}/api/virtual/assets`;
      if (this.assetGroupId) {
        url += '?group_ids=' + encodeURIComponent(this.assetGroupId);
      }
      const res = await vpApi.call(this, url);
      if (res?.ok) this.assets = (res.assets || []).map(a => ({ ...a, asset_id: a.asset_id || a.id }));
    },

    async deleteAsset(id) {
      const res = await vpApi.call(this, `${appPath}/api/virtual/assets/${id}`, 'DELETE');
      if (res?.ok || (res?.error && res.error !== 'Network error')) this.loadAssets();
    },

    async loadOutputDir() {
      const res = await vpApi.call(this, `${appPath}/api/config`);
      if (res?.ok) {
        this.outputDir = res.output_dir || '';
        this.outputDirInput = this.outputDir;
      }
    },

    async setOutputDir() {
      const p = (this.outputDirInput || '').trim();
      if (!p) { this.outputDirMsg = '路径不能为空'; this.outputDirOk = false; return; }
      this.savingOutputDir = true; this.outputDirMsg = '';
      const res = await vpApi.call(this, `${appPath}/api/config`, 'POST', JSON.stringify({ output_dir: p }));
      if (res?.ok) {
        this.outputDir = res.output_dir || p;
        this.outputDirMsg = '保存位置已更新'; this.outputDirOk = true;
        this.showOutputDirInput = false;
      } else {
        this.outputDirMsg = (res?.error || '保存失败') + (res?.detail ? ' — ' + res.detail.slice(0, 120) : '');
        this.outputDirOk = false;
      }
      this.savingOutputDir = false;
    },

    async chooseOutputDir() {
      // Backend native directory picker
      const res = await vpApi.call(this, `${appPath}/api/choose-output-dir`, 'POST');
      if (res?.path) {
        this.outputDirInput = res.path;
        await this.setOutputDir();
        return;
      }
      // Browser File System Access API
      if (window.showDirectoryPicker) {
        try {
          const handle = await window.showDirectoryPicker({ mode: 'readwrite' });
          this.outputDirInput = handle.name;
          this.outputDirMsg = '已选择: ' + handle.name + '（浏览器目录，非服务端路径）';
          this.outputDirOk = true;
          return;
        } catch (e) { /* user cancelled */ }
      }
      // Fallback
      if (res?.remote && !window.isSecureContext) {
        this.outputDirMsg = '远程访问不支持服务端目录选择，请手动输入路径';
        this.outputDirOk = false;
      }
    },

    // === Extra reference image files ===
    onExtraFilesSelect() {
      const el = document.getElementById('vp-extra-files');
      if (!el?.files) return;
      const existing = new Set(this.extraFiles.map(f => f.name));
      for (const file of el.files) {
        if (existing.has(file.name)) continue;
        const mime = file.type || '';
        const isImage = mime.startsWith('image/');
        const isVideo = mime.startsWith('video/');
        const isAudio = mime.startsWith('audio/');
        if (!isImage && !isVideo && !isAudio) continue;
        this.extraFiles.push({
          name: file.name,
          file: file,
          mime_type: mime,
          // Only images get a preview ObjectURL — video/audio just show a label.
          preview: isImage ? URL.createObjectURL(file) : '',
        });
      }
      el.value = '';
    },
    removeExtraFile(idx) {
      const removed = this.extraFiles.splice(idx, 1)[0];
      if (removed?.preview) URL.revokeObjectURL(removed.preview);
    },

    // === Jobs ===
    async createJob() {
      if (!this.genAssetId) { this.statusText = '请选择资产 ID（图1）'; return; }
      if (!this.prompt) { this.statusText = '请输入 Prompt'; return; }
      if (this.submitting) return;
      this.submitting = true; this.statusText = '提交中...'; this.events = ''; this.results = [];

      let res;
      try {
        if (this.extraFiles.length) {
          // 多文件 + asset 任意组合 — 走 multipart
          const fd = new FormData();
          fd.append('asset_id', this.genAssetId);
          fd.append('prompt', this.prompt);
          fd.append('duration', this.duration);
          fd.append('resolution', this.resolution);
          fd.append('ratio', this.ratio);
          fd.append('repeat_count', this.repeat);
          fd.append('extra_asset_ids', JSON.stringify(this.extraAssetIds));
          for (const f of this.extraFiles) {
            fd.append('extra_files', f.file, f.name);
          }
          res = await vpApi.call(this, `${appPath}/api/virtual/jobs`, 'POST', fd);
        } else {
          // 只有 asset 引用 — 走 JSON
          res = await vpApi.call(this, `${appPath}/api/virtual/jobs`, 'POST', JSON.stringify({
            asset_id: this.genAssetId,
            extra_asset_ids: this.extraAssetIds,
            prompt: this.prompt,
            duration: this.duration, resolution: this.resolution, ratio: this.ratio, repeat_count: this.repeat
          }));
        }
      } finally {
        this.submitting = false;
      }
      if (res?.ok) {
        this.statusText = '已提交，任务在后台运行';
        this.loadJobs();
        this.pollJob(res.job_id);
      } else {
        this.statusText = '提交失败: ' + (res?.error || '');
      }
    },

    async pollJob(jobId) {
      while (true) {
        const job = await vpApi.call(this, `${appPath}/api/virtual/jobs/${jobId}`);
        if (!job || job.ok === false) break;
        this.statusText = `${job.status} ${job.done || 0}/${job.total || 0}`;
        this.events = (job.events || []).map(e => '<div>' + e.time + ' ' + e.message + '</div>').join('');
        for (const r of job.results || []) {
          if (r.download_url) {
            const url = `${appPath}${r.download_url}`;
            if (!this.results.find(x => x.url === url)) this.results.push({ url, filename: r.filename });
          }
        }
        if (['succeeded', 'failed'].includes(job.status)) break;
        await new Promise(r => setTimeout(r, 3000));
      }
      this.statusText = '空闲'; this.loadJobs();
    },

    async loadJobs() {
      const res = await vpApi.call(this, `${appPath}/api/virtual/jobs`);
      if (res?.ok) this.jobs = res.jobs || [];
    },

    async blobDownload(url, filename) {
      try {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl; a.download = filename; document.body.appendChild(a);
        a.click(); document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
      } catch (e) {
        // Fallback: direct anchor with download attribute
        const a = document.createElement('a');
        a.href = url; a.download = filename; a.target = '_blank'; a.rel = 'noopener';
        document.body.appendChild(a);
        a.click(); document.body.removeChild(a);
      }
    }
  };
}

// === Mount ===
PetiteVue.createApp({
  DreaminaApp,
  VolcenginePortraitApp,
  StatsApp,
  KeysApp,
  openPreview
}).mount();

// === User info & session ===
(async () => {
  const res = await api('/api/auth/me');
  if (!res?.ok) { location.replace('/login?next=' + encodeURIComponent(location.pathname + location.search)); return; }
  const label = document.getElementById('userLabel');
  const btn = document.getElementById('logoutBtn');
  if (label) label.textContent = res.username + (res.role === 'admin' ? ' ★' : '');
  if (btn) {
    btn.style.display = '';
    btn.addEventListener('click', async () => {
      await api('/api/auth/logout', 'POST');
      location.replace('/login');
    });
  }
  // Init key-bar selectors above iframes
  initKeyBars();
})();

// === Key-bar: dropdown above Seedance / Nano Banana iframes ===
async function initKeyBars() {
  for (const bar of document.querySelectorAll('.key-bar')) {
    const app = bar.dataset.app;
    const providers = (bar.dataset.provider || '').split(',');
    // Build selector HTML
    const sel = document.createElement('div');
    sel.style.cssText = 'padding:6px 12px;background:#1e293b;border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px;font-size:12px;color:#94a3b8';
    sel.innerHTML = `<span>API Key:</span><select style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:4px;padding:3px 8px;font-size:12px"><option value="">— 使用子应用内置 Key —</option></select><span id="kbar-hint-${app}" style="color:#22c55e;font-size:11px"></span>`;
    bar.appendChild(sel);
    const dropdown = sel.querySelector('select');

    // Load keys for all matching providers
    for (const prov of providers) {
      const r = await api(`/api/keys?provider=${prov.trim()}`);
      for (const k of (r?.keys || [])) {
        const opt = document.createElement('option');
        opt.value = k.id;
        opt.textContent = `[${k.provider}] ${k.name} (${k.key_hint})`;
        dropdown.appendChild(opt);
      }
    }

    // Restore saved selection
    const saved = localStorage.getItem(`portal_key_id_${app}`);
    if (saved && dropdown.querySelector(`option[value="${saved}"]`)) dropdown.value = saved;

    dropdown.addEventListener('change', () => {
      const val = dropdown.value;
      if (val) {
        localStorage.setItem(`portal_key_id_${app}`, val);
        document.getElementById(`kbar-hint-${app}`).textContent = '已选择，刷新 iframe 后生效';
        // Reload iframe to apply
        const iframe = document.getElementById(`iframe-${app === 'nano-banana' ? 'nb' : app}`);
        if (iframe) iframe.src = iframe.src;
      } else {
        localStorage.removeItem(`portal_key_id_${app}`);
        document.getElementById(`kbar-hint-${app}`).textContent = '';
      }
    });
  }
}
