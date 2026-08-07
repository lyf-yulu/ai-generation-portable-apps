"""Verify Seedance 2.5 once ByteDance activates it on the Ark account.

Two things this checks, in order:

1. Activation — free and reliable. An unactivated model answers ModelNotOpen
   before any parameter validation; an activated one answers InvalidParameter
   for the deliberately illegal duration we send. Unambiguous either way.

2. Real generation — the authoritative compatibility test. Costs one short
   video per resolution tried.

Why there is no "probe the legal parameter values for free" step: Ark reports
only the first field it objects to, and which field that is turns out to be
non-deterministic. Sending resolution=2K with a guaranteed-illegal sentinel
returned the sentinel's field on some attempts and `resolution` on others,
across repeated identical requests. So "the sentinel caught it" does not prove
the value under test is legal, and a probe-only run would report confident
nonsense. Real generation settles it.

    python3 tools/verify_seedance_25.py              # activation only, free
    python3 tools/verify_seedance_25.py --generate   # + one 5s/480p video
    python3 tools/verify_seedance_25.py --generate --resolution 1080p
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "seedance" / "state" / "secrets.json"
BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MODEL_25 = "doubao-seedance-2-5-260628"
MODEL_20 = "doubao-seedance-2-0-260128"
PROMPT = "a red ball rolling slowly across a wooden table, cinematic lighting"


def load_key() -> str:
    if not SECRETS.exists():
        sys.exit(f"缺少 {SECRETS}")
    key = str(json.loads(SECRETS.read_text("utf-8")).get("volcengine_api_key") or "").strip()
    if not key:
        sys.exit(f"{SECRETS} 里 volcengine_api_key 为空")
    return key


def post(key: str, path: str, payload: dict | None, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"error": {"code": f"HTTP{exc.code}", "message": body[:300]}}
    except Exception as exc:
        return {"error": {"code": "NetworkError", "message": str(exc)}}


def err(body: dict) -> tuple[str, str]:
    e = body.get("error")
    if not isinstance(e, dict):
        return "", ""
    return str(e.get("code") or ""), str(e.get("message") or "")


def payload_for(model: str, resolution: str, duration: int, ratio: str) -> dict:
    return {
        "model": model,
        "content": [{"type": "text", "text": PROMPT}],
        "duration": duration,
        "ratio": ratio,
        "resolution": resolution,
    }


def check_activation(key: str) -> bool:
    """Return True when 2.5 is usable. duration=999 keeps this free."""
    print("=" * 64)
    print("STEP 1  开通状态（不计费）")
    print("=" * 64)
    ok = False
    for model in (MODEL_20, MODEL_25):
        body = post(key, "/contents/generations/tasks", payload_for(model, "480p", 999, "16:9"))
        code, message = err(body)
        if code == "ModelNotOpen":
            verdict = "未开通 —— 需在方舟控制台开通该模型"
        elif code == "InvalidParameter":
            verdict = "已开通（参数校验生效）"
            ok = ok or model == MODEL_25
        elif not code:
            verdict = "已开通，但意外接受了 duration=999（请检查是否建了任务）"
            ok = ok or model == MODEL_25
        else:
            verdict = f"{code}: {message[:70]}"
        print(f"  {model:<34} {verdict}")
    return ok


def generate(key: str, resolution: str, duration: int, ratio: str, timeout: float) -> bool:
    print()
    print("=" * 64)
    print(f"STEP 2  真实生成 {duration}s / {resolution} / {ratio}（计费）")
    print("=" * 64)
    body = post(key, "/contents/generations/tasks", payload_for(MODEL_25, resolution, duration, ratio))
    code, message = err(body)
    if code:
        print(f"  提交被拒: {code}")
        print(f"  {message[:300]}")
        return False
    task_id = str(body.get("id") or "")
    if not task_id:
        print(f"  未拿到 task id，响应: {json.dumps(body, ensure_ascii=False)[:300]}")
        return False
    print(f"  已提交: {task_id}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(10)
        data = post(key, f"/contents/generations/tasks/{task_id}", None, timeout=30)
        code, message = err(data)
        if code:
            print(f"  轮询出错: {code} / {message[:120]}")
            continue
        status = str(data.get("status") or "").lower()
        print(f"  status={status}  ({int(time.time() - (deadline - timeout))}s)")
        if status in ("succeeded", "failed", "cancelled", "canceled"):
            if status == "succeeded":
                content = data.get("content") or {}
                url = content.get("video_url") if isinstance(content, dict) else None
                print(f"  成功。video_url: {str(url)[:110]}")
                usage = data.get("usage")
                if usage:
                    print(f"  usage: {json.dumps(usage, ensure_ascii=False)[:200]}")
                return True
            print(f"  终态 {status}，完整响应:")
            print(f"  {json.dumps(data, ensure_ascii=False)[:400]}")
            return False
    print("  超时未出终态")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="检查 Seedance 2.5 是否可用，并可选跑一次真实生成。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--generate", action="store_true", help="开通后跑一次真实生成（会计费）")
    parser.add_argument("--resolution", default="480p", help="生成分辨率（默认 480p，最省）")
    parser.add_argument("--duration", type=int, default=5, help="时长秒数（默认 5）")
    parser.add_argument("--ratio", default="16:9", help="画面比例（默认 16:9）")
    parser.add_argument("--timeout", type=float, default=900.0, help="轮询上限秒数（默认 900）")
    args = parser.parse_args()

    key = load_key()
    if not check_activation(key):
        print()
        print("2.5 尚未开通。开通后重跑：")
        print("  python3 tools/verify_seedance_25.py --generate")
        return

    if not args.generate:
        print()
        print("2.5 已开通。跑真实生成验证参数兼容性：")
        print("  python3 tools/verify_seedance_25.py --generate")
        print("再逐档确认更高分辨率（每次都会计费）：")
        print("  python3 tools/verify_seedance_25.py --generate --resolution 1080p")
        return

    ok = generate(key, args.resolution, args.duration, args.ratio, args.timeout)
    print()
    if ok:
        print("结论：2.5 可用，且现有 payload 结构无需改动。")
    else:
        print("结论：提交或生成失败，请把上面的报错贴给我，据此调整 payload。")


if __name__ == "__main__":
    main()
