(() => {
  "use strict";

  const ReviewState = globalThis.ReviewState;
  if (!ReviewState) throw new Error("审批草稿状态模块加载失败");
  const BitableState = globalThis.BitableState;
  if (!BitableState) throw new Error("多维表格状态模块加载失败");

  const state = {
    runId: null,
    view: null,
    busy: false,
    runMode: null,
    modes: { bitable: false, legacy_delivery: false },
    bitable: BitableState.createState(),
    review: ReviewState.createReviewState(),
  };
  const byId = (id) => document.getElementById(id);
  const runForm = byId("run-form");
  const errorMessage = byId("error-message");
  const taskList = byId("task-list");
  const rejectButton = byId("reject-button");
  const cancelButton = byId("cancel-button");
  const approveButton = byId("approve-button");
  const retryDeliveryButton = byId("retry-delivery-button");
  const deleteRunButton = byId("delete-run-button");
  const conflictBox = byId("review-conflict");
  const conflictText = byId("review-conflict-text");
  const discardButton = byId("discard-review-draft");
  const scanBitableButton = byId("scan-bitable-button");
  const bitableTaskList = byId("bitable-task-list");
  const bitableStatus = byId("bitable-status");

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function detailText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => item.msg || JSON.stringify(item)).join("；");
    }
    if (detail && typeof detail === "object") return JSON.stringify(detail);
    return "请求失败";
  }

  function showError(error) {
    errorMessage.textContent = error instanceof Error ? error.message : String(error);
    errorMessage.hidden = false;
  }

  function clearError() {
    errorMessage.textContent = "";
    errorMessage.hidden = true;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      const detail = payload && typeof payload === "object" ? payload.detail : payload;
      const error = new Error(detailText(detail));
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function setBusy(value) {
    state.busy = value;
    runForm.querySelectorAll("input, button").forEach((control) => {
      control.disabled = value || !state.modes.legacy_delivery;
    });
    scanBitableButton.disabled = value || !state.modes.bitable;
    bitableTaskList.querySelectorAll("button").forEach((control) => {
      control.disabled = value;
    });
    updateActionAvailability();
  }

  function renderBitableTasks() {
    const scan = state.bitable.scan;
    if (scan.phase === "loading") bitableStatus.textContent = "正在读取多维表格…";
    else if (scan.phase === "error") bitableStatus.textContent = scan.error;
    else if (state.bitable.claim.phase === "conflict") {
      bitableStatus.textContent = state.bitable.claim.error;
    } else if (scan.phase === "ready") {
      bitableStatus.textContent = state.bitable.tasks.length
        ? `发现 ${state.bitable.tasks.length} 条可处理任务，请手动选择一条。`
        : "当前没有来源有效且结果为空的可处理任务。";
    }

    const nodes = state.bitable.tasks.map((task) => {
      const card = element("article", "bitable-task");
      const identity = element("div", "");
      const executors = task.executor_names?.length
        ? task.executor_names.join("、")
        : task.executor_open_ids?.length
        ? task.executor_open_ids.join("、")
        : "未指定";
      identity.append(
        element("h3", "", task.display_text || task.record_id),
        element("p", "bitable-task-meta", `执行人：${executors}`),
      );
      const link = element("a", "", "查看需求来源");
      link.href = task.source_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      const claim = element("button", "primary", "开始分析");
      claim.type = "button";
      claim.disabled = state.busy || state.bitable.claim.phase === "loading";
      claim.addEventListener("click", () => claimBitableTask(task.record_id));
      card.append(identity, link, claim);
      return card;
    });
    if (scan.phase === "ready" && nodes.length === 0) {
      nodes.push(element("p", "bitable-empty", "没有可领取任务。"));
    }
    bitableTaskList.replaceChildren(...nodes);
  }

  async function scanBitableTasks() {
    if (state.busy || !state.modes.bitable) return;
    state.bitable = BitableState.scanStarted(state.bitable);
    renderBitableTasks();
    setBusy(true);
    clearError();
    try {
      const tasks = await api("/api/bitable/tasks");
      state.bitable = BitableState.scanSucceeded(state.bitable, tasks);
    } catch (error) {
      state.bitable = BitableState.scanFailed(state.bitable, error.message);
      showError(error);
    } finally {
      setBusy(false);
      renderBitableTasks();
    }
  }

  async function claimBitableTask(recordId) {
    if (state.busy) return;
    state.bitable = BitableState.claimStarted(state.bitable, recordId);
    renderBitableTasks();
    setBusy(true);
    clearError();
    try {
      const created = await api(
        `/api/bitable/tasks/${encodeURIComponent(recordId)}/claim`,
        { method: "POST" },
      );
      state.bitable = BitableState.claimSucceeded(state.bitable, created.run_id);
      state.runId = created.run_id;
      state.runMode = "bitable";
      state.review = ReviewState.createReviewState();
      await poll(true);
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      state.bitable = error.status === 409
        ? BitableState.claimConflict(state.bitable, error.message)
        : BitableState.claimConflict(state.bitable, error.message);
      showError(error);
    } finally {
      setBusy(false);
      renderBitableTasks();
    }
  }

  async function configureModes() {
    try {
      const health = await api("/api/health");
      state.modes = health.modes || state.modes;
    } catch (error) {
      showError(error);
    }
    scanBitableButton.disabled = !state.modes.bitable;
    if (!state.modes.bitable) {
      bitableStatus.textContent = "多维表格尚未配置，请先补全表格链接、数据表和视图。";
    }
    if (!state.modes.legacy_delivery) {
      runForm.querySelectorAll("input, button").forEach((control) => {
        control.disabled = true;
      });
      const message = byId("legacy-mode-message");
      message.textContent = "当前未配置旧版文档交付，请从下方多维表格任务开始。";
      message.hidden = false;
    }
  }

  function updateActionAvailability() {
    const canReview = state.view && state.view.status === "waiting_approval";
    const conflict = ReviewState.conflictMessage(state.review);
    rejectButton.disabled = state.busy || !canReview;
    cancelButton.disabled = state.busy || !canReview;
    approveButton.disabled = state.busy || !ReviewState.canApprove(state.review);
    retryDeliveryButton.disabled = state.busy || state.view?.status !== "delivery_failed";
    const deletable = [
      "waiting_approval", "succeeded", "completed_with_errors",
      "delivery_failed", "failed", "cancelled",
    ].includes(state.view?.status);
    deleteRunButton.disabled = state.busy || !deletable;
    byId("reject-feedback").disabled = state.busy || !canReview;
    taskList.querySelectorAll("input, textarea, select, button").forEach((control) => {
      control.disabled = state.busy || !canReview || Boolean(conflict);
    });
    conflictText.textContent = conflict;
    conflictBox.hidden = !conflict;
    discardButton.disabled = state.busy || !conflict;
  }

  function formatDuration(value) {
    if (typeof value !== "number") return "—";
    if (value < 1000) return `${value} ms`;
    return `${(value / 1000).toFixed(1)} s`;
  }

  function renderEvents(events) {
    const list = byId("event-list");
    const nodes = (events || []).map((event) => {
      const item = element("li", "event-item");
      const meta = element("div", "event-meta");
      meta.append(
        element("strong", "", `${event.node || "workflow"} · ${event.status || ""}`),
        element("span", "", formatDuration(event.duration_ms)),
      );
      item.append(meta, element("p", "event-summary", event.summary || ""));
      return item;
    });
    list.replaceChildren(...nodes);
  }

  function descriptionFor(assetId) {
    const descriptions = state.view?.approval?.vision_descriptions || [];
    return descriptions.find((item) => item.asset_id === assetId) || null;
  }

  function assetFor(assetId) {
    const assets = state.view?.approval?.media_assets || [];
    return assets.find((item) => item.asset_id === assetId) || null;
  }

  function field(labelText, control, wide = false) {
    const wrapper = element("div", wide ? "field field-wide" : "field");
    wrapper.append(element("label", "", labelText), control);
    return wrapper;
  }

  function textArea(value, onInput, rows = 3) {
    const control = document.createElement("textarea");
    control.rows = rows;
    control.value = value || "";
    control.addEventListener("input", () => onInput(control.value));
    return control;
  }

  function textInput(value, onInput, type = "text") {
    const control = document.createElement("input");
    control.type = type;
    control.value = value ?? "";
    control.addEventListener("input", () => onInput(control.value));
    return control;
  }

  function updateTask(taskId, patch) {
    try {
      state.review = ReviewState.patchTask(state.review, taskId, patch);
      state.view = ReviewState.draftView(state.review);
      updateActionAvailability();
    } catch (error) {
      showError(error);
    }
  }

  function currentTask(taskId) {
    return state.view?.approval?.tasks.find((task) => task.task_id === taskId) || null;
  }

  function updateReference(taskId, assetId, patch) {
    const task = currentTask(taskId);
    if (!task) return;
    const references = task.reference_images.map((reference) => (
      reference.asset_id === assetId ? { ...reference, ...patch } : reference
    ));
    updateTask(taskId, { reference_images: references });
  }

  function requireCleanDraftForReferenceMutation() {
    if (!ReviewState.hasDirty(state.review)) return true;
    showError(new Error("请先提交或放弃本地任务编辑，再增添、替换或删除参考图片"));
    return false;
  }

  async function patchReferences(task) {
    if (!ReviewState.canSaveReferences(state.review, task.task_id)) {
      showError(new Error("请先处理其他本地任务编辑，再保存参考图片用途与顺序"));
      return;
    }
    const references = currentTask(task.task_id)?.reference_images || [];
    await mutate(`/api/runs/${state.runId}/tasks/${encodeURIComponent(task.task_id)}/references`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ references }),
    }, true);
  }

  async function uploadReference(task, fileInput, role, order, replacesAssetId = null) {
    if (!requireCleanDraftForReferenceMutation()) return;
    const file = fileInput.files[0];
    if (!file) {
      showError(new Error("请选择图片文件"));
      return;
    }
    const body = new FormData();
    body.append("file", file);
    body.append("task_id", task.task_id);
    body.append("role", role);
    body.append("order", String(order));
    if (replacesAssetId) body.append("replaces_asset_id", replacesAssetId);
    await mutate(`/api/runs/${state.runId}/references`, { method: "POST", body }, true);
  }

  async function unlinkReference(task, assetId) {
    if (!requireCleanDraftForReferenceMutation()) return;
    await mutate(
      `/api/runs/${state.runId}/tasks/${encodeURIComponent(task.task_id)}/references/${encodeURIComponent(assetId)}`,
      { method: "DELETE" },
      true,
    );
  }

  async function mutate(url, options, resetDraft = false) {
    if (state.busy) return;
    setBusy(true);
    clearError();
    try {
      await api(url, options);
      await poll(true, resetDraft);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  function referenceRow(task, reference) {
    const asset = assetFor(reference.asset_id);
    const description = descriptionFor(reference.asset_id);
    const row = element("div", "reference-row");
    row.dataset.referenceTask = task.task_id;
    row.dataset.assetId = reference.asset_id;

    const image = document.createElement("img");
    image.alt = `参考图片 ${reference.order}`;
    if (asset?.preview_url) image.src = asset.preview_url;

    const descriptionText = description
      ? [description.subjects?.join("、"), description.scene, description.probable_role]
          .filter(Boolean)
          .join(" · ")
      : "本地新增图片，尚无视觉描述";
    const descriptionNode = element("div", "reference-description", descriptionText);

    const role = document.createElement("select");
    ["reference_image", "first_frame", "last_frame"].forEach((value) => {
      const option = element("option", "", value);
      option.value = value;
      option.selected = value === reference.role;
      role.append(option);
    });
    role.setAttribute("aria-label", "图片用途");
    role.addEventListener("change", () => {
      updateReference(task.task_id, reference.asset_id, { role: role.value });
    });

    const order = document.createElement("input");
    order.type = "number";
    order.min = "1";
    order.value = reference.order;
    order.setAttribute("aria-label", "图片顺序");
    order.addEventListener("input", () => {
      updateReference(task.task_id, reference.asset_id, { order: Number(order.value) });
    });

    const actions = element("div", "reference-actions");
    const replaceInput = document.createElement("input");
    replaceInput.type = "file";
    replaceInput.accept = "image/*";
    replaceInput.hidden = true;
    const replace = element("button", "quiet-button", "替换");
    replace.type = "button";
    replace.addEventListener("click", () => replaceInput.click());
    replaceInput.addEventListener("change", () => {
      uploadReference(task, replaceInput, role.value, Number(order.value), reference.asset_id);
    });
    const remove = element("button", "quiet-button", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => unlinkReference(task, reference.asset_id));
    actions.append(replaceInput, replace, remove);
    row.append(image, descriptionNode, role, order, actions);
    return row;
  }

  function referenceSection(task) {
    const section = element("section", "reference-section");
    const heading = element("div", "panel-heading");
    heading.append(element("h3", "", "参考图片"));
    const save = element("button", "quiet-button", "保存用途与顺序");
    save.type = "button";
    save.addEventListener("click", () => patchReferences(task));
    heading.append(save);
    const list = element("div", "reference-list");
    [...task.reference_images]
      .sort((a, b) => a.order - b.order)
      .forEach((reference) => list.append(referenceRow(task, reference)));

    const upload = element("div", "upload-row");
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = "image/*";
    const role = document.createElement("select");
    ["reference_image", "first_frame", "last_frame"].forEach((value) => {
      const option = element("option", "", value);
      option.value = value;
      role.append(option);
    });
    const order = document.createElement("input");
    order.type = "number";
    order.min = "1";
    order.value = String(task.reference_images.length + 1);
    const add = element("button", "secondary", "增添图片");
    add.type = "button";
    add.addEventListener("click", () => {
      uploadReference(task, fileInput, role.value, Number(order.value));
    });
    upload.append(fileInput, role, order, add);
    section.append(heading, list, upload);
    return section;
  }

  function renderTask(task) {
    const card = element("article", "task-card");
    const titleRow = element("div", "task-title-row");
    const selected = document.createElement("input");
    selected.type = "checkbox";
    selected.checked = ReviewState.selectedTaskIds(state.review).includes(task.task_id);
    selected.dataset.taskId = task.task_id;
    selected.setAttribute("aria-label", `选择任务 ${task.title}`);
    selected.addEventListener("change", () => {
      try {
        state.review = ReviewState.setTaskSelected(state.review, task.task_id, selected.checked);
        state.view = ReviewState.draftView(state.review);
        updateActionAvailability();
      } catch (error) {
        showError(error);
      }
    });
    const title = element("div", "");
    title.append(
      element("h3", "", task.title),
      element("span", "task-type", `${task.task_type} · 置信度 ${task.confidence ?? "—"}`),
    );
    titleRow.append(selected, title);

    const grid = element("div", "task-grid");
    grid.append(
      field("提示词", textArea(task.prompt, (value) => {
        updateTask(task.task_id, { prompt: value });
      }, 5), true),
      field(
        "负面约束",
        textArea((task.negative_constraints || []).join("\n"), (value) => {
          updateTask(task.task_id, {
            negative_constraints: value.split("\n").map((item) => item.trim()).filter(Boolean),
          });
        }),
        true,
      ),
      field("画面比例", textInput(task.aspect_ratio, (value) => {
        updateTask(task.task_id, { aspect_ratio: value });
      })),
      field("生成数量", textInput(task.output_count, (value) => {
        updateTask(task.task_id, { output_count: Number(value) });
      }, "number")),
    );
    if (task.task_type === "image_to_image") {
      grid.append(field("图片尺寸", textInput(task.image_size, (value) => {
        updateTask(task.task_id, { image_size: value });
      })));
    } else {
      grid.append(
        field("视频时长", textInput(task.duration, (value) => {
          updateTask(task.task_id, { duration: Number(value) });
        }, "number")),
        field("分辨率", textInput(task.resolution, (value) => {
          updateTask(task.task_id, { resolution: value });
        })),
      );
      const audio = document.createElement("select");
      [["true", "开启"], ["false", "关闭"]].forEach(([value, label]) => {
        const option = element("option", "", label);
        option.value = value;
        option.selected = String(Boolean(task.generate_audio)) === value;
        audio.append(option);
      });
      audio.addEventListener("change", () => {
        updateTask(task.task_id, { generate_audio: audio.value === "true" });
      });
      grid.append(field("声音", audio));
    }

    const notes = element("div", "task-notes");
    (task.assumptions || []).forEach((text) => notes.append(element("span", "note", `假设：${text}`)));
    (task.warnings || []).forEach((text) => notes.append(element("span", "note", `警告：${text}`)));
    (task.blocking_issues || []).forEach((text) => notes.append(element("span", "note blocking", `阻塞：${text}`)));
    card.append(titleRow, grid, notes, referenceSection(task));
    return card;
  }

  function render(view) {
    state.view = view;
    byId("status-badge").textContent = view.status;
    byId("run-status").textContent = view.status;
    byId("thread-id").textContent = view.thread_id;
    const latestEvent = (view.events || []).at(-1);
    byId("current-node").textContent = latestEvent?.node || "—";
    const durations = (view.events || []).map((item) => item.duration_ms).filter((item) => typeof item === "number");
    byId("run-duration").textContent = formatDuration(durations.reduce((sum, value) => sum + value, 0));
    byId("document-title").textContent = view.approval.document_title || "未命名文档";
    byId("source-link").href = view.source_url;
    byId("document-revision").textContent = view.approval.revision ?? "—";
    byId("document-summary").textContent = view.approval.document_summary || "";
    byId("langsmith-warning").hidden = !view.privacy?.langsmith_tracing;
    renderEvents(view.events);

    const issues = view.approval.validation_issues || [];
    const issueBox = byId("validation-issues");
    issueBox.textContent = issues.join("；");
    issueBox.hidden = issues.length === 0;
    taskList.replaceChildren(...(view.approval.tasks || []).map(renderTask));
    updateActionAvailability();
  }

  async function poll(force = false, resetDraft = false) {
    if (!state.runId || (state.busy && !force)) return;
    if (!force && document.activeElement?.closest(".task-card")) return;
    try {
      const serverView = await api(`/api/runs/${state.runId}`);
      state.review = resetDraft
        ? ReviewState.mergeServerView(ReviewState.createReviewState(), serverView)
        : ReviewState.mergeServerView(state.review, serverView);
      render(ReviewState.draftView(state.review));
    } catch (error) {
      showError(error);
    }
  }

  async function submitDecision(action) {
    if (!state.runId || state.busy) return;
    let body = { action };
    if (action === "reject") body.feedback = byId("reject-feedback").value;
    try {
      if (action === "approve") {
        const submission = ReviewState.beginApprovalSubmit(state.review);
        state.review = submission.state;
        body = submission.payload;
      }
      setBusy(true);
      clearError();
      await api(`/api/runs/${state.runId}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (action === "approve") {
        state.review = ReviewState.completeApprovalSubmit(state.review);
      } else {
        state.review = ReviewState.createReviewState();
      }
      await poll(true);
    } catch (error) {
      if (action === "approve" && ReviewState.isSubmitting(state.review)) {
        state.review = ReviewState.failApprovalSubmit(state.review);
        state.view = ReviewState.draftView(state.review);
      }
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  runForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy) return;
    setBusy(true);
    clearError();
    try {
      const created = await api("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_url: byId("source-url").value }),
      });
      state.runId = created.run_id;
      state.runMode = "legacy";
      state.review = ReviewState.createReviewState();
      await poll(true);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  });

  byId("reject-button").addEventListener("click", () => submitDecision("reject"));
  byId("cancel-button").addEventListener("click", () => submitDecision("cancel"));
  byId("approve-button").addEventListener("click", () => submitDecision("approve"));
  retryDeliveryButton.addEventListener("click", async () => {
    if (!state.runId || state.busy) return;
    const url = state.runMode === "bitable"
      ? `/api/bitable/runs/${state.runId}/retry-delivery`
      : `/api/runs/${state.runId}/retry-delivery`;
    if (state.runMode !== "bitable") {
      await mutate(url, { method: "POST" });
      return;
    }
    state.bitable = BitableState.retryStarted(state.bitable, state.runId);
    setBusy(true);
    clearError();
    try {
      await api(url, { method: "POST" });
      state.bitable = BitableState.retrySucceeded(state.bitable);
      await poll(true);
    } catch (error) {
      state.bitable = BitableState.retryFailed(state.bitable, error.message);
      showError(error);
    } finally {
      setBusy(false);
    }
  });
  deleteRunButton.addEventListener("click", async () => {
    if (!state.runId || state.busy) return;
    if (!globalThis.confirm("删除此运行的本地记录、输入和产物？飞书交付文档不会删除。")) return;
    setBusy(true);
    clearError();
    try {
      await api(`/api/runs/${state.runId}`, { method: "DELETE" });
      state.runId = null;
      state.view = null;
      state.review = ReviewState.createReviewState();
      globalThis.location.reload();
    } catch (error) {
      showError(error);
      setBusy(false);
    }
  });
  discardButton.addEventListener("click", () => {
    state.review = ReviewState.discardLocalChanges(state.review);
    clearError();
    render(ReviewState.draftView(state.review));
  });
  scanBitableButton.addEventListener("click", scanBitableTasks);
  setInterval(() => poll(false), 1000);
  updateActionAvailability();
  configureModes();
})();
