const form = document.querySelector("#jobForm");
const submitBtn = document.querySelector("#submitBtn");
const savePresetBtn = document.querySelector("#savePresetBtn");
const clearPresetBtn = document.querySelector("#clearPresetBtn");
const loadArchiveBtn = document.querySelector("#loadArchiveBtn");
const deleteArchiveBtn = document.querySelector("#deleteArchiveBtn");
const archiveSelect = document.querySelector("#archiveSelect");
const previewDialog = document.querySelector("#previewDialog");
const previewDialogBody = document.querySelector("#previewDialogBody");
const closePreviewBtn = document.querySelector("#closePreviewBtn");
const chooseOutputBtn = document.querySelector("#chooseOutputBtn");
const appOutputBtn = document.querySelector("#appOutputBtn");
const desktopOutputBtn = document.querySelector("#desktopOutputBtn");
const mainTabBtn = document.querySelector("#mainTabBtn");
const activityTabBtn = document.querySelector("#activityTabBtn");
const mainView = document.querySelector("#mainView");
const activityView = document.querySelector("#activityView");
const refreshActivityBtn = document.querySelector("#refreshActivityBtn");
const activityStats = document.querySelector("#activityStats");
const activityList = document.querySelector("#activityList");
const activityDetail = document.querySelector("#activityDetail");
const statusText = document.querySelector("#statusText");
const resultsEl = document.querySelector("#results");
const eventsEl = document.querySelector("#events");
const keyHint = document.querySelector("#keyHint");
const presetHint = document.querySelector("#presetHint");
const providerHint = document.querySelector("#providerHint");
const workspaceName = document.querySelector("#workspaceName");
const newWorkspaceBtn = document.querySelector("#newWorkspaceBtn");
const duplicateWorkspaceBtn = document.querySelector("#duplicateWorkspaceBtn");
const saveWorkspaceBtn = document.querySelector("#saveWorkspaceBtn");
const workspaceHint = document.querySelector("#workspaceHint");
const urlParams = new URLSearchParams(window.location.search);
const workspaceId = urlParams.get("ws") || "default";
const workspaceKey = `seedance.workspace.${workspaceId}`;
let savedMedia = {};
let workspaceSaveTimer = 0;
const providerDefaults = {
  t8star: {
    baseUrl: "https://ai.t8star.cn",
    models: [
      ["doubao-seedance-2-0-260128", "doubao-seedance-2-0-260128"],
      ["doubao-seedance-2-0-fast-260128", "doubao-seedance-2-0-fast-260128"],
    ],
    hint: "使用原有 T8Star 兼容接口，素材会先上传到 /v1/files。",
  },
  volcengine: {
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    models: [
      ["doubao-seedance-2-0-260128", "doubao-seedance-2-0-260128"],
      ["doubao-seedance-2-0-fast-260128", "doubao-seedance-2-0-fast-260128"],
    ],
    hint: "使用豆包官方火山方舟 API。首尾帧模式不能与参考素材混用；本地素材会以 data URL 发送。",
  },
};

function field(name) {
  return form.elements[name] || document.querySelector(`[name="${name}"]`);
}

function updateProviderOptions(preserveBase = true) {
  const provider = field("provider")?.value || "t8star";
  const config = providerDefaults[provider] || providerDefaults.t8star;
  const baseInput = field("base_url");
  const modelInput = field("model");
  const currentModel = modelInput.value;
  const knownBases = Object.values(providerDefaults).map((item) => item.baseUrl);
  if (!preserveBase || !baseInput.value.trim() || knownBases.includes(baseInput.value.trim())) {
    baseInput.value = config.baseUrl;
  }
  modelInput.innerHTML = "";
  for (const [value, label] of config.models) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    modelInput.append(option);
  }
  modelInput.value = config.models.some(([value]) => value === currentModel) ? currentModel : config.models[0][0];
  providerHint.textContent = config.hint;
}

function isWorkspaceMode() {
  return workspaceId !== "default";
}

function workspaceLabel() {
  return workspaceName.value.trim() || (isWorkspaceMode() ? `主题 ${workspaceId.slice(0, 6)}` : "默认主题");
}

