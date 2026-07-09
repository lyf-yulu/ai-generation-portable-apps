#!/bin/bash
# ============================================================
# AI Generation Portal — macOS 启动器
# 双击运行或终端执行: ./启动器.command
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORTAL_DIR="$SCRIPT_DIR/portal"
PID_FILE="$PORTAL_DIR/.launcher_pid.json"
PORTS=(8787 8797 8888 8891 9089 9090)
PYTHON=""

# ---- launchd 集成 ----
LAUNCHD_LABEL="com.ai-portal"
launchd_present() { launchctl list 2>/dev/null | awk '{print $3}' | grep -Fxq "$LAUNCHD_LABEL"; }
launchd_kickstart() { launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL"; }
launchd_kill()      { launchctl kill SIGTERM "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null || true; }

# ---- 子应用元数据 ----
SUBAPP_NAMES=(seedance nano-banana dreamina volcengine-portrait)
SUBAPP_PORTS=(8787     8797         8888     8891)

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'

# ---- 查找 Python ----
find_python() {
    for c in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        if [ -x "$c" ]; then
            PYTHON="$c"
            return 0
        fi
    done
    return 1
}

# ---- PID 管理 ----
load_pids() { [ -f "$PID_FILE" ] && python3 -c "import json;print(json.dumps(json.load(open('$PID_FILE'))))" 2>/dev/null || echo "{}"; }
save_pids() { mkdir -p "$PORTAL_DIR"; echo "$1" > "$PID_FILE"; }

is_alive() { kill -0 "$1" 2>/dev/null; }

collect_child_pids() {
    local data="{}"
    for port in "${PORTS[@]}"; do
        local pids
        pids=$(lsof -ti ":$port" 2>/dev/null | tr '\n' ' ' || true)
        for pid in $pids; do
            local cmd cwd name
            cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
            cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)
            if echo "$cmd" | grep -q "app.py" && echo "$cwd" | grep -q "$SCRIPT_DIR"; then
                case "$cwd" in
                    */portal)             name="portal(9090)" ;;
                    */seedance)           name="seedance(8787)" ;;
                    */nano-banana)        name="nano-banana(8797)" ;;
                    */dreamina)           name="dreamina(8888)" ;;
                    */volcengine-portrait) name="volcengine-portrait(8891)" ;;
                    *) name="app($port)" ;;
                esac
                data=$(echo "$data" | python3 -c "import sys,json; d=json.load(sys.stdin); d['$name']=$pid; print(json.dumps(d))")
            fi
        done
    done
    echo "$data"
}

