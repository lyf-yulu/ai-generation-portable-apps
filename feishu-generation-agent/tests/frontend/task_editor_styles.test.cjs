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

test("task editors have stable resizable scrollable styles", () => {
  assert.match(appSource, /task-prompt-editor/);
  assert.match(appSource, /task-negative-editor/);

  const sharedRule = cssRule(".task-grid textarea");
  assert.match(sharedRule, /overflow-y:\s*auto/);
  assert.match(sharedRule, /resize:\s*vertical/);

  assert.match(
    cssRule(".task-prompt-editor"),
    /min-height:\s*18rem/,
  );
  assert.match(
    cssRule(".task-negative-editor"),
    /min-height:\s*10rem/,
  );
});
