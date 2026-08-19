#!/bin/bash
# ============================================================
# AI Generation Portal — 安全重启脚本
# 用法：bash portal/state/scripts/safe_restart.sh [--force]
# 功能：固化重启前检查 + launchctl kickstart + 重启后探活
# ============================================================
set -o pipefail

FORCE=0
if [[ "${1:-}" == "--force" ]]; then FORCE=1; fi

PORTAL=https://127.0.0.1:9090
PORTS=(8787 8797 8888 8891 9090)
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'; BOLD='\033[1m'

fail() { echo -e "${RED}✗ $1${NC}" >&2; [[ $FORCE -eq 1 ]] || exit 1; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
info() { echo -e "${BLUE}→ $1${NC}"; }

echo -e "${BOLD}=== AI Portal 安全重启 ===${NC}"
echo ""

# ---- 1. VPN 检查（240.0.0.1 / 100.64.x.x / Tailscale） ----
info "检查 VPN 抢路由..."
if ifconfig 2>/dev/null | grep -E "inet (240\.|100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\.)" >/dev/null; then
  fail "VPN 仍在运行（检测到 240.x 或 100.64-127 私有段地址），先关掉 VPN 再重启"
else
  ok "VPN 未抢路由"
fi

# ---- 2. 最近 5 分钟用户活动 ----
info "检查最近 5 分钟用户活动..."
ACTIVITY_RECENT=$(curl -sk -m 5 "$PORTAL/api/platform/activity" 2>/dev/null | \
  python3 -c "
import sys, json, time
try:
    d = json.load(sys.stdin)
except Exception:
    print(0); sys.exit(0)
items = d.get('activity', [])
now = time.time()
n = 0
for r in items:
    t = r.get('time') or r.get('created_at') or ''
    if not t: continue
    try:
        ts = time.mktime(time.strptime(t, '%Y-%m-%d %H:%M:%S'))
        if now - ts < 300: n += 1
    except Exception: pass
print(n)
" 2>/dev/null || echo "?")
if [[ "$ACTIVITY_RECENT" == "?" ]]; then
  warn "无法读取 activity（Portal 可能已挂），跳过此检查"
elif [[ "$ACTIVITY_RECENT" -gt 0 ]]; then
  fail "最近 5 分钟有 $ACTIVITY_RECENT 条用户活动，重启会打断；--force 可跳过"
else
  ok "最近 5 分钟无用户活动"
fi

# ---- 3. 各子应用 running jobs ----
info "检查 running jobs..."
TOTAL_RUNNING=0
for app in seedance nano-banana dreamina volcengine-portrait; do
  count=$(curl -sk -m 5 "$PORTAL/$app/api/jobs" 2>/dev/null | \
    python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(0); sys.exit(0)
jobs = d.get('jobs', []) if isinstance(d, dict) else []
print(sum(1 for j in jobs if j.get('status') == 'running'))
" 2>/dev/null || echo 0)
  if [[ "$count" -gt 0 ]]; then
    warn "$app 还有 $count 个 running job"
    TOTAL_RUNNING=$((TOTAL_RUNNING + count))
  fi
done
if [[ $TOTAL_RUNNING -gt 0 ]]; then
  fail "共 $TOTAL_RUNNING 个 running job，重启会丢失；--force 可跳过"
else
  ok "无 running job"
fi

# ---- 4. lan_ip 与 cert 一致性 ----
info "检查 LAN IP 与证书 SAN 一致..."
CERT_IP_FILE=/Users/260413a/ai-generation-portable-apps/portal/certs/lan_ip.txt
CURRENT_IP="?"
CERT_IP="?"
if python3 -c "import sys; sys.path.insert(0,'/Users/260413a/ai-generation-portable-apps/portal'); from app import get_lan_ip; print(get_lan_ip())" >/tmp/.safe_restart_ip.txt 2>/dev/null; then
  CURRENT_IP=$(cat /tmp/.safe_restart_ip.txt)
fi
rm -f /tmp/.safe_restart_ip.txt
if [[ -f "$CERT_IP_FILE" ]]; then
  CERT_IP=$(cat "$CERT_IP_FILE" 2>/dev/null)
  [[ -z "$CERT_IP" ]] && CERT_IP="?"
  if [[ "$CURRENT_IP" == "?" ]]; then
    warn "无法读取当前 LAN IP（python import 失败），仅显示 cert=$CERT_IP"
  elif [[ "$CURRENT_IP" != "$CERT_IP" ]]; then
    warn "lan_ip 漂移（cert=$CERT_IP, current=$CURRENT_IP）；重启时会自动重签证书"
  else
    ok "lan_ip=$CERT_IP（与证书一致）"
  fi
else
  warn "未找到 lan_ip.txt，重启会重新生成"
fi

# ---- 5. 当前端口残留 ----
info "检查端口 listener..."
for p in "${PORTS[@]}"; do
  pid=$(lsof -ti:"$p" -sTCP:LISTEN 2>/dev/null | head -1)
  if [[ -n "$pid" ]]; then
    cmd=$(ps -p "$pid" -o command= 2>/dev/null | head -c 80)
    echo "  $p → pid $pid ($cmd)"
  fi
done

# ---- 6. 孤儿子进程枚举 ----
info "检查 PPID=1 的孤儿 app.py..."
ORPHANS=$(ps -axo pid,ppid,command | awk '$2==1 && / app\.py/ {print}' || true)
if [[ -n "$ORPHANS" ]]; then
  warn "发现孤儿进程："
  echo "$ORPHANS"
else
  ok "无孤儿 app.py"
fi

# ---- 7. 执行 launchctl kickstart ----
echo ""
info "执行 launchctl kickstart -k gui/$(id -u)/com.ai-portal..."
launchctl kickstart -k "gui/$(id -u)/com.ai-portal"
sleep 5

# ---- 8. 重启后探活 ----
info "重启后探活（最多等 30 秒）..."
ATTEMPT=0
while [[ $ATTEMPT -lt 15 ]]; do
  CODE=$(curl -sk -m 3 -o /dev/null -w "%{http_code}" "$PORTAL/api/auth/first-run" 2>/dev/null || echo 000)
  if [[ "$CODE" == "200" ]]; then
    ok "Portal 已就绪 → $PORTAL"
    break
  fi
  ATTEMPT=$((ATTEMPT + 1))
  sleep 2
done
if [[ "$CODE" != "200" ]]; then
  fail "Portal 在 30 秒内未恢复（最后 HTTP=$CODE）"
fi

# ---- 9. 子应用就绪检查 ----
info "检查子应用 status..."
sleep 3
curl -sk -m 5 "$PORTAL/api/platform/status" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('  无法读取 status'); sys.exit(0)
for app in d.get('apps', []):
    name = app.get('name', '?')
    status = app.get('status', '?')
    port = app.get('port', '?')
    flag = '✓' if status == 'ready' else '⚠'
    print(f'  {flag} {name} (port {port}) → {status}')
" 2>/dev/null || warn "status JSON 解析失败"

echo ""
ok "重启完成"