function collectWorkspaceValues() {
  const values = {};
  for (const item of form.elements) {
    if (!item.name || item.type === "file") continue;
    values[item.name] = item.type === "checkbox" ? (item.checked ? "on" : "") : item.value;
  }
  return values;
}

function mediaSnapshot(media = savedMedia) {
  return JSON.parse(JSON.stringify(media || {}));
}

function localWorkspaceSnapshot() {
  return {
    name: workspaceLabel(),
    values: collectWorkspaceValues(),
    media: mediaSnapshot(),
    saved_at: Date.now(),
  };
}

async function workspaceSnapshot(options = {}) {
  if (!options.persistMedia) return localWorkspaceSnapshot();
  const res = await fetch("/api/workspace/snapshot", { method: "POST", body: formDataWithSavedMedia() });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "保存主题素材失败");
  applyPreset(data);
  return {
    name: workspaceLabel(),
    values: collectWorkspaceValues(),
    media: mediaSnapshot(data.media),
    saved_at: Date.now(),
  };
}

async function saveWorkspaceDraft(options = {}) {
  const payload = await workspaceSnapshot(options);
  localStorage.setItem(workspaceKey, JSON.stringify(payload));
  workspaceHint.textContent = `已保存草稿：${payload.name}`;
  return payload;
}

function scheduleWorkspaceSave() {
  clearTimeout(workspaceSaveTimer);
  workspaceSaveTimer = setTimeout(saveWorkspaceDraft, 500);
}

function loadWorkspaceDraft() {
  workspaceName.value = isWorkspaceMode() ? workspaceLabel() : "默认主题";
  workspaceHint.textContent = isWorkspaceMode() ? "当前是独立主题页，可与其它主题并发提交" : "默认主题会读取当前保存配置";
  const raw = localStorage.getItem(workspaceKey);
  if (!raw) return false;
  try {
    const draft = JSON.parse(raw);
    workspaceName.value = draft.name || workspaceName.value;
    applyPreset({ values: draft.values || {}, media: draft.media || {} });
    workspaceHint.textContent = `已读取主题草稿：${workspaceName.value}`;
    return true;
  } catch {
    return false;
  }
}

function workspaceUrl(id) {
  const url = new URL(window.location.href);
  url.searchParams.set("ws", id);
  return url.toString();
}

function newWorkspaceId() {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

async function openWorkspace(copyCurrent) {
  const id = newWorkspaceId();
  if (copyCurrent) {
    const current = await saveWorkspaceDraft({ persistMedia: true });
    current.name = `${workspaceLabel()} 副本`;
    localStorage.setItem(`seedance.workspace.${id}`, JSON.stringify(current));
  }
  window.open(workspaceUrl(id), "_blank");
}

function mediaKind(name) {
  if (name.includes("video")) return "video";
  if (name.includes("audio")) return "audio";
  return "image";
}

function renderPreview(drop, kind, url, filename) {
  drop.classList.add("hasPreview");
  drop.querySelector(".preview")?.remove();
  const media = document.createElement(kind === "image" ? "img" : kind);
  media.className = "preview";
  media.src = url;
  if (kind !== "image") media.controls = true;
  if (kind !== "audio") {
    media.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openPreview(kind, url);
    });
  }
  drop.append(media);
  drop.querySelector("span").textContent = filename || "已上传";
}

function openPreview(kind, url) {
  previewDialogBody.innerHTML = "";
  const media = document.createElement(kind === "image" ? "img" : "video");
  media.src = url;
  if (kind === "video") media.controls = true;
  previewDialogBody.append(media);
  previewDialog.showModal();
}

function clearPreview(drop) {
  drop.classList.remove("hasPreview");
  drop.querySelector(".preview")?.remove();
  drop.querySelector("span").textContent = "未上传";
}

function clearSelectedMedia(input) {
  input.value = "";
  delete savedMedia[input.name];
  clearPreview(input.closest(".drop"));
}

function clearAllMediaInputs() {
  document.querySelectorAll('.drop input[type="file"]').forEach((input) => {
    input.value = "";
    const drop = input.closest(".drop");
    if (drop) clearPreview(drop);
  });
}

