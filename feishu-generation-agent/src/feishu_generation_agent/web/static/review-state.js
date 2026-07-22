(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ReviewState = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const CONFLICT_MESSAGE = "服务端计划已更新，请确认/刷新";

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function stableValue(value) {
    if (Array.isArray(value)) return value.map(stableValue);
    if (value && typeof value === "object") {
      return Object.keys(value).sort().reduce((result, key) => {
        result[key] = stableValue(value[key]);
        return result;
      }, {});
    }
    return value;
  }

  function serverIdentity(view) {
    const approval = view?.approval || {};
    return JSON.stringify(stableValue({
      status: view?.status || null,
      revision: approval.revision ?? null,
      tasks: approval.tasks || [],
      document_summary: approval.document_summary || "",
      media_assets: approval.media_assets || [],
      selected_task_ids: approval.selected_task_ids || [],
    }));
  }

  function taskIds(view) {
    return (view?.approval?.tasks || [])
      .map((task) => task?.task_id)
      .filter((taskId) => typeof taskId === "string");
  }

  function initialSelection(view) {
    const known = new Set(taskIds(view));
    const serverSelected = Array.isArray(view?.approval?.selected_task_ids)
      ? view.approval.selected_task_ids.filter((taskId) => known.has(taskId))
      : [];
    if (view?.status === "waiting_approval" && serverSelected.length === 0) {
      return [...known];
    }
    return serverSelected;
  }

  function createReviewState() {
    return {
      serverView: null,
      serverIdentity: null,
      editsByTaskId: {},
      selectedTaskIds: [],
      selectionDirty: false,
      conflict: "",
      pendingServerView: null,
      submitting: false,
      submitSnapshot: null,
    };
  }

  function adoptServerView(view) {
    const serverView = clone(view);
    return {
      ...createReviewState(),
      serverView,
      serverIdentity: serverIdentity(serverView),
      selectedTaskIds: initialSelection(serverView),
    };
  }

  function hasDirty(state) {
    return Boolean(state.selectionDirty || Object.keys(state.editsByTaskId).length);
  }

  function mergeServerView(state, view) {
    if (!state.serverView) return adoptServerView(view);
    const incoming = clone(view);
    const incomingIdentity = serverIdentity(incoming);
    if (incomingIdentity === state.serverIdentity) {
      return { ...state, serverView: incoming };
    }
    if (hasDirty(state) || state.submitting) {
      return {
        ...state,
        conflict: CONFLICT_MESSAGE,
        pendingServerView: incoming,
      };
    }
    return adoptServerView(incoming);
  }

  function assertEditable(state) {
    if (state.submitting) throw new Error("审批提交中，不能继续修改");
  }

  function setTaskSelected(state, taskId, selected) {
    assertEditable(state);
    if (!taskIds(state.serverView).includes(taskId)) {
      throw new Error(`未知任务：${taskId}`);
    }
    const selectedIds = new Set(state.selectedTaskIds);
    if (selected) selectedIds.add(taskId);
    else selectedIds.delete(taskId);
    return {
      ...state,
      selectedTaskIds: taskIds(state.serverView).filter((id) => selectedIds.has(id)),
      selectionDirty: true,
    };
  }

  function patchTask(state, taskId, patch) {
    assertEditable(state);
    if (!taskIds(state.serverView).includes(taskId)) {
      throw new Error(`未知任务：${taskId}`);
    }
    const safePatch = clone(patch || {});
    delete safePatch.task_id;
    return {
      ...state,
      editsByTaskId: {
        ...state.editsByTaskId,
        [taskId]: {
          ...(state.editsByTaskId[taskId] || {}),
          ...safePatch,
        },
      },
    };
  }

  function setReferenceMode(state, taskId, referenceMode) {
    assertEditable(state);
    if (!taskIds(state.serverView).includes(taskId)) {
      throw new Error(`未知任务：${taskId}`);
    }
    if (!['multi_reference', 'first_last_frame'].includes(referenceMode)) {
      throw new Error('参考模式无效');
    }
    const task = draftTasks(state).find((item) => item.task_id === taskId);
    if (!task) throw new Error(`未知任务：${taskId}`);
    const references = [...(task.reference_images || [])]
      .sort((left, right) => left.order - right.order);
    if (referenceMode === 'first_last_frame') {
      if (task.task_type !== 'image_to_video') {
        throw new Error('图生图只能使用多参考模式');
      }
      if (references.length !== 2) {
        throw new Error('首尾帧模式需要恰好两张图片');
      }
      return patchTask(state, taskId, {
        reference_mode: referenceMode,
        reference_images: references.map((reference, index) => ({
          ...reference,
          role: index === 0 ? 'first_frame' : 'last_frame',
        })),
      });
    }
    return patchTask(state, taskId, {
      reference_mode: referenceMode,
      reference_images: references.map((reference) => ({
        ...reference,
        role: 'reference_image',
      })),
    });
  }

  function draftTasks(state) {
    return (state.serverView?.approval?.tasks || []).map((task) => ({
      ...clone(task),
      ...clone(state.editsByTaskId[task.task_id] || {}),
      task_id: task.task_id,
    }));
  }

  function draftView(state) {
    if (!state.serverView) return null;
    const view = clone(state.serverView);
    view.approval = view.approval || {};
    view.approval.tasks = draftTasks(state);
    view.approval.selected_task_ids = [...state.selectedTaskIds];
    return view;
  }

  function selectedTaskIds(state) {
    return [...state.selectedTaskIds];
  }

  function canApprove(state) {
    return Boolean(
      state.serverView?.status === "waiting_approval"
      && !state.conflict
      && !state.submitting
      && state.selectedTaskIds.length,
    );
  }

  function buildApprovalPayload(state) {
    if (state.conflict) throw new Error(state.conflict);
    if (state.submitting) throw new Error("审批提交中，请勿重复提交");
    if (state.serverView?.status !== "waiting_approval") {
      throw new Error("当前运行不在等待审批状态");
    }
    if (state.selectedTaskIds.length === 0) {
      throw new Error("批准时必须选择至少一个任务");
    }
    return {
      action: "approve",
      selected_task_ids: [...state.selectedTaskIds],
      tasks: draftTasks(state),
    };
  }

  function deepFreeze(value) {
    if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
    Object.values(value).forEach(deepFreeze);
    return Object.freeze(value);
  }

  function beginApprovalSubmit(state) {
    const payload = deepFreeze(clone(buildApprovalPayload(state)));
    return {
      state: { ...state, submitting: true, submitSnapshot: payload },
      payload,
    };
  }

  function failApprovalSubmit(state) {
    return { ...state, submitting: false, submitSnapshot: null };
  }

  function completeApprovalSubmit(state) {
    if (state.pendingServerView) return adoptServerView(state.pendingServerView);
    return adoptServerView(state.serverView);
  }

  function discardLocalChanges(state) {
    return adoptServerView(state.pendingServerView || state.serverView);
  }

  function conflictMessage(state) {
    return state.conflict || "";
  }

  function isSubmitting(state) {
    return Boolean(state.submitting);
  }

  function canSaveReferences(state, taskId) {
    if (state.submitting || state.conflict || state.selectionDirty) return false;
    return Object.entries(state.editsByTaskId).every(([editedTaskId, patch]) => (
      editedTaskId === taskId
      && Object.keys(patch).every((fieldName) => (
        fieldName === "reference_images" || fieldName === "reference_mode"
      ))
    ));
  }

  return {
    CONFLICT_MESSAGE,
    beginApprovalSubmit,
    buildApprovalPayload,
    canApprove,
    canSaveReferences,
    completeApprovalSubmit,
    conflictMessage,
    createReviewState,
    discardLocalChanges,
    draftView,
    failApprovalSubmit,
    hasDirty,
    isSubmitting,
    mergeServerView,
    patchTask,
    selectedTaskIds,
    setReferenceMode,
    setTaskSelected,
  };
});
