const form = document.querySelector("#jobForm");
const submitBtn = document.querySelector("#submitBtn");
const savePresetBtn = document.querySelector("#savePresetBtn");
const clearPresetBtn = document.querySelector("#clearPresetBtn");
const loadArchiveBtn = document.querySelector("#loadArchiveBtn");
const deleteArchiveBtn = document.querySelector("#deleteArchiveBtn");
const chooseOutputBtn = document.querySelector("#chooseOutputBtn");
const appOutputBtn = document.querySelector("#appOutputBtn");
const desktopOutputBtn = document.querySelector("#desktopOutputBtn");
const openOutputBtn = document.querySelector("#openOutputBtn");
const cleanCacheBtn = document.querySelector("#cleanCacheBtn");
const archiveSelect = document.querySelector("#archiveSelect");
const statusText = document.querySelector("#statusText");
const resultsEl = document.querySelector("#results");
const eventsEl = document.querySelector("#events");
const keyHint = document.querySelector("#keyHint");
const presetHint = document.querySelector("#presetHint");
const previewDialog = document.querySelector("#previewDialog");
const previewDialogBody = document.querySelector("#previewDialogBody");
const closePreviewBtn = document.querySelector("#closePreviewBtn");
const resizeControls = document.querySelector("#resizeControls");
const mainTabBtn = document.querySelector("#mainTabBtn");
const activityTabBtn = document.querySelector("#activityTabBtn");
const mainView = document.querySelector("#mainView");
const activityView = document.querySelector("#activityView");
const refreshActivityBtn = document.querySelector("#refreshActivityBtn");
const activityStats = document.querySelector("#activityStats");
const activityList = document.querySelector("#activityList");
const activityDetail = document.querySelector("#activityDetail");
const workspaceName = document.querySelector("#workspaceName");
const newWorkspaceBtn = document.querySelector("#newWorkspaceBtn");
const duplicateWorkspaceBtn = document.querySelector("#duplicateWorkspaceBtn");
const saveWorkspaceBtn = document.querySelector("#saveWorkspaceBtn");
const workspaceHint = document.querySelector("#workspaceHint");
const urlParams = new URLSearchParams(window.location.search);
let workspaceId = urlParams.get("ws");
if (!workspaceId) {
  workspaceId = localStorage.getItem("workspace_id");
  if (!workspaceId) { workspaceId = crypto.randomUUID(); localStorage.setItem("workspace_id", workspaceId); }
}
const workspaceKey = `nano-banana.workspace.${workspaceId}`;

async function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  opts.headers["X-Workspace-Id"] = workspaceId;
  return fetch(url, opts);
}
let providerModels = {
  t8star: {
    baseUrl: "https://ai.t8star.org",
    models: ["nano-banana-2", "gemini-3.1-flash-image-preview"],
  },
  gemini: {
    baseUrl: "https://chiyun.work",
    models: ["banana2-ssvip", "nano-banana2[2K]-base", "gpt-image-2"],
  },
};
let savedMedia = {};
let workspaceSaveTimer = 0;

function providersFromConfig(providers) {
  const next = {};
  for (const [id, provider] of Object.entries(providers || {})) {
    next[id] = {
      label: provider.label || id,
      baseUrl: provider.base_url || "",
      models: (provider.models || []).map((item) => typeof item === "string" ? item : item.id).filter(Boolean),
    };
  }
  return Object.keys(next).length ? next : providerModels;
}

function field(name) {
  return form.elements[name] || document.querySelector(`[name="${name}"]`);
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
  const res = await apiFetch("/api/workspace/snapshot", { method: "POST", body: await formDataWithSavedMedia() });
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
    localStorage.setItem(`nano-banana.workspace.${id}`, JSON.stringify(current));
  }
  window.open(workspaceUrl(id), "_blank");
}

function openPreview(url) {
  previewDialogBody.innerHTML = "";
  const img = document.createElement("img");
  img.src = url;
  previewDialogBody.append(img);
  previewDialog.showModal();
}

