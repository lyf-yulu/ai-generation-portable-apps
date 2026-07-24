(function (root, factory) {
  const paths = factory();
  if (typeof module === "object" && module.exports) module.exports = paths;
  root.ApiPaths = paths;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  "use strict";

  const MOUNT_PATH = "/feishu-generation-agent";

  function basePath(pathname) {
    const path = typeof pathname === "string" ? pathname : "";
    return path === MOUNT_PATH || path.startsWith(`${MOUNT_PATH}/`)
      ? MOUNT_PATH
      : "";
  }

  function absolutePath(path) {
    return `/${String(path || "").replace(/^\/+/, "")}`;
  }

  function apiUrl(pathname, path) {
    return `${basePath(pathname)}${absolutePath(path)}`;
  }

  function assetUrl(pathname, path) {
    return `${basePath(pathname)}${absolutePath(path)}`;
  }

  return { basePath, apiUrl, assetUrl };
});
