#!/bin/bash
# 选择性回退: 在 v2 内部切到某个 tag 的状态,而不是完整回滚到 v2 之前.
#
# 场景:
#   - 生产上 v2.3-dreamina-fastapi 用了几天, dreamina 稳定但 seedance 有问题
#     → rollback-to-tag.sh v2.1-seedance-fastapi (保留 seedance,但退回 dreamina 之前)
#
#   - v2.0 后新加的 fastapi 都要撤,只保留阶段 1 安全 fix + nano-banana
#     → rollback-to-tag.sh v2.0-nano-banana-fastapi
#
# 做什么:
#   1. 确认在生产 v2 目录里
#   2. 停 launchd
#   3. git checkout <tag>. 只切代码, 不动 state/outputs (它们是软链)
#   4. 重启 launchd
#
# 不做什么:
#   - 不动数据目录 (state/outputs 都是软链到备份, 里面用户数据不变)
#   - 不删除任何 tag / commit
#   - 不动生产以外的目录
#
# 用法:
#   bash rollback-to-tag.sh                        # 列出可用 tag
#   bash rollback-to-tag.sh v2.1-seedance-fastapi  # 切到该 tag

set -euo pipefail

PROD=/Users/260413a/ai-generation-portable-apps
PLIST=~/Library/LaunchAgents/com.ai-portal.plist

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

if [ ! -d "$PROD/.git" ]; then
  echo -e "${RED}Error: $PROD 不是 git 仓库,不能选择性回退${NC}"
  echo "  这可能意味着你还没跑过 deploy.sh, 或者跑的是 baseline v1"
  exit 1
fi

cd "$PROD"

# 列 tag
echo -e "${YELLOW}=== 可用 tag ===${NC}"
git tag -l -n1 | while read tag desc; do
  # 显示 tag 是哪个 commit
  commit=$(git rev-list -n 1 "$tag" 2>/dev/null | cut -c1-8)
  echo "  $tag  ($commit)  — $desc"
done

echo ""
echo -e "${YELLOW}=== 当前 HEAD ===${NC}"
current=$(git describe --tags --always 2>/dev/null || git rev-parse --short HEAD)
echo "  $current"

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo ""
  echo "用法: $0 <tag>"
  echo "  例: $0 v2.1-seedance-fastapi"
  exit 0
fi

if ! git rev-parse "$TARGET" >/dev/null 2>&1; then
  echo -e "${RED}Error: tag '$TARGET' 不存在${NC}"
  exit 1
fi

if [ "$current" = "$TARGET" ]; then
  echo -e "${GREEN}已经在 $TARGET, 无需切换${NC}"
  exit 0
fi

echo ""
echo -e "${YELLOW}将从 $current  →  $TARGET${NC}"
echo ""
echo "这会修改的文件 (git diff):"
git diff --stat "$TARGET..HEAD" | tail -10
echo ""

# 保护未 commit 修改
if [ -n "$(git status --porcelain)" ]; then
  echo -e "${RED}警告: 生产目录里有未 commit 修改:${NC}"
  git status --short
  echo ""
  read -p "这些修改将丢失! 输入 'yes-lose-changes' 继续: " CONFIRM
  [ "$CONFIRM" = "yes-lose-changes" ] || { echo "取消"; exit 0; }
  git stash -u --quiet
  echo "  未 commit 修改已 stash (可用 git stash pop 恢复)"
fi

read -p "确认切到 $TARGET? 输入 'yes' 继续: " CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "取消"; exit 0; }

echo ""
echo -e "${YELLOW}[1/3] 停 launchd${NC}"
launchctl unload "$PLIST" 2>/dev/null || true
sleep 2
for port in 9090 9089 8787 8797 8888 8891; do
  for pid in $(lsof -ti:$port 2>/dev/null); do kill -9 "$pid" 2>/dev/null || true; done
done

echo ""
echo -e "${YELLOW}[2/3] git checkout $TARGET${NC}"
# detached HEAD 是可接受的 — 每次回退都是 detached 状态
git checkout --quiet "$TARGET"
echo "  已切到 $TARGET"
git log --oneline -1

echo ""
echo -e "${YELLOW}[3/3] 启动 launchd${NC}"
launchctl load "$PLIST"
sleep 8

echo ""
echo -e "${YELLOW}=== 检查 ===${NC}"
for port in 9090 8787 8797 8888 8891; do
  if lsof -iTCP:$port -sTCP:LISTEN -P -n 2>/dev/null | grep -q ":$port"; then
    echo -e "${GREEN}  ✓ 端口 $port LISTEN${NC}"
  else
    echo -e "${RED}  ✗ 端口 $port 未 LISTEN${NC}"
  fi
done

echo ""
echo -e "${YELLOW}=== 各子应用版本 ===${NC}"
for pair in "8787 seedance" "8797 nano-banana" "8888 dreamina" "8891 volcengine-portrait"; do
  set -- $pair
  ver=$(curl -4 -s -m 3 http://127.0.0.1:$1/api/v1/meta 2>/dev/null | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('version','?'))" 2>/dev/null || echo "?")
  echo "  $2:$1  version=$ver"
done

echo ""
echo -e "${GREEN}=== 完成 ===${NC}"
echo ""
echo "  当前 HEAD: $(git describe --tags --always)"
echo "  再切别的 tag: bash $0 <tag>"
echo "  完全回到 pre-v2: bash $PROD/deploy/rollback.sh"
echo "  向前切到最新: cd $PROD && git checkout main"