function renderPreview(drop, url, filename) {
  drop.classList.add("hasPreview");
  drop.querySelector(".preview")?.remove();
  const img = document.createElement("img");
  img.className = "preview";
  img.src = url;
  img.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openPreview(url);
  });
  drop.append(img);
  drop.querySelector("span").textContent = filename || "已上传";
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
  input.addEventListener("change", () => {
    const drop = input.closest(".drop");
    const file = input.files?.[0];
    if (!file) {
      clearPreview(drop);
      return;
    }
    delete savedMedia[input.name];
    renderPreview(drop, URL.createObjectURL(file), file.name);
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
    if (!file || !file.type.startsWith("image/")) return;
    assignFile(input, file);
  });
}

function makeDrop(name, label) {
  const el = document.createElement("label");
  el.className = "drop";
  el.textContent = label;
  const input = document.createElement("input");
  input.name = name;
  input.type = "file";
  input.accept = "image/*";
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

for (let i = 1; i <= 14; i += 1) {
  document.querySelector("#imageRefs").append(makeDrop(`image_${i}`, `Image ${i}`));
}

function setOptions(select, values, selected) {
  select.innerHTML = "";
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
  select.value = values.includes(selected) ? selected : values[0];
}

function updateProviderOptions(preserveCurrent = true) {
  const provider = field("provider").value || "t8star";
  const config = providerModels[provider] || providerModels.t8star;
  const currentModel = preserveCurrent ? field("model").value : "";
  setOptions(field("model"), config.models, currentModel);
  const currentBase = field("base_url").value.trim();
  const knownBases = Object.values(providerModels).map((item) => item.baseUrl);
  if (!currentBase || knownBases.includes(currentBase)) {
    field("base_url").value = config.baseUrl;
  }
  field("response_format").disabled = provider === "gemini";
}

function updateResizeState() {
  const enabled = field("resize_enabled").checked;
  resizeControls.classList.toggle("isDisabled", !enabled);
  resizeControls.querySelectorAll("input, select").forEach((input) => {
    input.disabled = !enabled;
  });
}

async function loadConfig() {
  const res = await apiFetch("/api/config");
  const data = await res.json();
  keyHint.textContent = data.has_key ? `已检测到本地 key：${data.masked_key}` : "未检测到本地 key，请手动填写";
  if (data.config_error) {
    keyHint.textContent = `${keyHint.textContent}；供应商配置读取失败，请联系维护者：${data.config_error.detail || data.config_error.message}`;
    submitBtn.disabled = true;
    return;
  }
  providerModels = providersFromConfig(data.providers);
  const providerSelect = field("provider");
  const currentProvider = providerSelect.value;
  providerSelect.innerHTML = "";
  for (const [id, provider] of Object.entries(providerModels)) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = provider.label || id;
    providerSelect.append(option);
  }
  providerSelect.value = providerModels[currentProvider] ? currentProvider : (data.default_provider || Object.keys(providerModels)[0]);
  updateProviderOptions(true);
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
  updateResizeState();
  savedMedia = mediaSnapshot(preset.media);
  for (const [name, item] of Object.entries(savedMedia)) {
    const input = field(name);
    const drop = input?.closest(".drop");
    if (drop) renderPreview(drop, item.url, item.filename);
  }
  if (preset.archives) renderArchives(preset.archives);
  const count = Object.keys(savedMedia).length;
  presetHint.textContent = count ? `已读取保存配置：${count} 张图` : "";
}

async function loadPreset() {
  if (isWorkspaceMode() && loadWorkspaceDraft()) return;
  if (isWorkspaceMode()) {
    loadWorkspaceDraft();
    return;
  }
  workspaceName.value = "默认主题";
  const res = await apiFetch("/api/preset");
  if (res.ok) applyPreset(await res.json());
}

async function loadArchives() {
  const res = await apiFetch("/api/archives");
  if (res.ok) renderArchives((await res.json()).archives);
}

function appendDisabledResizeValues(data) {
  for (const name of ["resize_width", "resize_height", "resize_interpolation", "resize_method", "resize_condition", "resize_multiple_of"]) {
    const input = field(name);
    if (input) data.set(name, input.value);
  }
}

async function imageUrlToFile(url, filename) {
  const res = await fetch(url);
  const blob = await res.blob();
  return new File([blob], filename || "image.png", { type: blob.type || "image/png" });
}

