#!/bin/bash
# 把 v2 部署到生产 (launchd)。首次切换用。
#
# 做什么:
#   1. 备份当前生产代码目录到 <老目录>-backup-<日期>
#   2. 把 v2 目录 mv 到生产路径
#   3. 把老 state/outputs/archives/uploads 数据软链回来 (不动 25GB 用户数据)
#   4. 备份并改 launchd plist:
#      - Python: /usr/bin/python3 (3.9) → /opt/homebrew/bin/python3.12
#      - 加 DYLD_LIBRARY_PATH env
#      - 加 NANO_BANANA_ENGINE=fastapi, SEEDANCE_ENGINE=fastapi
#   5. launchctl reload 让守护进程重启
#
# 前提:
#   - /Users/260413a/ai-generation-portable-apps-v2 目录已就绪 (含 .venv)
#   - 已在 v2 里 pip install (已装好 fastapi/uvicorn/httpx/pillow 等)
#   - 同事此刻空闲 (切换有 <15s 断服)
#
# 回滚: bash rollback.sh (一键回到切换前状态)

set -euo pipefail

DATE=$(date +%Y-%m-%d-%H%M)
PROD=/Users/260413a/ai-generation-portable-apps
V2=/Users/260413a/ai-generation-portable-apps-v2
PROD_BACKUP="${PROD}-backup-${DATE}"
PLIST=~/Library/LaunchAgents/com.ai-portal.plist
PLIST_BACKUP="${PLIST}.bak-${DATE}"
LAUNCHD_LABEL=com.ai-portal

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${YELLOW}=== v2 部署脚本 ===${NC}"
echo "  DATE=$DATE"
echo "  PROD=$PROD"
echo "  V2=$V2"
echo "  备份到: $PROD_BACKUP"
echo ""

if [ ! -d "$V2" ]; then
  echo -e "${RED}Error: v2 目录不存在: $V2${NC}"
  exit 1
fi
if [ ! -x "$V2/.venv/bin/uvicorn" ]; then
  echo -e "${RED}Error: v2 venv 未装依赖. 先跑:${NC}"
  echo "  DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib $V2/.venv/bin/pip install -r $V2/requirements.txt"
  exit 1
fi
if [ -d "$PROD_BACKUP" ]; then
  echo -e "${RED}Error: 备份路径已存在: $PROD_BACKUP${NC}"
  exit 1
fi

read -p "确认切换? 输入 'yes' 继续: " CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "取消"; exit 0; }

echo ""
echo -e "${YELLOW}[1/5] 停 launchd 守护 (KeepAlive off 才能停)${NC}"
launchctl unload "$PLIST" 2>/dev/null || true
sleep 2
for port in 9090 9089 8787 8797 8888 8891; do
  for pid in $(lsof -ti:$port 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null || true
  done
done
echo "  端口清理完成"

echo ""
echo -e "${YELLOW}[2/5] 备份生产代码目录${NC}"
mv "$PROD" "$PROD_BACKUP"
echo "  $PROD -> $PROD_BACKUP"

echo ""
echo -e "${YELLOW}[3/5] 移动 v2 到生产路径${NC}"
mv "$V2" "$PROD"
echo "  $V2 -> $PROD"

echo ""
echo -e "${YELLOW}[4/5] 软链 25GB 用户数据 (state/outputs/archives/uploads/accounts)${NC}"
for app in seedance nano-banana dreamina volcengine-portrait; do
  for d in state outputs archives uploads accounts; do
    src="$PROD_BACKUP/$app/$d"
    dst="$PROD/$app/$d"
    if [ -e "$src" ] && [ ! -e "$dst" ]; then
      ln -s "$src" "$dst"
      echo "  链接 $app/$d"
    fi
  done
done
# portal state 也软链 (用户/会话/统计)
if [ -e "$PROD_BACKUP/portal/state" ] && [ ! -e "$PROD/portal/state" ]; then
  ln -s "$PROD_BACKUP/portal/state" "$PROD/portal/state"
  echo "  链接 portal/state"
fi

echo ""
echo -e "${YELLOW}[5/5] 改 launchd plist${NC}"
cp "$PLIST" "$PLIST_BACKUP"
echo "  plist 备份到: $PLIST_BACKUP"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ai-portal</string>

  <key>WorkingDirectory</key>
  <string>${PROD}/portal</string>

  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/python3.12</string>
    <string>app.py</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PORTAL_PORT</key>
    <string>9090</string>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/260413a/.local/bin:/opt/homebrew/bin</string>
    <key>HOME</key>
    <string>/Users/260413a</string>
    <key>DYLD_LIBRARY_PATH</key>
    <string>/opt/homebrew/opt/expat/lib</string>
    <key>NANO_BANANA_ENGINE</key>
    <string>fastapi</string>
    <key>SEEDANCE_ENGINE</key>
    <string>fastapi</string>
    <key>DREAMINA_ENGINE</key>
    <string>fastapi</string>
    <key>VOLCENGINE_PORTRAIT_ENGINE</key>
    <string>fastapi</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>/Users/260413a/Library/Logs/ai-portal.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/260413a/Library/Logs/ai-portal.err</string>
</dict>
</plist>
PLIST_EOF

echo "  plist 已更新"

echo ""
echo -e "${YELLOW}[reload] 启动 launchd 服务${NC}"
launchctl load "$PLIST"
sleep 8

echo ""
echo -e "${YELLOW}=== 检查 ===${NC}"
if launchctl list | grep -q "$LAUNCHD_LABEL"; then
  echo -e "${GREEN}  ✓ launchd 服务已注册${NC}"
else
  echo -e "${RED}  ✗ launchd 未注册, 查 /Users/260413a/Library/Logs/ai-portal.err${NC}"
  exit 1
fi

for port in 9090 8787 8797 8888 8891; do
  if lsof -iTCP:$port -sTCP:LISTEN -P -n 2>/dev/null | grep -q ":$port"; then
    echo -e "${GREEN}  ✓ 端口 $port LISTEN${NC}"
  else
    echo -e "${RED}  ✗ 端口 $port 未 LISTEN${NC}"
  fi
done

echo ""
echo -e "${YELLOW}=== nano-banana 8797 版本 (应是 2.0.0-fastapi) ===${NC}"
curl -4 -s -m 3 http://127.0.0.1:8797/api/v1/meta 2>&1 | head -c 200

echo ""
echo -e "${YELLOW}=== seedance 8787 版本 (应是 2.0.0-fastapi) ===${NC}"
curl -4 -s -m 3 http://127.0.0.1:8787/api/v1/meta 2>&1 | head -c 200

echo ""
echo ""
echo -e "${GREEN}=== 部署完成 ===${NC}"
echo ""
echo "  备份位置:"
echo "    代码: $PROD_BACKUP"
echo "    plist: $PLIST_BACKUP"
echo ""
echo "  回滚: bash $PROD/deploy/rollback.sh"
echo "  访问: https://192.168.30.5:9090"
