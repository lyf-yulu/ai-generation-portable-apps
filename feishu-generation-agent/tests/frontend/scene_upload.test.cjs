"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const staticDir = path.resolve(
  __dirname,
  "../../src/feishu_generation_agent/web/static",
);
const appSource = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");

function referenceSectionSource() {
  const start = appSource.indexOf("function referenceSection(task)");
  assert.ok(start > 0, "找不到 referenceSection");
  const end = appSource.indexOf("\n  function ", start + 1);
  return appSource.slice(start, end > 0 ? end : undefined);
}

test("scene image upload is available to image tasks", () => {
  const section = referenceSectionSource();
  // 图片任务的 reference_mode 在领域层被强制为 multi_reference，
  // 所以上传入口对图片任务始终可用；这条断言锁死该前提。
  assert.match(section, /referenceMode === "multi_reference"/);
  assert.match(section, /增添素材/);
  assert.match(section, /type = "file"/);
});

test("upload accepts image formats", () => {
  const section = referenceSectionSource();
  assert.match(section, /accept = "image\/\*/);
});

test("upload lets the operator choose insertion order", () => {
  const section = referenceSectionSource();
  assert.match(section, /order\.type = "number"/);
  assert.match(section, /order\.min = "1"/);
});

test("image tasks are forced into multi_reference by the domain layer", () => {
  const planSource = fs.readFileSync(
    path.resolve(
      __dirname,
      "../../src/feishu_generation_agent/domain/plan.py",
    ),
    "utf8",
  );
  const imageBranch = planSource.slice(
    planSource.indexOf("if self.task_type is TaskType.IMAGE_TO_IMAGE:"),
    planSource.indexOf("if self.duration is None:"),
  );
  assert.ok(imageBranch.length > 0, "找不到图片校验分支");
  assert.match(imageBranch, /self\.reference_mode = "multi_reference"/);
});