function targetResizeSize(fileWidth, fileHeight) {
  let width = Math.max(1, Number(field("resize_width").value) || fileWidth);
  let height = Math.max(1, Number(field("resize_height").value) || fileHeight);
  const multiple = Math.max(0, Number(field("resize_multiple_of").value) || 0);
  if (multiple > 1) {
    width = Math.max(multiple, Math.round(width / multiple) * multiple);
    height = Math.max(multiple, Math.round(height / multiple) * multiple);
  }
  const condition = field("resize_condition").value;
  if (condition === "only_downscale" && (width >= fileWidth || height >= fileHeight)) return null;
  if (condition === "only_upscale" && (width <= fileWidth || height <= fileHeight)) return null;
  return { width, height };
}

async function resizeImageFile(file) {
  if (!field("resize_enabled").checked || !file.type.startsWith("image/")) return file;
  const bitmap = await createImageBitmap(file);
  const target = targetResizeSize(bitmap.width, bitmap.height);
  if (!target) return file;
  const canvas = document.createElement("canvas");
  canvas.width = target.width;
  canvas.height = target.height;
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = field("resize_interpolation").value;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  let sx = 0;
  let sy = 0;
  let sw = bitmap.width;
  let sh = bitmap.height;
  let dx = 0;
  let dy = 0;
  let dw = canvas.width;
  let dh = canvas.height;
  const method = field("resize_method").value;
  if (method === "contain" || method === "cover") {
    const imageRatio = bitmap.width / bitmap.height;
    const targetRatio = canvas.width / canvas.height;
    if (method === "contain") {
      if (imageRatio > targetRatio) {
        dw = canvas.width;
        dh = Math.round(canvas.width / imageRatio);
      } else {
        dh = canvas.height;
        dw = Math.round(canvas.height * imageRatio);
      }
      dx = Math.round((canvas.width - dw) / 2);
      dy = Math.round((canvas.height - dh) / 2);
    } else if (imageRatio > targetRatio) {
      sw = Math.round(bitmap.height * targetRatio);
      sx = Math.round((bitmap.width - sw) / 2);
    } else {
      sh = Math.round(bitmap.width / targetRatio);
      sy = Math.round((bitmap.height - sh) / 2);
    }
  }
  ctx.drawImage(bitmap, sx, sy, sw, sh, dx, dy, dw, dh);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  bitmap.close();
  if (!blob) return file;
  const stem = file.name.replace(/\.[^.]+$/, "");
  return new File([blob], `${stem}_resized.png`, { type: "image/png" });
}

async function formDataWithSavedMedia(options = {}) {
  const data = new FormData(form);
  appendDisabledResizeValues(data);
  const savedForBackend = { ...savedMedia };
  if (options.resizeImages && field("resize_enabled").checked) {
    for (let i = 1; i <= 14; i += 1) {
      const name = `image_${i}`;
      const input = field(name);
      let file = input?.files?.[0] || null;
      if (!file && savedMedia[name]) {
        file = await imageUrlToFile(savedMedia[name].url, savedMedia[name].filename);
      }
      if (!file) continue;
      const resized = await resizeImageFile(file);
      data.set(name, resized, resized.name);
      delete savedForBackend[name];
    }
  }
  data.set("saved_media", JSON.stringify(savedForBackend));
  return data;
}

async function savePreset() {
  presetHint.textContent = "保存中...";
  saveWorkspaceDraft();
  const res = await apiFetch("/api/preset", { method: "POST", body: await formDataWithSavedMedia() });
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
  const res = await apiFetch("/api/archive/load", { method: "POST", body: data });
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
  if (!name) return;
  const data = new FormData();
  data.set("archive_name", name);
  const res = await apiFetch("/api/archive/delete", { method: "POST", body: data });
  if (res.ok) {
    renderArchives((await res.json()).archives);
    presetHint.textContent = `已删除存档：${name}`;
  }
}

async function clearPreset() {
  const res = await apiFetch("/api/preset/clear", { method: "POST" });
  if (!res.ok) return;
  savedMedia = {};
  document.querySelectorAll(".drop").forEach(clearPreview);
  presetHint.textContent = "已清空当前读取配置";
}

