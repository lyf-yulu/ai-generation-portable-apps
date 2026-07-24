(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PlannerPromptState = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function modeLabel(mode, version) {
    return mode === "personal" ? `使用个人版本 v${version}` : "使用 Prime";
  }

  function createPlannerPromptState() {
    return {
      loaded: false,
      editable: false,
      entryVisible: false,
      entryDisabled: true,
      mode: "prime",
      version: 0,
      modeLabel: "使用 Prime",
      promptText: "",
      savedPromptText: "",
      editorOpen: false,
      dirty: false,
      saving: false,
      saveDisabled: true,
      resetting: false,
      resetDisabled: true,
      statusMessage: "",
      statusType: "",
      closeConfirmationNeeded: false,
    };
  }

  function controls(state) {
    const disabled = !state.editable || state.saving || state.resetting;
    return {
      ...state,
      entryVisible: Boolean(state.editable),
      entryDisabled: !state.editable,
      saveDisabled: disabled,
      resetDisabled: disabled || state.mode !== "personal",
    };
  }

  function applyPlannerPromptResponse(state, payload) {
    const promptText = typeof payload?.prompt_text === "string" ? payload.prompt_text : "";
    const mode = payload?.mode === "personal" ? "personal" : "prime";
    const version = Number.isInteger(payload?.version) ? payload.version : 0;
    return controls({
      ...state,
      loaded: true,
      editable: payload?.editable === true,
      mode,
      version,
      modeLabel: modeLabel(mode, version),
      promptText,
      savedPromptText: promptText,
      dirty: false,
      saving: false,
      resetting: false,
      closeConfirmationNeeded: false,
    });
  }

  function openPromptEditor(state) {
    if (!state.editable) return state;
    return { ...state, editorOpen: true, closeConfirmationNeeded: false };
  }

  function beginPromptSave(state) {
    if (state.saving || state.resetting) throw new Error("提示词保存中，请勿重复提交");
    if (!state.editable) throw new Error("当前提示词不可编辑");
    return controls({
      ...state,
      editorOpen: true,
      saving: true,
      statusMessage: "正在保存个人版本…",
      statusType: "loading",
    });
  }

  function finishPromptSave(state, payload) {
    return controls({
      ...applyPlannerPromptResponse(state, payload),
      editorOpen: true,
      statusMessage: "个人版本已保存",
      statusType: "success",
    });
  }

  function failPromptSave(state, message) {
    return controls({
      ...state,
      editorOpen: true,
      saving: false,
      statusMessage: String(message || "保存失败，请重试"),
      statusType: "error",
    });
  }

  function beginPromptReset(state) {
    if (state.saving || state.resetting) throw new Error("提示词操作中，请勿重复提交");
    if (!state.editable || state.mode !== "personal") throw new Error("当前没有个人版本可恢复");
    return controls({
      ...state,
      editorOpen: true,
      resetting: true,
      statusMessage: "正在恢复 Prime…",
      statusType: "loading",
    });
  }

  function finishPromptReset(state, payload) {
    return controls({
      ...applyPlannerPromptResponse(state, payload),
      editorOpen: true,
      statusMessage: "已恢复 Prime",
      statusType: "success",
    });
  }

  function failPromptReset(state, message) {
    return controls({
      ...state,
      editorOpen: true,
      resetting: false,
      statusMessage: String(message || "恢复 Prime 失败，请重试"),
      statusType: "error",
    });
  }

  function markPromptDirty(state, promptText) {
    const value = String(promptText ?? "");
    return controls({
      ...state,
      promptText: value,
      dirty: value !== state.savedPromptText,
      statusMessage: "",
      statusType: "",
      closeConfirmationNeeded: false,
    });
  }

  function requestPromptEditorClose(state) {
    if (state.saving || state.resetting) return state;
    if (state.dirty) return { ...state, closeConfirmationNeeded: true };
    return { ...state, editorOpen: false, closeConfirmationNeeded: false };
  }

  function discardPromptEditorChanges(state) {
    return controls({
      ...state,
      promptText: state.savedPromptText,
      dirty: false,
      editorOpen: false,
      closeConfirmationNeeded: false,
      statusMessage: "",
      statusType: "",
    });
  }

  return {
    createPlannerPromptState,
    applyPlannerPromptResponse,
    openPromptEditor,
    beginPromptSave,
    finishPromptSave,
    failPromptSave,
    beginPromptReset,
    finishPromptReset,
    failPromptReset,
    markPromptDirty,
    requestPromptEditorClose,
    discardPromptEditorChanges,
  };
});