status_text() {
    local pids alive=() names=()
    pids=$(load_pids)
    for name in portal\(9090\) seedance\(8787\) nano-banana\(8797\) dreamina\(8888\) volcengine-portrait\(8891\); do
        local pid
        pid=$(echo "$pids" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$name',''))" 2>/dev/null || true)
        [ -n "$pid" ] && is_alive "$pid" && alive+=("$name:$pid")
    done
    if [ ${#alive[@]} -eq 0 ]; then
        echo "●  未运行"
    else
        local joined
        joined=$(printf ", %s" "${alive[@]}")
        echo "●  运行中 — ${joined:2}"
    fi
}

# ---- 停止 ----
# 有 launchd 服务：kill Portal 前先停 launchd（否则 KeepAlive 会立刻拉起）
do_stop() {
    echo -e "${YELLOW}正在停止...${NC}"
    if launchd_present; then
        echo "  发送 SIGTERM 给 launchd 服务 $LAUNCHD_LABEL"
        launchd_kill
        sleep 2
    fi
    local pids
    pids=$(load_pids)
    for pid in $(echo "$pids" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(v) for v in d.values()]" 2>/dev/null); do
        if is_alive "$pid"; then
            kill "$pid" 2>/dev/null && echo "  已发送终止信号 → pid $pid" || true
        fi
    done
    sleep 3
    for pid in $(echo "$pids" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(v) for v in d.values()]" 2>/dev/null); do
        if is_alive "$pid"; then
            kill -9 "$pid" 2>/dev/null && echo "  强制终止 → pid $pid" || true
        fi
    done
    for port in "${PORTS[@]}"; do
        lsof -ti ":$port" 2>/dev/null | while read -r pid; do
            local cmd
            cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
            if echo "$cmd" | grep -q "app.py" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
                echo "  清理端口 $port → pid $pid"
            fi
        done || true
    done
    rm -f "$PID_FILE"
    echo -e "${GREEN}已停止。${NC}"
}

# ---- 关闭残留 cloudflared tunnel（避免外网慢链路与 Portal 并存） ----
stop_cloudflared() {
    if pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
        echo -e "${YELLOW}检测到 cloudflared，正在关闭以走局域网...${NC}"
        pkill -f "cloudflared tunnel" 2>/dev/null || true
        sleep 1
        if pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
            pkill -9 -f "cloudflared tunnel" 2>/dev/null || true
        fi
    fi
}

# ---- 启动 ----
# 有 launchd 服务：走 launchctl kickstart（KeepAlive 保证 Mac 重启后自动上线）
# 无 launchd 服务：本地开发场景，回退 python3 app.py &
do_start() {
    stop_cloudflared
    if launchd_present; then
        echo -e "${BLUE}走 launchd 启动 $LAUNCHD_LABEL（Portal + 子应用）${NC}"
        launchd_kickstart
        sleep 6
    else
        if ! find_python; then
            osascript -e 'display dialog "未找到 Python 3.9+。请安装:" & return & "brew install python@3.12" buttons {"OK"} default button "OK" with icon stop'
            return 1
        fi
        echo -e "${BLUE}Python: $PYTHON${NC}"
        echo -e "${BLUE}前台启动 Portal（未检测到 launchd 服务）${NC}"
        cd "$PORTAL_DIR"
        "$PYTHON" app.py &
        local portal_pid=$!
        echo -e "  Portal PID: $portal_pid"
        sleep 6
    fi
    local data
    data=$(collect_child_pids)
    echo "$data" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  {k}: {v}') for k,v in sorted(d.items())]"
    save_pids "$data"
    echo -e "${GREEN}启动完成。${NC}"
}

# ---- 重启全部 ----
# 有 launchd 服务：一条 kickstart -k 原子完成（Portal 重启，Portal 自己拉起子应用）
# 无 launchd 服务：stop + start 回退
do_restart_all() {
    stop_cloudflared
    if launchd_present; then
        echo -e "${BLUE}launchctl kickstart -k gui/$(id -u)/$LAUNCHD_LABEL${NC}"
        launchd_kickstart
        sleep 6
        local data
        data=$(collect_child_pids)
        echo "$data" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  {k}: {v}') for k,v in sorted(d.items())]"
        save_pids "$data"
        echo -e "${GREEN}重启完成。${NC}"
    else
        do_stop
        sleep 1
        do_start
    fi
}

# ---- 重启单个子应用 ----
# 子应用（seedance/nano-banana/dreamina/volcengine-portrait）：kill 端口上的 python 进程，
#   Portal 的 _health_loop 会在 15 秒内自动拉起最新代码
# Portal：走 launchctl kickstart（子应用会被 Portal 一起重启）
do_restart_subapp() {
    echo ""
    echo -e "  ${BOLD}选择要重启的子应用：${NC}"
    echo -e "  ${BOLD}[1]${NC} seedance (8787)"
    echo -e "  ${BOLD}[2]${NC} nano-banana (8797)"
    echo -e "  ${BOLD}[3]${NC} dreamina (8888)"
    echo -e "  ${BOLD}[4]${NC} volcengine-portrait (8891)"
    echo -e "  ${BOLD}[5]${NC} portal (9090) — 会一并重启所有子应用"
    echo -e "  ${BOLD}[c]${NC} 取消"
    echo ""
    read -r -p "  选择 [1-5/c]: " SUB
    local name="" port=""
    case "$SUB" in
        1) name="seedance"; port="8787" ;;
        2) name="nano-banana"; port="8797" ;;
        3) name="dreamina"; port="8888" ;;
        4) name="volcengine-portrait"; port="8891" ;;
        5)
            if launchd_present; then
                echo -e "${BLUE}launchctl kickstart -k Portal（会一并重启子应用）${NC}"
                launchd_kickstart
                sleep 6
                echo -e "${GREEN}Portal 已重启。${NC}"
            else
                echo -e "${YELLOW}未检测到 launchd 服务，请使用主菜单 [2] 重启全部${NC}"
            fi
            return
            ;;
        c|C|*) echo "取消。"; return ;;
    esac
    echo -e "${YELLOW}正在重启 $name (端口 $port)...${NC}"
    local killed=0
    for pid in $(lsof -ti ":$port" 2>/dev/null); do
        local cmd
        cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
        if echo "$cmd" | grep -q "app.py" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null && { echo "  kill $name pid=$pid"; killed=1; } || true
        fi
    done
    if [ "$killed" -eq 0 ]; then
        echo -e "${YELLOW}  未找到 $name 的 python 进程（端口 $port 上没有 app.py）${NC}"
        echo -e "${YELLOW}  请确认 Portal 是否在运行；主菜单 [1] 启动${NC}"
        return
    fi
    echo -e "${BLUE}  等待 Portal watchdog 自动拉起（最多 15 秒）...${NC}"
    local waited=0
    while [ "$waited" -lt 20 ]; do
        sleep 2
        waited=$((waited + 2))
        local new_pid
        new_pid=$(lsof -ti ":$port" 2>/dev/null | head -1 || true)
        if [ -n "$new_pid" ]; then
            local new_cmd
            new_cmd=$(ps -p "$new_pid" -o command= 2>/dev/null || true)
            if echo "$new_cmd" | grep -q "app.py" 2>/dev/null; then
                echo -e "${GREEN}  $name 已重启 → pid $new_pid${NC}"
                return
            fi
        fi
    done
    echo -e "${RED}  20 秒未见 $name 恢复，请检查 Portal 日志（主菜单 [5]）${NC}"
}

