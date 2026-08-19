"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const PlannerPromptState = require(
  "../../src/feishu_generation_agent/web/static/planner-prompt-state.js",
);

function response(overrides = {}) {
  return {
    mode: "prime",
    editable: true,
    prompt_text: "Prime 提示词",
    version: 0,
    source: "prime",
    ...overrides,
  };
}

test("Prime response renders 使用 Prime", () => {
  const state = PlannerPromptState.applyPlannerPromptResponse(
    PlannerPromptState.createPlannerPromptState(), response(),
  );

  assert.equal(state.modeLabel, "使用 Prime");
  assert.equal(state.entryVisible, true);
});

test("personal version 3 renders 使用个人版本 v3", () => {
  const state = PlannerPromptState.applyPlannerPromptResponse(
    PlannerPromptState.createPlannerPromptState(),
    response({ mode: "personal", source: "personal", version: 3, prompt_text: "个人提示词" }),
  );

  assert.equal(state.modeLabel, "使用个人版本 v3");
});

test("direct editable=false hides and disables the entry", () => {
  const state = PlannerPromptState.applyPlannerPromptResponse(
    PlannerPromptState.createPlannerPromptState(), response({ editable: false }),
  );

  assert.equal(state.entryVisible, false);
  assert.equal(state.entryDisabled, true);
});

test("saving disables duplicate clicks and surfaces success", () => {
  let state = PlannerPromptState.applyPlannerPromptResponse(
    PlannerPromptState.createPlannerPromptState(), response(),
  );
  state = PlannerPromptState.markPromptDirty(state, "我的个人提示词");
  state = PlannerPromptState.beginPromptSave(state);

  assert.equal(state.saving, true);
  assert.equal(state.saveDisabled, true);
  assert.throws(() => PlannerPromptState.beginPromptSave(state), /保存中/);

  state = PlannerPromptState.finishPromptSave(
    state,
    response({ mode: "personal", source: "personal", version: 1, prompt_text: "我的个人提示词" }),
  );
  assert.equal(state.saving, false);
  assert.equal(state.statusMessage, "个人版本已保存");
  assert.equal(state.dirty, false);
});

test("a 422 response leaves the editor open and shows the server error", () => {
  let state = PlannerPromptState.applyPlannerPromptResponse(
    PlannerPromptState.createPlannerPromptState(), response(),
  );
  state = PlannerPromptState.openPromptEditor(state);
  state = PlannerPromptState.markPromptDirty(state, "无效内容");
  state = PlannerPromptState.beginPromptSave(state);
  state = PlannerPromptState.failPromptSave(state, "提示词无效");

  assert.equal(state.editorOpen, true);
  assert.equal(state.promptText, "无效内容");
  assert.equal(state.statusMessage, "提示词无效");
});

test("reset restores returned Prime content", () => {
  let state = PlannerPromptState.applyPlannerPromptResponse(
    PlannerPromptState.createPlannerPromptState(),
    response({ mode: "personal", source: "personal", version: 3, prompt_text: "个人提示词" }),
  );
  state = PlannerPromptState.markPromptDirty(state, "尚未保存的编辑");
  state = PlannerPromptState.finishPromptReset(state, response({ prompt_text: "返回的 Prime 提示词" }));

  assert.equal(state.promptText, "返回的 Prime 提示词");
  assert.equal(state.modeLabel, "使用 Prime");
  assert.equal(state.dirty, false);
});

test("dirty text is not silently discarded when the modal closes", () => {
  let state = PlannerPromptState.applyPlannerPromptResponse(
    PlannerPromptState.createPlannerPromptState(), response(),
  );
  state = PlannerPromptState.openPromptEditor(state);
  state = PlannerPromptState.markPromptDirty(state, "未保存内容");
  state = PlannerPromptState.requestPromptEditorClose(state);

  assert.equal(state.editorOpen, true);
  assert.equal(state.closeConfirmationNeeded, true);
  assert.equal(state.promptText, "未保存内容");
});
