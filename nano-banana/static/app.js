const form = document.querySelector("#jobForm");
const submitBtn = document.querySelector("#submitBtn");
const savePresetBtn = document.querySelector("#savePresetBtn");
const clearPresetBtn = document.querySelector("#clearPresetBtn");
const loadArchiveBtn = document.querySelector("#loadArchiveBtn");
const deleteArchiveBtn = document.querySelector("#deleteArchiveBtn");
const chooseOutputBtn = document.querySelector("#chooseOutputBtn");
const appOutputBtn = document.querySelector("#appOutputBtn");
const desktopOutputBtn = document.querySelector("#desktopOutputBtn");
const archiveSelect = document.querySelector("#archiveSelect");
const statusText = document.querySelector("#statusText");
const resultsEl = document.querySelector("#results");
const eventsEl = document.querySelector("#events");
const keyHint = document.querySelector("#keyHint");
const presetHint = document.querySelector("#presetHint");
const previewDialog = document.querySelector("#previewDialog");
const previewDialogBody = document.querySelector("#previewDialogBody");
const closePreviewBtn = document.querySelector("#closePreviewBtn");
let savedMedia = {};

function field(name) {
  return form.elements[name] || document.querySelector(`[name="${name}"]`);
}

function openPreview(url) {
  previewDialogBody.innerHTML = "";
  const img = document.createElement("img");
  img.src = url;
  previewDialogBody.append(img);
  previewDialog.showModal();
}

function renderPreview(drop, url, filename) {
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
  drop.querySelector(".preview")?.remove();
  drop.querySelector("span").textContent = "未上传";
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
  wireFileInput(input);
  el.append(input, span);
  return el;
}

for (let i = 1; i <= 14; i += 1) {
  document.querySelector("#imageRefs").append(makeDrop(`image_${i}`, `Image ${i}`));
}

async function loadConfig() {
  const res = await fetch("/api/config");
  const data = await res.json();
  keyHint.textContent = data.has_key ? `已检测到本地 key：${data.masked_key}` : "未检测到本地 key，请手动填写";
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
  savedMedia = preset.media || {};
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
  const res = await fetch("/api/preset");
  if (res.ok) applyPreset(await res.json());
}

async function loadArchives() {
  const res = await fetch("/api/archives");
  if (res.ok) renderArchives((await res.json()).archives);
}

function formDataWithSavedMedia() {
  const data = new FormData(form);
  data.set("saved_media", JSON.stringify(savedMedia));
  return data;
}

async function savePreset() {
  presetHint.textContent = "保存中...";
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
  if (!name) return;
  const data = new FormData();
  data.set("archive_name", name);
  const res = await fetch("/api/archive/delete", { method: "POST", body: data });
  if (res.ok) {
    renderArchives((await res.json()).archives);
    presetHint.textContent = `已删除存档：${name}`;
  }
}

async function clearPreset() {
  const res = await fetch("/api/preset/clear", { method: "POST" });
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

async function poll(jobId) {
  while (true) {
    const res = await fetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    renderJob(job);
    if (["succeeded", "failed"].includes(job.status)) {
      submitBtn.disabled = false;
      submitBtn.textContent = "开始生成";
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
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
loadArchiveBtn.addEventListener("click", loadArchive);
deleteArchiveBtn.addEventListener("click", deleteArchive);
clearPresetBtn.addEventListener("click", clearPreset);
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
appOutputBtn.addEventListener("click", () => { field("output_dir").value = ""; });
desktopOutputBtn.addEventListener("click", () => { field("output_dir").value = `${navigator.platform.includes("Mac") ? "/Users/Shared" : ""}/NanoBanana_outputs`; });

loadConfig();
loadPreset();
loadArchives();
