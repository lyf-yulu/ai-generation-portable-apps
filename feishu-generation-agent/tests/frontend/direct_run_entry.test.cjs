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
const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
const styles = fs.readFileSync(path.join(staticDir, "styles.css"), "utf8");

function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = styles.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  return match?.[1] || "";
}

test("direct document entry exists in markup", () => {
  assert.match(html, /id="direct-run-panel"/);
  assert.match(html, /id="direct-run-url"/);
  assert.match(html, /id="direct-run-mode"/);
  assert.match(html, /id="direct-run-button"/);
});

test("direct entry offers both planning modes", () => {
  const panel = html.slice(
    html.indexOf('id="direct-run-panel"'),
    html.indexOf("</section>", html.indexOf('id="direct-run-panel"')),
  );
  assert.match(panel, /value="image"/);
  assert.match(panel, /value="video"/);
});

test("app posts source url and planning mode to /api/runs", () => {
  assert.match(appSource, /"\/api\/runs"/);
  assert.match(appSource, /planning_mode/);
  assert.match(appSource, /source_url/);
});

test("direct run wires the submit handler", () => {
  assert.match(appSource, /directRunButton/);
  assert.match(appSource, /addEventListener\("click", startDirectRun\)/);
});

test("direct run panel is styled", () => {
  assert.ok(cssRule(".direct-run-panel").length > 0);
  assert.match(cssRule(".direct-run-form"), /display/);
});
