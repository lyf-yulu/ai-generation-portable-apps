"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const paths = require(
  "../../src/feishu_generation_agent/web/static/api-paths.js",
);

test("uses the explicit Agent mount prefix when present", () => {
  assert.equal(paths.basePath("/"), "");
  assert.equal(paths.basePath("/feishu-generation-agent/"), "/feishu-generation-agent");
  assert.equal(
    paths.apiUrl("/feishu-generation-agent/", "/api/health"),
    "/feishu-generation-agent/api/health",
  );
  assert.equal(paths.assetUrl("/", "/static/styles.css"), "/static/styles.css");
});

test("does not guess a prefix from unrelated paths", () => {
  assert.equal(paths.basePath("/portal/"), "");
  assert.equal(paths.apiUrl("/portal/", "/api/health"), "/api/health");
});
