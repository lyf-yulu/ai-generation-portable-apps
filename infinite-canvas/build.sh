#!/bin/sh
# 构建画布前端并产出 Portal 直接服务的 static/。
# 生产机（launchd）不跑这个脚本：static/ 提交进仓库，改前端后在开发机构建、提交、再重启。
set -eu
dir=$(cd "$(dirname "$0")" && pwd)
cd "$dir/web"
[ -d node_modules ] || npm ci
# VITE_BASE 必须与 portal/apps.json 里的挂载前缀一致。
VITE_BASE=/infinite-canvas/ npm run build
rm -rf "$dir/static"
cp -R dist "$dir/static"
echo "built -> $dir/static"