# ---- 主菜单 ----
show_menu() {
    clear
    echo ""
    echo -e "${BOLD}${CYAN}╔══════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYAN}║   AI Generation Portal 启动器    ║${NC}"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════╝${NC}"
    echo ""
    echo -e "  $(status_text)"
    echo ""
    echo -e "  ${BOLD}[1]${NC} 启动"
    echo -e "  ${BOLD}[2]${NC} 重启全部（Portal + 子应用）"
    echo -e "  ${BOLD}[3]${NC} 停止"
    echo -e "  ${BOLD}[4]${NC} 打开网关  ${CYAN}https://127.0.0.1:9090${NC}"
    echo -e "  ${BOLD}[5]${NC} 查看日志"
    echo -e "  ${BOLD}[6]${NC} 重启单个子应用"
    echo -e "  ${BOLD}[q]${NC} 退出"
    echo ""
    read -r -p "  选择 [1-6/q]: " CHOICE
    case "$CHOICE" in
        1) do_start ;;
        2) do_restart_all ;;
        3) do_stop ;;
        4) open "https://127.0.0.1:9090" ;;
        5) view_logs ;;
        6) do_restart_subapp ;;
        q|Q) do_stop; echo "再见."; exit 0 ;;
        *) ;;
    esac
    echo ""
    read -r -p "  按回车继续..."
}

view_logs() {
    local log_dir="$PORTAL_DIR/state/logs"
    if [ -d "$log_dir" ]; then
        echo -e "${CYAN}最近的日志 (按 q 退出):${NC}"
        echo ""
        for f in "$log_dir"/*.log; do
            [ -f "$f" ] || continue
            local name; name=$(basename "$f" .log)
            echo -e "${BOLD}─── $name ───${NC}"
            tail -8 "$f" 2>/dev/null || echo "  (空)"
            echo ""
        done
    else
        echo "日志目录不存在"
    fi
}

# ---- 入口 ----
cd "$SCRIPT_DIR"
while true; do
    show_menu
done
