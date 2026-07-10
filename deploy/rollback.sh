#!/bin/bash
# 完整回滚: 把生产退回到 deploy.sh 前的状态.
#
# 做什么:
#   1. 停 launchd
#   2. 找到最近一次 deploy.sh 生成的备份 (-backup-<日期>) 或让你指定
#   3. mv 现生产到 v2-rollback-<日期> 保留 (万一你还想切回来)
#   4. mv 备份目录 恢复到生产路径
#   5. 恢复 plist 备份
#   6. reload launchd
#
# 数据完整: 用户 state/outputs/archives 因为是软链回备份,数据不丢.

set -euo pipefail

DATE=$(date +%Y-%m-%d-%H%M)
PROD=/Users/260413a/ai-generation-portable-apps
PLIST=~/Library/LaunchAgents/com.ai-portal.plist

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${YELLOW}=== 完整回滚脚本 ===${NC}"

# 1. 找最近备份
BACKUP=""
if [ -n "${1:-}" ]; then
  BACKUP="$1"
else
  BACKUP=$(ls -td /Users/260413a/ai-generation-portable-apps-backup-* 2>/dev/null | head -1 || true)
fi

if [ -z "$BACKUP" ] || [ ! -d "$BACKUP" ]; then
  echo -e "${RED}Error: 没找到备份目录${NC}"
  echo "可用备份:"
  ls -d /Users/260413a/ai-generation-portable-apps-backup-* 2>/dev/null || echo "  (无)"
  echo ""
  echo "用法: $0 [备份路径]"
  exit 1
fi

echo "  从: $BACKUP"
echo "  到: $PROD"

# 找 plist 备份
PLIST_BACKUP=$(ls -t "${PLIST}".bak-* 2>/dev/null | head -1 || true)
if [ -z "$PLIST_BACKUP" ]; then
  echo -e "${YELLOW}  警告: 没找到 plist 备份, 只回滚代码${NC}"
else
  echo "  plist 备份: $PLIST_BACKUP"
fi

echo ""
read -p "确认回滚? 输入 'yes' 继续: " CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "取消"; exit 0; }

echo ""
echo -e "${YELLOW}[1/5] 停 launchd${NC}"
launchctl unload "$PLIST" 2>/dev/null || true
sleep 2
for port in 9090 9089 8787 8797 8888 8891; do
  for pid in $(lsof -ti:$port 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null || true
  done
done

echo ""
echo -e "${YELLOW}[2/5] 保存当前 v2 为 v2-rollback-${DATE}${NC}"
V2_SAVED="/Users/260413a/ai-generation-portable-apps-v2-rollback-${DATE}"
if [ -e "$PROD" ]; then
  mv "$PROD" "$V2_SAVED"
  echo "  $PROD -> $V2_SAVED"
fi

echo ""
echo -e "${YELLOW}[3/5] 恢复备份代码${NC}"
mv "$BACKUP" "$PROD"
echo "  $BACKUP -> $PROD"

echo ""
echo -e "${YELLOW}[4/5] 恢复 plist${NC}"
if [ -n "$PLIST_BACKUP" ]; then
  cp "$PLIST_BACKUP" "$PLIST"
  echo "  已恢复"
else
  echo "  (跳过, 无备份)"
fi

echo ""
echo -e "${YELLOW}[5/5] 启动 launchd${NC}"
launchctl load "$PLIST"
sleep 8

echo ""
echo -e "${YELLOW}=== 检查 ===${NC}"
for port in 9090 8787 8797 8888; do
  if lsof -iTCP:$port -sTCP:LISTEN -P -n 2>/dev/null | grep -q ":$port"; then
    echo -e "${GREEN}  ✓ 端口 $port LISTEN${NC}"
  else
    echo -e "${RED}  ✗ 端口 $port 未 LISTEN${NC}"
  fi
done

echo ""
echo -e "${GREEN}=== 回滚完成 ===${NC}"
echo ""
echo "  v2 代码保存在: $V2_SAVED"
echo "  如需重新切换回 v2: 用 v2-rollback-<日期> 目录跑 deploy.sh"
echo "  访问: https://192.168.30.5:9090"
