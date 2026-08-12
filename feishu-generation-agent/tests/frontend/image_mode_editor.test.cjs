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
const styles = fs.readFileSync(path.join(staticDir, "styles.css"), "utf8");

function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = styles.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  return match?.[1] || "";
}

test("image tasks expose provider picker with all three providers", () => {
  assert.match(appSource, /image_provider/);
  for (const provider of ["seedream", "banana", "gpt-image2"]) {
    assert.match(
      appSource,
      new RegExp(provider.replace(/[-]/g, "\\-")),
      `provider ${provider} 必须出现在前端选项里`,
    );
  }
});

test("image tasks expose size variants and safe area editors", () => {
  assert.match(appSource, /size_variants/);
  assert.match(appSource, /safe_area/);
  assert.match(appSource, /task-size-variants-editor/);
});

test("style preset buttons are rendered for image tasks", () => {
  assert.match(appSource, /STYLE_PRESETS/);
  assert.match(appSource, /厚涂/);
  assert.match(appSource, /3D/);
  assert.match(appSource, /task-style-presets/);
});

test("video-only controls stay out of the image branch", () => {
  const branchStart = appSource.indexOf('task.task_type === "image_to_image"');
  assert.ok(branchStart > 0, "找不到图片分支");
  const imageBranch = appSource.slice(
    branchStart,
    appSource.indexOf("} else {", branchStart),
  );
  assert.ok(imageBranch.length > 0, "图片分支为空");
  assert.doesNotMatch(imageBranch, /generate_audio/);
  assert.doesNotMatch(imageBranch, /视频时长/);
  assert.match(imageBranch, /providerPicker/);
  assert.match(imageBranch, /size_variants/);
});

test("style preset buttons have visible styling", () => {
  const rule = cssRule(".task-style-preset");
  assert.match(rule, /cursor:\s*pointer/);
});

test("size variants editor is styled as a multiline control", () => {
  const rule = cssRule(".task-size-variants-editor");
  assert.match(rule, /min-height/);
});