function assignFile(input, file) {
  const transfer = new DataTransfer();
  transfer.items.add(file);
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function wireFileInput(input) {
  if (input.dataset.wired === "1") return;
  input.dataset.wired = "1";
  input.addEventListener("change", () => {
    const drop = input.closest(".drop");
    const file = input.files?.[0];
    if (!file) {
      clearPreview(drop);
      return;
    }
    delete savedMedia[input.name];
    renderPreview(drop, mediaKind(input.name), URL.createObjectURL(file), file.name);
  });

  const drop = input.closest(".drop");
  drop.addEventListener("dragover", (event) => {
    event.preventDefault();
    drop.classList.add("isDragging");
  });
  drop.addEventListener("dragleave", () => {
    drop.classList.remove("isDragging");
  });
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("isDragging");
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    if (input.accept && !input.accept.split(",").some((accept) => {
      const rule = accept.trim();
      return rule.endsWith("/*") ? file.type.startsWith(rule.slice(0, -1)) : file.type === rule;
    })) return;
    assignFile(input, file);
  });
}

function makeDrop(name, label, accept) {
  const el = document.createElement("label");
  el.className = "drop";
  el.textContent = label;

  const input = document.createElement("input");
  input.name = name;
  input.type = "file";
  input.accept = accept;
  input.setAttribute("form", "jobForm");

  const span = document.createElement("span");
  span.textContent = "未上传";

  const removeBtn = document.createElement("button");
  removeBtn.className = "removeMediaBtn";
  removeBtn.type = "button";
  removeBtn.textContent = "移除";
  removeBtn.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    clearSelectedMedia(input);
  });

  el.append(input, span, removeBtn);
  wireFileInput(input);
  return el;
}

function ensureDropControls(input) {
  const drop = input.closest(".drop");
  if (!drop) return;
  if (!drop.querySelector(".removeMediaBtn")) {
    const removeBtn = document.createElement("button");
    removeBtn.className = "removeMediaBtn";
    removeBtn.type = "button";
    removeBtn.textContent = "移除";
    removeBtn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      clearSelectedMedia(input);
    });
    drop.append(removeBtn);
  }
  wireFileInput(input);
}

for (let i = 1; i <= 9; i += 1) {
  document.querySelector("#imageRefs").append(makeDrop(`ref_image_${i}`, `@ref_image${i}`, "image/*"));
}
for (let i = 1; i <= 3; i += 1) {
  document.querySelector("#videoRefs").append(makeDrop(`ref_video_${i}`, `参考视频 ${i}`, "video/*"));
  document.querySelector("#audioRefs").append(makeDrop(`ref_audio_${i}`, `参考音频 ${i}`, "audio/*"));
}
document.querySelectorAll('.drop input[type="file"]').forEach(ensureDropControls);

async function loadConfig() {
  const res = await fetch("/api/config");
  const data = await res.json();
  keyHint.textContent = data.has_key ? `已检测到本地 key：${data.masked_key}` : "未检测到本地 key，请手动填写";
}

function applyPreset(preset) {
  clearAllMediaInputs();
  const values = preset.values || {};
  for (const [name, value] of Object.entries(values)) {
    const input = field(name);
    if (!input) continue;
    if (input.type === "checkbox") {
      input.checked = ["1", "true", "yes", "on"].includes(String(value).toLowerCase());
    } else if (input.type !== "file") {
      input.value = value;
    }
  }
  updateProviderOptions(true);

  savedMedia = mediaSnapshot(preset.media);
  for (const [name, item] of Object.entries(savedMedia)) {
    const input = field(name);
    const drop = input?.closest(".drop");
    if (!drop) continue;
    renderPreview(drop, mediaKind(name), item.url, item.filename);
  }
  const count = Object.keys(savedMedia).length;
  presetHint.textContent = count ? `已读取保存配置：${count} 个素材` : "";
  if (preset.archives) renderArchives(preset.archives);
}

function renderArchives(archives) {
  archiveSelect.innerHTML = "";
  if (!archives || archives.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "暂无存档";
    archiveSelect.append(option);
    return;
  }
  for (const item of archives) {
    const option = document.createElement("option");
    option.value = item.name;
    option.textContent = item.name;
    archiveSelect.append(option);
  }
}

async function loadArchives() {
  const res = await fetch("/api/archives");
  if (!res.ok) return;
  renderArchives((await res.json()).archives);
}

