#!/bin/bash
# ============================================================
# AI Generation Portal — macOS 启动器
# 双击运行或终端执行: ./启动器.command
# ============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORTAL_DIR="$SCRIPT_DIR/portal"
PID_FILE="$PORTAL_DIR/.launcher_pid.json"
PORTS=(8787 8797 8888 9089 9090)
PYTHON=""

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
                    */portal)   name="portal(9090)" ;;
                    */seedance) name="seedance(8787)" ;;
                    */nano-banana) name="nano-banana(8797)" ;;
                    */dreamina) name="dreamina(8888)" ;;
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
    for name in portal\(9090\) seedance\(8787\) nano-banana\(8797\) dreamina\(8888\); do
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
do_stop() {
    echo -e "${YELLOW}正在停止...${NC}"
    local pids
    pids=$(load_pids)
    for pid in $(echo "$pids" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(v) for v in d.values()]" 2>/dev/null); do
        is_alive "$pid" && kill "$pid" 2>/dev/null && echo "  已发送终止信号 → pid $pid"
    done
    sleep 3
    for pid in $(echo "$pids" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(v) for v in d.values()]" 2>/dev/null); do
        is_alive "$pid" && kill -9 "$pid" 2>/dev/null && echo "  强制终止 → pid $pid"
    done
    for port in "${PORTS[@]}"; do
        lsof -ti ":$port" 2>/dev/null | while read -r pid; do
            local cmd
            cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
            if echo "$cmd" | grep -q "app.py"; then
                kill -9 "$pid" 2>/dev/null
                echo "  清理端口 $port → pid $pid"
            fi
        done
    done
    rm -f "$PID_FILE"
    echo -e "${GREEN}已停止。${NC}"
}

# ---- 启动 ----
do_start() {
    if ! find_python; then
        osascript -e 'display dialog "未找到 Python 3.9+。请安装:" & return & "brew install python@3.12" buttons {"OK"} default button "OK" with icon stop'
        return 1
    fi
    echo -e "${BLUE}Python: $PYTHON${NC}"
    echo -e "${BLUE}启动 Portal + 子应用...${NC}"
    cd "$PORTAL_DIR"
    "$PYTHON" app.py &
    local portal_pid=$!
    echo -e "  Portal PID: $portal_pid"
    # 等待子应用启动
    sleep 6
    # 收集子进程 PID
    local data
    data=$(collect_child_pids)
    echo "$data" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  {k}: {v}') for k,v in sorted(d.items())]"
    save_pids "$data"
    echo -e "${GREEN}启动完成。${NC}"
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
    echo -e "  ${BOLD}[2]${NC} 重启"
    echo -e "  ${BOLD}[3]${NC} 停止"
    echo -e "  ${BOLD}[4]${NC} 打开网关  ${CYAN}https://127.0.0.1:9090${NC}"
    echo -e "  ${BOLD}[5]${NC} 查看日志"
    echo -e "  ${BOLD}[q]${NC} 退出"
    echo ""
    read -r -p "  选择 [1-5/q]: " CHOICE
    case "$CHOICE" in
        1) do_start ;;
        2) do_stop; sleep 1; do_start ;;
        3) do_stop ;;
        4) open "https://127.0.0.1:9090" ;;
        5) view_logs ;;
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
