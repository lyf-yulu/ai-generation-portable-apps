"""stdlib 回退入口占位。

Portal 要求每个子应用目录下存在 app.py（portal/app.py:609，缺失则状态判 missing
根本不启动），并在 engine != "fastapi" 时回退运行它（portal/app.py:646）。

无限画布只有 FastAPI 实现。与其让 Portal 静默跑起一个空壳、把问题推迟到用户
点开标签页才发现，不如在这里明确失败并说明原因。
"""

import sys

sys.stderr.write(
    "infinite-canvas 需要 FastAPI 引擎。\n"
    "请在 ~/Library/LaunchAgents/com.ai-portal.plist 的 EnvironmentVariables 中加入：\n"
    "    <key>INFINITE_CANVAS_ENGINE</key><string>fastapi</string>\n"
    "然后 launchctl kickstart -k gui/$(id -u)/com.ai-portal\n"
)
sys.exit(1)