function renderJob(job) {
  statusText.textContent = `${job.status} ${job.done || 0}/${job.total || 0}`;
  eventsEl.textContent = (job.events || []).map((e) => `[${e.time}] ${e.message}`).join("\n");
  resultsEl.innerHTML = "";

  for (const result of job.results || []) {
    for (const image of result.images || []) {
      const card = document.createElement("article");
      card.className = "result";
      const img = document.createElement("img");
      img.src = image.download_url;
      img.addEventListener("click", () => openPreview(image.download_url));
      const link = document.createElement("a");
      link.href = image.download_url;
      link.download = image.filename;
      link.textContent = "下载图片";
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `Run ${result.index} · task ${result.task_id} · ${image.local_path}`;
      card.append(img, link, meta);
      resultsEl.append(card);
    }
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
  const res = await apiFetch("/api/activity");
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
  const res = await apiFetch(`/api/activity/${id}`);
  const data = await res.json();
  activityDetail.innerHTML = "";
  const actions = document.createElement("div");
  actions.className = "rowButtons";
  const restoreBtn = document.createElement("button");
  restoreBtn.type = "button";
  restoreBtn.textContent = "恢复到当前页";
  restoreBtn.disabled = !data.restore;
  restoreBtn.addEventListener("click", () => {
    applyPreset(data.restore);
    saveWorkspaceDraft();
    setActiveTab("main");
    presetHint.textContent = data.restore.warning || "已从后台记录恢复参数和素材";
  });
  actions.append(restoreBtn);
  activityDetail.append(actions);
  if (data.restore?.warning) {
    const warning = document.createElement("p");
    warning.className = "hint";
    warning.textContent = data.restore.warning;
    activityDetail.append(warning);
  }
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(data, null, 2);
  activityDetail.append(pre);
}

async function poll(jobId) {
  while (true) {
    const res = await apiFetch(`/api/jobs/${jobId}`);
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
  const res = await apiFetch("/api/jobs", { method: "POST", body: await formDataWithSavedMedia({ resizeImages: true }) });
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
loadArchiveBtn.addEventListener("click", loadArchive);
deleteArchiveBtn.addEventListener("click", deleteArchive);
clearPresetBtn.addEventListener("click", clearPreset);
closePreviewBtn.addEventListener("click", () => previewDialog.close());
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
field("resize_enabled").addEventListener("change", updateResizeState);
previewDialog.addEventListener("click", (event) => {
  if (event.target === previewDialog) previewDialog.close();
});

chooseOutputBtn.addEventListener("click", async () => {
  chooseOutputBtn.disabled = true;
  try {
    const res = await apiFetch("/api/choose-output-dir", { method: "POST" });
    const data = await res.json();
    if (res.ok && data.path) field("output_dir").value = data.path;
    else presetHint.textContent = data.error || "未选择目录";
  } finally {
    chooseOutputBtn.disabled = false;
  }
});
appOutputBtn.addEventListener("click", () => { field("output_dir").value = ""; });
desktopOutputBtn.addEventListener("click", async () => {
  const res = await apiFetch("/api/default-output-dir");
  const data = await res.json();
  if (res.ok && data.path) field("output_dir").value = data.path;
});

openOutputBtn.addEventListener("click", async () => {
  openOutputBtn.disabled = true;
  try {
    const data = new FormData();
    data.set("output_dir", field("output_dir").value);
    const res = await apiFetch("/api/open-output-dir", { method: "POST", body: data });
    const payload = await res.json();
    presetHint.textContent = res.ok ? `已打开输出目录：${payload.path}` : (payload.error || "打开输出目录失败");
  } finally {
    openOutputBtn.disabled = false;
  }
});

cleanCacheBtn.addEventListener("click", async () => {
  cleanCacheBtn.disabled = true;
  try {
    const res = await apiFetch("/api/cleanup-cache", { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      presetHint.textContent = `已清理缓存：素材 ${data.media_deleted || 0} 个，日志 ${data.logs_deleted || 0} 个`;
    } else {
      presetHint.textContent = data.error || "清理缓存失败";
    }
  } finally {
    cleanCacheBtn.disabled = false;
  }
});

loadConfig();
updateProviderOptions(true);
updateResizeState();
loadPreset();
loadArchives();
