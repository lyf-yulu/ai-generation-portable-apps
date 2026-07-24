(() => {
  "use strict";

  const ReviewState = globalThis.ReviewState;
  if (!ReviewState) throw new Error("审批草稿状态模块加载失败");
  const BitableState = globalThis.BitableState;
  if (!BitableState) throw new Error("多维表格状态模块加载失败");
  const ReferenceUploadState = globalThis.ReferenceUploadState;
  if (!ReferenceUploadState) throw new Error("参考图片上传状态模块加载失败");
  const PlannerPromptState = globalThis.PlannerPromptState;
  const ApiPaths = globalThis.ApiPaths;

  const state = {
    runId: null,
    view: null,
    busy: false,
    runMode: null,
    pollTimer: null,
    modes: { bitable: false, legacy_delivery: false },
    bitable: BitableState.createState(),
    review: ReviewState.createReviewState(),
    referenceUploads: ReferenceUploadState.createState(),
    plannerPrompt: PlannerPromptState?.createPlannerPromptState?.() || null,
  };
  const byId = (id) => document.getElementById(id);
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
  const animationCategoryTab = byId("animation-category-tab");
  const portraitCategoryTab = byId("portrait-category-tab");
  const categoryTabs = [animationCategoryTab, portraitCategoryTab];
  const bitableTaskList = byId("bitable-task-list");
  const bitableStatus = byId("bitable-status");
  const recentRunList = byId("recent-run-list");
  const nextTaskButton = byId("next-task-button");
  const rerunButton = byId("rerun-button");
  const pollingNote = byId("polling-note");
  const plannerPromptEntry = byId("planner-prompt-entry");
  const plannerPromptButton = byId("planner-prompt-button");
  const plannerPromptMode = byId("planner-prompt-mode");
  const plannerPromptModal = byId("planner-prompt-modal");
  const plannerPromptModalMode = byId("planner-prompt-modal-mode");
  const plannerPromptText = byId("planner-prompt-text");
  const plannerPromptSave = byId("planner-prompt-save");
  const plannerPromptReset = byId("planner-prompt-reset");
  const plannerPromptFeedback = byId("planner-prompt-feedback");
  const TERMINAL_RUN_STATUSES = new Set([
    "succeeded", "completed_with_errors", "failed", "cancelled", "delivery_failed",
  ]);
  const RERUNNABLE_RUN_STATUSES = new Set([
    "succeeded", "completed_with_errors", "failed", "cancelled",
  ]);

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

  function agentUrl(path) {
    if (/^(?:https?:|blob:)/i.test(path)) return path;
    return ApiPaths
      ? ApiPaths.apiUrl(globalThis.location?.pathname || "/", path)
      : path;
  }

  async function api(url, options = {}) {
    const response = await fetch(agentUrl(url), options);
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

  function renderPlannerPrompt() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    const prompt = state.plannerPrompt;
    plannerPromptEntry.hidden = !prompt.entryVisible;
    plannerPromptButton.disabled = prompt.entryDisabled;
    plannerPromptMode.textContent = prompt.modeLabel;
    plannerPromptModal.hidden = !prompt.editorOpen;
    plannerPromptModalMode.textContent = prompt.modeLabel;
    if (plannerPromptText.value !== prompt.promptText) {
      plannerPromptText.value = prompt.promptText;
    }
    plannerPromptText.disabled = !prompt.editable || prompt.saving || prompt.resetting;
    plannerPromptSave.disabled = prompt.saveDisabled;
    plannerPromptReset.disabled = prompt.resetDisabled;
    plannerPromptFeedback.textContent = prompt.statusMessage;
    plannerPromptFeedback.className = `planner-prompt-feedback${prompt.statusType ? ` is-${prompt.statusType}` : ""}`;
  }

  async function loadPlannerPrompt() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    try {
      const payload = await api("/api/planner-prompt");
      state.plannerPrompt = PlannerPromptState.applyPlannerPromptResponse(
        state.plannerPrompt, payload,
      );
    } catch (error) {
      state.plannerPrompt = {
        ...state.plannerPrompt,
        statusMessage: error.message,
        statusType: "error",
      };
    }
    renderPlannerPrompt();
  }

  async function savePlannerPrompt() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    if (state.plannerPrompt.saving || state.plannerPrompt.resetting) return;
    try {
      state.plannerPrompt = PlannerPromptState.beginPromptSave(state.plannerPrompt);
      renderPlannerPrompt();
      const payload = await api("/api/planner-prompt", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt_text: state.plannerPrompt.promptText }),
      });
      state.plannerPrompt = PlannerPromptState.finishPromptSave(state.plannerPrompt, payload);
    } catch (error) {
      state.plannerPrompt = PlannerPromptState.failPromptSave(
        state.plannerPrompt, error.message,
      );
    }
    renderPlannerPrompt();
  }

  async function resetPlannerPrompt() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    if (state.plannerPrompt.saving || state.plannerPrompt.resetting) return;
    if (!globalThis.confirm("恢复 Prime 将删除当前个人版本，是否继续？")) return;
    try {
      state.plannerPrompt = PlannerPromptState.beginPromptReset(state.plannerPrompt);
      renderPlannerPrompt();
      const payload = await api("/api/planner-prompt", { method: "DELETE" });
      state.plannerPrompt = PlannerPromptState.finishPromptReset(state.plannerPrompt, payload);
    } catch (error) {
      state.plannerPrompt = PlannerPromptState.failPromptReset(
        state.plannerPrompt, error.message,
      );
    }
    renderPlannerPrompt();
  }

  function closePlannerPromptEditor() {
    if (!PlannerPromptState || !state.plannerPrompt) return;
    const next = PlannerPromptState.requestPromptEditorClose(state.plannerPrompt);
    if (next.closeConfirmationNeeded) {
      if (!globalThis.confirm("尚有未保存的提示词，确定放弃这些修改吗？")) {
        state.plannerPrompt = { ...next, closeConfirmationNeeded: false };
      } else {
        state.plannerPrompt = PlannerPromptState.discardPromptEditorChanges(next);
      }
    } else {
      state.plannerPrompt = next;
    }
    renderPlannerPrompt();
  }

  function setBusy(value) {
    state.busy = value;
    scanBitableButton.disabled = value || !state.modes.bitable;
    categoryTabs.forEach((tab) => {
      tab.disabled = value || !state.modes.bitable;
    });
    bitableTaskList.querySelectorAll("button").forEach((control) => {
      control.disabled = value;
    });
    updateActionAvailability();
  }

  function stopPolling() {
    if (state.pollTimer !== null) globalThis.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function startPolling() {
    stopPolling();
    if (!state.runId || TERMINAL_RUN_STATUSES.has(state.view?.status)) return;
    state.pollTimer = globalThis.setInterval(() => poll(false), 1000);
  }

  function renderBitableTasks() {
    const categoryState = BitableState.activeCategoryState(state.bitable);
    const scan = categoryState.scan;
    const tasks = categoryState.tasks;
    const activeCategory = state.bitable.activeCategory;
    categoryTabs.forEach((tab) => {
      const isActive = tab.dataset.category === activeCategory;
      tab.classList.toggle("is-active", isActive);
      tab.setAttribute("aria-selected", String(isActive));
      tab.disabled = state.busy || !state.modes.bitable;
    });
    if (scan.phase === "loading") bitableStatus.textContent = "正在读取多维表格…";
    else if (scan.phase === "error") bitableStatus.textContent = scan.error;
    else if (
      state.bitable.claim.phase === "conflict"
      && state.bitable.claim.category === activeCategory
    ) {
      bitableStatus.textContent = state.bitable.claim.error;
    } else if (scan.phase === "ready") {
      bitableStatus.textContent = tasks.length
        ? `发现 ${tasks.length} 条可处理任务，请手动选择一条。`
        : "当前没有需求附件可读且进度符合规则的可处理任务。";
    }

    const nodes = tasks.map((task) => {
      const card = element("article", "bitable-task");
      const identity = element("div", "");
      identity.append(element("h3", "", task.display_text || task.record_id));
      if (Object.hasOwn(task, "progress")) {
        identity.append(
          element("p", "bitable-task-meta", `进度：${task.progress || "—"}`),
          element("p", "bitable-task-meta", `类型：${task.task_type || "未分类"}`),
          element("p", "bitable-task-meta", `制作人：${task.maker_name || "未填写"}`),
        );
        if (!task.deliverable && task.delivery_block_reason) {
          identity.append(element("p", "bitable-task-warning", task.delivery_block_reason));
        }
      } else {
        const executors = task.executor_names?.length
          ? task.executor_names.join("、")
          : task.executor_open_ids?.length
          ? task.executor_open_ids.join("、")
          : "未指定";
        identity.append(element("p", "bitable-task-meta", `执行人：${executors}`));
      }
      const link = element("a", "", "查看需求来源");
      link.href = task.source_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      const claim = element("button", "primary", "开始分析");
      claim.type = "button";
      claim.disabled = state.busy || state.bitable.claim.phase === "loading" || task.deliverable === false;
      claim.addEventListener("click", () => claimBitableTask(task.record_id));
      card.append(identity, link, claim);
      return card;
    });
    if (scan.phase === "ready" && nodes.length === 0) {
      nodes.push(element("p", "bitable-empty", "没有可领取任务。"));
    }
    bitableTaskList.replaceChildren(...nodes);
    renderRecentRuns();
  }

  function renderRecentRuns() {
    const runs = state.bitable.recentRuns || [];
    const nodes = runs.map((run) => {
      const row = element("article", "recent-run");
      const details = element("div", "");
      details.append(
        element("strong", "", run.display_text || run.run_id),
        element("p", "bitable-task-meta", `状态：${run.status || "—"}`),
      );
      const actions = element("div", "recent-run-actions");
      const view = element("button", "quiet-button", "查看详情");
      view.type = "button";
      view.disabled = state.busy;
      view.addEventListener("click", () => viewRecentRun(run.run_id));
      actions.append(view);
      if (run.result_table_url) {
        const link = element("a", "", "结果表");
        link.href = run.result_table_url;
        link.target = "_blank";
        link.rel = "noreferrer";
        actions.append(link);
      }
      if (run.rerunnable) {
        const rerun = element("button", "quiet-button", "重跑");
        rerun.type = "button";
        rerun.disabled = state.busy;
        rerun.addEventListener("click", () => rerunBitableTask(run.run_id));
        actions.append(rerun);
      }
      row.append(details, actions);
      return row;
    });
    if (!nodes.length) nodes.push(element("p", "bitable-empty", "暂无已完成任务。"));
    recentRunList.replaceChildren(...nodes);
  }

  async function loadRecentRuns() {
    if (!state.modes.bitable) return;
    try {
      const runs = await api("/api/bitable/recent-runs");
      state.bitable = BitableState.recentSucceeded(state.bitable, runs);
      renderRecentRuns();
    } catch (error) {
      showError(error);
    }
  }

  async function scanBitableTasks() {
    if (state.busy || !state.modes.bitable) return;
    const category = state.bitable.activeCategory;
    state.bitable = BitableState.scanStarted(state.bitable, category);
    renderBitableTasks();
    setBusy(true);
    clearError();
    try {
      const tasks = await api(
        `/api/bitable/tasks?category=${encodeURIComponent(category)}`,
      );
      state.bitable = BitableState.scanSucceeded(state.bitable, category, tasks);
    } catch (error) {
      state.bitable = BitableState.scanFailed(state.bitable, category, error.message);
    } finally {
      setBusy(false);
      renderBitableTasks();
    }
  }

  async function claimBitableTask(recordId) {
    if (state.busy) return;
    const category = state.bitable.activeCategory;
    state.bitable = BitableState.claimStarted(state.bitable, recordId, category);
    renderBitableTasks();
    setBusy(true);
    clearError();
    try {
      const created = await api(
        `/api/bitable/tasks/${encodeURIComponent(recordId)}/claim`
          + `?category=${encodeURIComponent(category)}`,
        { method: "POST" },
      );
      state.bitable = BitableState.claimSucceeded(state.bitable, created.run_id);
      state.runId = created.run_id;
      state.runMode = "bitable";
      state.review = ReviewState.createReviewState();
      state.referenceUploads = ReferenceUploadState.createState();
      await poll(true);
      startPolling();
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      state.bitable = error.status === 409
        ? BitableState.claimConflict(state.bitable, error.message)
        : BitableState.claimConflict(state.bitable, error.message);
    } finally {
      setBusy(false);
      renderBitableTasks();
    }
  }

  async function selectBitableCategory(category) {
    if (
      state.busy
      || !state.modes.bitable
      || category === state.bitable.activeCategory
    ) return;
    state.bitable = BitableState.selectCategory(state.bitable, category);
    renderBitableTasks();
    if (BitableState.activeCategoryState(state.bitable).scan.phase === "idle") {
      await scanBitableTasks();
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
    categoryTabs.forEach((tab) => {
      tab.disabled = !state.modes.bitable;
    });
    if (!state.modes.bitable) {
      bitableStatus.textContent = "多维表格尚未配置，请先补全表格链接、数据表和视图。";
    }
    if (state.modes.bitable && !state.runId) {
      await loadRecentRuns();
      try {
        const activeRuns = await api("/api/bitable/active-runs");
        const latest = Array.isArray(activeRuns) ? activeRuns.at(-1) : null;
        if (latest?.run_id) {
          state.runId = latest.run_id;
          state.runMode = "bitable";
          state.review = ReviewState.createReviewState();
          await poll(true);
          startPolling();
          bitableStatus.textContent = `已恢复进行中任务：${latest.display_text || latest.run_id}`;
          document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
        }
      } catch (error) {
        showError(error);
      }
    }
    if (
      state.modes.bitable
      && state.bitable.activeCategory === "animation"
      && BitableState.activeCategoryState(state.bitable).scan.phase === "idle"
    ) {
      await scanBitableTasks();
    }
  }

  function updateActionAvailability() {
    const canReview = state.view && state.view.status === "waiting_approval";
    const conflict = ReviewState.conflictMessage(state.review);
    rejectButton.disabled = state.busy || !canReview;
    cancelButton.disabled = state.busy || !canReview;
    approveButton.disabled = state.busy || !ReviewState.canApprove(state.review);
    retryDeliveryButton.disabled = state.busy || state.view?.status !== "delivery_failed";
    const terminal = TERMINAL_RUN_STATUSES.has(state.view?.status);
    nextTaskButton.disabled = state.busy || !terminal;
    rerunButton.disabled = state.busy
      || state.runMode !== "bitable"
      || !RERUNNABLE_RUN_STATUSES.has(state.view?.status);
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
    if (value >= 60_000) {
      const minutes = Math.floor(value / 60_000);
      const seconds = Math.floor((value % 60_000) / 1000);
      return `${minutes} 分 ${String(seconds).padStart(2, "0")} 秒`;
    }
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

  function updateReferenceMode(taskId, referenceMode) {
    try {
      state.review = ReviewState.setReferenceMode(state.review, taskId, referenceMode);
      state.view = ReviewState.draftView(state.review);
      updateActionAvailability();
      render();
    } catch (error) {
      showError(error);
      render();
    }
  }

  async function prepareReferenceMutation(task) {
    const directive = typeof ReviewState.referenceMutationDirective === "function"
      ? ReviewState.referenceMutationDirective(state.review, task.task_id)
      : !ReviewState.hasDirty(state.review)
        ? "proceed"
        : ReviewState.canSaveReferences(state.review, task.task_id)
          ? "save_then_proceed"
          : "blocked";
    if (directive === "proceed") return true;
    if (directive === "save_then_proceed") return patchReferences(task);
    showError(new Error("请先提交或放弃提示词、任务选择等本地编辑，再增添、替换或删除参考图片"));
    return false;
  }

  async function patchReferences(task) {
    if (!ReviewState.canSaveReferences(state.review, task.task_id)) {
      showError(new Error("请先处理其他本地任务编辑，再保存参考图片用途与顺序"));
      return false;
    }
    const current = currentTask(task.task_id);
    const references = current?.reference_images || [];
    return mutate(`/api/runs/${state.runId}/tasks/${encodeURIComponent(task.task_id)}/references`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        references,
        reference_mode: current?.reference_mode || "multi_reference",
      }),
    }, true);
  }

  async function uploadReference(task, file, role, order, replacesAssetId = null) {
    if (!await prepareReferenceMutation(task)) return;
    if (!file) {
      showError(new Error("请选择图片文件"));
      return false;
    }
    if (task.reference_mode === "first_last_frame" && !replacesAssetId) {
      showError(new Error("首尾帧模式只能保留两张图片；请先切换到多参考模式再增添图片"));
      return false;
    }
    const body = new FormData();
    body.append("file", file);
    body.append("task_id", task.task_id);
    body.append("role", role);
    body.append("order", String(order));
    if (replacesAssetId) body.append("replaces_asset_id", replacesAssetId);
    return mutate(`/api/runs/${state.runId}/references`, { method: "POST", body }, true);
  }

  async function unlinkReference(task, assetId) {
    if (!await prepareReferenceMutation(task)) return;
    return mutate(
      `/api/runs/${state.runId}/tasks/${encodeURIComponent(task.task_id)}/references/${encodeURIComponent(assetId)}`,
      { method: "DELETE" },
      true,
    );
  }

  async function mutate(url, options, resetDraft = false) {
    if (state.busy) return false;
    setBusy(true);
    clearError();
    try {
      await api(url, options);
      await poll(true, resetDraft);
      return true;
    } catch (error) {
      showError(error);
      return false;
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

    const isVideo = reference.role === "reference_video";
    const isAudio = reference.role === "reference_audio";
    const image = document.createElement(isVideo ? "video" : isAudio ? "audio" : "img");
    image.alt = `参考素材 ${reference.order}`;
    if (isVideo) {
      image.muted = true;
      image.controls = true;
      image.preload = "metadata";
      image.playsInline = true;
    }
    if (isAudio) {
      image.controls = true;
      image.preload = "metadata";
    }
    if (asset?.preview_url) image.src = agentUrl(asset.preview_url);

    const descriptionText = description
      ? [description.subjects?.join("、"), description.scene, description.probable_role]
          .filter(Boolean)
          .join(" · ")
      : "本地新增图片，尚无视觉描述";
    const descriptionNode = element("div", "reference-description", descriptionText);

    const role = element(
      "div",
      "reference-role",
      reference.role === "first_frame"
        ? "首帧"
        : reference.role === "last_frame"
          ? "尾帧"
          : isVideo ? "参考视频" : isAudio ? "参考音频" : "普通参考图",
    );

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
    replaceInput.accept = "image/*,video/mp4,video/webm,audio/mpeg,audio/wav,audio/ogg,audio/aac";
    replaceInput.hidden = true;
    const replace = element("button", "quiet-button", "替换");
    replace.type = "button";
    replace.addEventListener("click", () => replaceInput.click());
    replaceInput.addEventListener("change", () => {
      uploadReference(
        task,
        replaceInput.files[0],
        reference.role,
        Number(order.value),
        reference.asset_id,
      );
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
    heading.append(element("h3", "", "参考素材"));
    const referenceMode = task.reference_mode || "multi_reference";
    const mode = document.createElement("select");
    mode.setAttribute("aria-label", "参考模式");
    [
      ["multi_reference", "多参考模式"],
      ["first_last_frame", "首尾帧模式"],
    ].forEach(([value, label]) => {
      const option = element("option", "", label);
      option.value = value;
      option.selected = value === referenceMode;
      mode.append(option);
    });
    mode.addEventListener("change", () => updateReferenceMode(task.task_id, mode.value));
    heading.append(mode);
    const save = element("button", "quiet-button", "保存用途与顺序");
    save.type = "button";
    save.addEventListener("click", () => patchReferences(task));
    heading.append(save);
    const list = element("div", "reference-list");
    [...task.reference_images]
      .sort((a, b) => a.order - b.order)
      .forEach((reference) => list.append(referenceRow(task, reference)));

    const modeHint = element(
      "p",
      "mode-message",
      referenceMode === "first_last_frame"
        ? "首尾帧模式仅提交两张图片：首帧和尾帧。"
        : "多参考模式支持图片、视频和音频；首尾效果请在提示词中描述。",
    );
    section.append(heading, modeHint, list);
    if (referenceMode === "multi_reference") {
      const upload = element("div", "upload-row");
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = "image/*,video/mp4,video/webm,audio/mpeg,audio/wav,audio/ogg,audio/aac";
      const feedback = ReferenceUploadState.feedback(state.referenceUploads, task.task_id);
      const uploadFeedback = element(
        "p",
        `upload-feedback${feedback ? ` is-${feedback.phase}` : ""}`,
        feedback?.message || "请选择图片、视频或音频后再上传。",
      );
      uploadFeedback.setAttribute("aria-live", "polite");
      const order = document.createElement("input");
      order.type = "number";
      order.min = "1";
      order.value = String(task.reference_images.length + 1);
      const add = element("button", "secondary", "增添素材");
      add.type = "button";
      fileInput.addEventListener("change", () => {
        const file = fileInput.files[0];
        if (!file) return;
        state.referenceUploads = ReferenceUploadState.fileSelected(
          state.referenceUploads,
          task.task_id,
          file,
        );
        uploadFeedback.className = "upload-feedback is-selected";
        uploadFeedback.textContent = ReferenceUploadState.feedback(
          state.referenceUploads,
          task.task_id,
        ).message;
      });
      add.addEventListener("click", async () => {
        const file = ReferenceUploadState.pendingFile(state.referenceUploads, task.task_id);
        if (!file) {
          const message = "请先选择图片、视频或音频文件";
          state.referenceUploads = ReferenceUploadState.uploadFailed(
            state.referenceUploads, task.task_id, message,
          );
          uploadFeedback.className = "upload-feedback is-error";
          uploadFeedback.textContent = message;
          showError(new Error(message));
          return;
        }
        state.referenceUploads = ReferenceUploadState.uploadStarted(state.referenceUploads, task.task_id);
        uploadFeedback.className = "upload-feedback is-uploading";
        uploadFeedback.textContent = ReferenceUploadState.feedback(
          state.referenceUploads, task.task_id,
        ).message;
        add.disabled = true;
        add.textContent = "正在添加…";
        let succeeded = false;
        try {
          const role = file.type.startsWith("video/") ? "reference_video" : file.type.startsWith("audio/") ? "reference_audio" : "reference_image";
          succeeded = await uploadReference(task, file, role, Number(order.value));
        } catch (error) {
          showError(error);
        }
        state.referenceUploads = succeeded
          ? ReferenceUploadState.uploadSucceeded(state.referenceUploads, task.task_id)
          : ReferenceUploadState.uploadFailed(
            state.referenceUploads,
            task.task_id,
            errorMessage.textContent || "图片添加失败，请重试",
          );
        render(ReviewState.draftView(state.review));
      });
      upload.append(fileInput, order, add, uploadFeedback);
      section.append(upload);
    }
    return section;
  }

  function renderCoverage(view) {
    const coverage = ReviewState.assetCoverage(view);
    byId("coverage-label").textContent = ReviewState.coverageLabel(view);
    const details = [
      `已排除 ${coverage.excluded_count} 张`,
      `未覆盖 ${coverage.uncovered_count} 张`,
      `读取失败 ${coverage.failed_count} 张`,
    ];
    byId("coverage-detail").textContent = details.join(" · ");
    const rows = ReviewState.excludedAssetRows(view).map((item) => {
      const row = element("div", "excluded-asset-row");
      let preview;
      if (item.media_kind === "image") {
        preview = document.createElement("img");
        preview.alt = `排除素材 ${item.asset_id}`;
      } else if (item.media_kind === "video") {
        preview = document.createElement("video");
        preview.controls = true;
        preview.preload = "metadata";
        preview.muted = true;
        preview.playsInline = true;
        preview.setAttribute("aria-label", `排除视频 ${item.asset_id}`);
      } else if (item.media_kind === "audio") {
        preview = document.createElement("audio");
        preview.controls = true;
        preview.preload = "metadata";
        preview.setAttribute("aria-label", `排除音频 ${item.asset_id}`);
      } else {
        preview = element(
          "div",
          "excluded-asset-placeholder",
          item.mime_type || "附件",
        );
      }
      if (item.preview_url && item.media_kind !== "file") {
        preview.src = agentUrl(item.preview_url);
      }
      const content = element("div", "excluded-asset-copy");
      content.append(
        element("strong", "", item.asset_id),
        element("p", "", item.reason),
      );
      row.append(preview, content);
      return row;
    });
    if (!rows.length) {
      rows.push(element("p", "mode-message", "暂无排除素材。"));
    }
    byId("excluded-asset-list").replaceChildren(...rows);
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
    byId("current-node").textContent = BitableState.runStage(view) || latestEvent?.node || "—";
    byId("run-duration").textContent = formatDuration(BitableState.runElapsedMs(view));
    byId("document-title").textContent = view.approval.document_title || "未命名文档";
    byId("source-link").href = view.source_url;
    byId("document-revision").textContent = view.approval.revision ?? "—";
    byId("document-summary").textContent = view.approval.document_summary || "";
    const deliveryTarget = byId("delivery-target");
    const delivery = view.delivery || {};
    deliveryTarget.replaceChildren();
    if (delivery.target_type === "production_result_record" && delivery.result_table_url) {
      const link = element("a", "", "打开结果表");
      link.href = delivery.result_table_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      deliveryTarget.append("生成结果已写入：", link);
      deliveryTarget.hidden = false;
    } else {
      deliveryTarget.hidden = true;
    }
    byId("langsmith-warning").hidden = !view.privacy?.langsmith_tracing;
    renderEvents(view.events);

    const blockingIngestIssues = view.approval.blocking_ingest_issues || [];
    const issues = (view.approval.validation_issues || [])
      .filter((issue) => !blockingIngestIssues.includes(issue));
    const issueBox = byId("validation-issues");
    issueBox.textContent = issues.join("；");
    issueBox.hidden = issues.length === 0;
    const blockingIngestBox = byId("blocking-ingest-issues");
    blockingIngestBox.textContent = blockingIngestIssues.length
      ? `文档读取阻塞：${blockingIngestIssues.join("；")}`
      : "";
    blockingIngestBox.hidden = blockingIngestIssues.length === 0;
    const assetIngestIssues = view.approval.asset_ingest_issues || [];
    const assetIngestBox = byId("asset-ingest-issues");
    assetIngestBox.textContent = assetIngestIssues.length
      ? `素材读取失败（不影响其他素材）：${assetIngestIssues.join("；")}`
      : "";
    assetIngestBox.hidden = assetIngestIssues.length === 0;
    const visionIssues = view.approval.vision_issues || [];
    const visionIssueBox = byId("vision-issues");
    visionIssueBox.textContent = visionIssues.length
      ? `素材识别失败（不影响其他素材）：${visionIssues.join("；")}`
      : "";
    visionIssueBox.hidden = visionIssues.length === 0;
    renderCoverage(view);
    taskList.replaceChildren(...(view.approval.tasks || []).map(renderTask));
    updateActionAvailability();
  }

  async function poll(force = false, resetDraft = false) {
    if (!state.runId || (state.busy && !force)) return;
    try {
      const serverView = await api(`/api/runs/${state.runId}`);
      state.review = resetDraft
        ? ReviewState.mergeServerView(ReviewState.createReviewState(), serverView)
        : ReviewState.mergeServerView(state.review, serverView);
      render(ReviewState.draftView(state.review));
      if (TERMINAL_RUN_STATUSES.has(serverView.status)) {
        stopPolling();
        pollingNote.textContent = "任务已结束，可开始下一任务或重跑。";
        await loadRecentRuns();
      } else {
        pollingNote.textContent = "每 1 秒自动刷新运行状态";
      }
    } catch (error) {
      showError(error);
    }
  }

  async function viewRecentRun(runId) {
    if (state.busy) return;
    stopPolling();
    setBusy(true);
    clearError();
    try {
      state.runId = runId;
      state.runMode = "bitable";
      state.review = ReviewState.createReviewState();
      await poll(true);
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function resetForNextTask() {
    stopPolling();
    state.runId = null;
    state.runMode = null;
    state.view = null;
    state.review = ReviewState.createReviewState();
    state.referenceUploads = ReferenceUploadState.createState();
    state.bitable = BitableState.resetRunContext(state.bitable);
    byId("status-badge").textContent = "尚未创建";
    byId("run-status").textContent = "—";
    byId("thread-id").textContent = "—";
    byId("current-node").textContent = "—";
    byId("run-duration").textContent = "—";
    byId("document-title").textContent = "等待选择多维表格任务";
    byId("document-summary").textContent = "";
    byId("delivery-target").hidden = true;
    [
      "validation-issues",
      "blocking-ingest-issues",
      "asset-ingest-issues",
      "vision-issues",
    ].forEach((id) => {
      byId(id).textContent = "";
      byId(id).hidden = true;
    });
    byId("event-list").replaceChildren();
    taskList.replaceChildren();
    pollingNote.textContent = "请选择下一条任务开始分析";
    updateActionAvailability();
    await scanBitableTasks();
    await loadRecentRuns();
  }

  async function rerunBitableTask(runId = state.runId) {
    if (!runId || state.busy) return;
    setBusy(true);
    clearError();
    try {
      const created = await api(`/api/bitable/runs/${encodeURIComponent(runId)}/rerun`, {
        method: "POST",
      });
      state.runId = created.run_id;
      state.runMode = "bitable";
      state.review = ReviewState.createReviewState();
      state.referenceUploads = ReferenceUploadState.createState();
      await poll(true);
      startPolling();
      document.querySelector(".workspace")?.scrollIntoView({ behavior: "smooth" });
    } catch (error) {
      showError(error);
      await loadRecentRuns();
    } finally {
      setBusy(false);
      renderRecentRuns();
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

  byId("reject-button").addEventListener("click", () => submitDecision("reject"));
  byId("cancel-button").addEventListener("click", () => submitDecision("cancel"));
  byId("approve-button").addEventListener("click", () => submitDecision("approve"));
  nextTaskButton.addEventListener("click", resetForNextTask);
  rerunButton.addEventListener("click", () => rerunBitableTask());
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
  if (PlannerPromptState && plannerPromptButton) {
    plannerPromptButton.addEventListener("click", () => {
      state.plannerPrompt = PlannerPromptState.openPromptEditor(state.plannerPrompt);
      renderPlannerPrompt();
      plannerPromptText.focus();
    });
    byId("planner-prompt-close").addEventListener("click", closePlannerPromptEditor);
    plannerPromptModal.addEventListener("click", (event) => {
      if (event.target === plannerPromptModal) closePlannerPromptEditor();
    });
    plannerPromptText.addEventListener("input", () => {
      state.plannerPrompt = PlannerPromptState.markPromptDirty(
        state.plannerPrompt, plannerPromptText.value,
      );
      renderPlannerPrompt();
    });
    plannerPromptSave.addEventListener("click", savePlannerPrompt);
    plannerPromptReset.addEventListener("click", resetPlannerPrompt);
  }
  scanBitableButton.addEventListener("click", scanBitableTasks);
  categoryTabs.forEach((tab) => {
    tab.addEventListener("click", () => selectBitableCategory(tab.dataset.category));
  });
  updateActionAvailability();
  if (PlannerPromptState) {
    renderPlannerPrompt();
    loadPlannerPrompt();
  }
  configureModes();
})();