async function loadPreset() {
  if (isWorkspaceMode() && loadWorkspaceDraft()) return;
  if (isWorkspaceMode()) {
    loadWorkspaceDraft();
    return;
  }
  workspaceName.value = "默认主题";
  const res = await fetch("/api/preset");
  if (!res.ok) return;
  applyPreset(await res.json());
}

function formDataWithSavedMedia() {
  const data = new FormData(form);
  data.set("saved_media", JSON.stringify(savedMedia));
  return data;
}

async function savePreset() {
  presetHint.textContent = "保存中...";
  saveWorkspaceDraft();
  const res = await fetch("/api/preset", { method: "POST", body: formDataWithSavedMedia() });
  const data = await res.json();
  if (!res.ok) {
    presetHint.textContent = data.error || "保存失败";
    return;
  }
  applyPreset(data);
  presetHint.textContent = data.archive ? `已保存为 ${data.archive}` : "已保存当前配置和素材";
}

async function loadArchive() {
  const name = archiveSelect.value;
  if (!name) {
    presetHint.textContent = "请选择一个存档";
    return;
  }
  const data = new FormData();
  data.set("archive_name", name);
  const res = await fetch("/api/archive/load", { method: "POST", body: data });
  const payload = await res.json();
  if (!res.ok) {
    presetHint.textContent = payload.error || "读取失败";
    return;
  }
  applyPreset(payload);
  field("archive_name").value = name;
  presetHint.textContent = `已读取存档：${name}`;
}

async function deleteArchive() {
  const name = archiveSelect.value;
  if (!name) {
    presetHint.textContent = "请选择一个存档";
    return;
  }
  const data = new FormData();
  data.set("archive_name", name);
  const res = await fetch("/api/archive/delete", { method: "POST", body: data });
  const payload = await res.json();
  if (res.ok) {
    renderArchives(payload.archives);
    presetHint.textContent = `已删除存档：${name}`;
  }
}

async function clearPreset() {
  const res = await fetch("/api/preset/clear", { method: "POST" });
  if (!res.ok) return;
  savedMedia = {};
  document.querySelectorAll(".drop").forEach(clearPreview);
  presetHint.textContent = "已清空保存配置";
}

function renderJob(job) {
  statusText.textContent = `${job.status} ${job.done || 0}/${job.total || 0}`;
  eventsEl.textContent = (job.events || []).map((e) => `[${e.time}] ${e.message}`).join("\n");

  resultsEl.innerHTML = "";
  for (const result of job.results || []) {
    const card = document.createElement("article");
    card.className = "result";
    const video = document.createElement("video");
    video.controls = true;
    video.src = result.download_url;

    const link = document.createElement("a");
    link.href = result.download_url;
    link.download = result.filename;
    link.textContent = "下载视频";

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `Run ${result.index} · task ${result.task_id} · ${result.local_path}`;

    card.append(video, link, meta);
    resultsEl.append(card);
  }

  for (const error of job.errors || []) {
    const card = document.createElement("article");
    card.className = "result";
    card.textContent = error;
    resultsEl.append(card);
  }
}

function setActiveTab(tab) {
  const showActivity = tab === "activity";
  mainTabBtn.classList.toggle("isActive", !showActivity);
  activityTabBtn.classList.toggle("isActive", showActivity);
  mainView.classList.toggle("hidden", showActivity);
  activityView.classList.toggle("hidden", !showActivity);
  if (showActivity) loadActivity();
}

function renderActivityStats(counts) {
  const items = [
    ["总记录", counts.total || 0],
    ["页面运行", counts.page || 0],
    ["API 调用", counts.api || 0],
    ["成功", counts.succeeded || 0],
    ["失败", counts.failed || 0],
    ["运行中", counts.running || 0],
  ];
  activityStats.innerHTML = items.map(([label, value]) => (
    `<div class="activityStat"><strong>${value}</strong><span>${label}</span></div>`
  )).join("");
}

function renderActivityList(records) {
  activityList.innerHTML = "";
  if (!records.length) {
    activityList.textContent = "暂无记录";
    return;
  }
  for (const record of records) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "activityItem";
    button.dataset.id = record.id;
    button.textContent = `${record.source === "api" ? "API" : "页面"} · ${record.status || "unknown"}`;
    const meta = document.createElement("span");
    meta.className = "activityItemMeta";
    meta.textContent = `${record.created_at || ""} · ${record.title || record.job_id || record.id}`;
    button.append(meta);
    button.addEventListener("click", () => loadActivityDetail(record.id, button));
    activityList.append(button);
  }
}

async function loadActivity() {
  const res = await fetch("/api/activity");
  const data = await res.json();
  if (!res.ok) {
    activityDetail.textContent = data.error || "读取后台记录失败";
    return;
  }
  renderActivityStats(data.counts || {});
  renderActivityList(data.records || []);
}

async function loadActivityDetail(id, activeButton) {
  activityList.querySelectorAll(".activityItem").forEach((item) => item.classList.remove("isActive"));
  activeButton?.classList.add("isActive");
  const res = await fetch(`/api/activity/${id}`);
  const data = await res.json();
  activityDetail.textContent = JSON.stringify(data, null, 2);
}

async function poll(jobId) {
  while (true) {
    const res = await fetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    renderJob(job);
    if (["succeeded", "failed"].includes(job.status)) {
      submitBtn.disabled = false;
      submitBtn.textContent = "开始生成";
      loadActivity();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveWorkspaceDraft();
  submitBtn.disabled = true;
  submitBtn.textContent = "生成中";
  statusText.textContent = "提交中";
  resultsEl.innerHTML = "";
  eventsEl.textContent = "";

  const res = await fetch("/api/jobs", { method: "POST", body: formDataWithSavedMedia() });
  const payload = await res.json();
  if (!res.ok) {
    submitBtn.disabled = false;
    submitBtn.textContent = "开始生成";
    statusText.textContent = payload.error || "提交失败";
    return;
  }
  poll(payload.job_id);
});

savePresetBtn.addEventListener("click", savePreset);
clearPresetBtn.addEventListener("click", clearPreset);
loadArchiveBtn.addEventListener("click", loadArchive);
deleteArchiveBtn.addEventListener("click", deleteArchive);
newWorkspaceBtn.addEventListener("click", () => openWorkspace(false));
mainTabBtn.addEventListener("click", () => setActiveTab("main"));
activityTabBtn.addEventListener("click", () => setActiveTab("activity"));
refreshActivityBtn.addEventListener("click", loadActivity);
duplicateWorkspaceBtn.addEventListener("click", async () => {
  duplicateWorkspaceBtn.disabled = true;
  try {
    await openWorkspace(true);
  } catch (error) {
    workspaceHint.textContent = error.message || "复制主题失败";
  } finally {
    duplicateWorkspaceBtn.disabled = false;
  }
});
saveWorkspaceBtn.addEventListener("click", async () => {
  saveWorkspaceBtn.disabled = true;
  try {
    await saveWorkspaceDraft({ persistMedia: true });
  } catch (error) {
    workspaceHint.textContent = error.message || "保存草稿失败";
  } finally {
    saveWorkspaceBtn.disabled = false;
  }
});
form.addEventListener("input", scheduleWorkspaceSave);
form.addEventListener("change", scheduleWorkspaceSave);
field("provider").addEventListener("change", () => updateProviderOptions(false));
closePreviewBtn.addEventListener("click", () => previewDialog.close());
previewDialog.addEventListener("click", (event) => {
  if (event.target === previewDialog) previewDialog.close();
});

chooseOutputBtn.addEventListener("click", async () => {
  chooseOutputBtn.disabled = true;
  try {
    const res = await fetch("/api/choose-output-dir", { method: "POST" });
    const data = await res.json();
    if (res.ok && data.path) field("output_dir").value = data.path;
    else presetHint.textContent = data.error || "未选择目录";
  } finally {
    chooseOutputBtn.disabled = false;
  }
});

appOutputBtn.addEventListener("click", () => {
  field("output_dir").value = "";
});

desktopOutputBtn.addEventListener("click", async () => {
  const res = await fetch("/api/default-output-dir");
  const data = await res.json();
  if (res.ok && data.path) field("output_dir").value = data.path;
});

updateProviderOptions(true);
loadConfig();
loadPreset();
loadArchives();
