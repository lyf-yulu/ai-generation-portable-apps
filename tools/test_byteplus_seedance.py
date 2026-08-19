#!/usr/bin/env python3
"""Submit a BytePlus Seedance task, poll it, and download the result."""

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"
DEFAULT_MODEL = "ep-20260805102121-kqzt7"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "canceled", "expired"}
IMAGE_MIME_OVERRIDES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


class ScriptError(RuntimeError):
    """Expected user-facing failure."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="测试 BytePlus Seedance：提交任务、轮询状态并下载视频。"
    )
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="直接提供提示词")
    prompt_group.add_argument("--prompt-file", type=Path, help="从 UTF-8 文本文件读取提示词")
    parser.add_argument(
        "--image",
        help="可选参考图：本地路径、http(s) URL 或 asset:// 地址",
    )
    parser.add_argument(
        "--image-role",
        choices=("reference_image", "first_frame", "last_frame"),
        default="reference_image",
        help="参考图角色（默认：reference_image）",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="区域 API 根地址")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型 endpoint ID")
    parser.add_argument("--ratio", default="16:9", help="画面比例（默认：16:9）")
    parser.add_argument("--duration", type=int, default=5, help="视频时长秒数（默认：5）")
    parser.add_argument("--output", type=Path, help="下载目标 MP4 路径")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="轮询间隔秒数（默认：5）",
    )
    parser.add_argument("--timeout", type=float, default=900.0, help="总等待秒数（默认：900）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅显示请求，不读取密钥、不提交任务",
    )
    return parser


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        prompt = args.prompt
    else:
        try:
            prompt = args.prompt_file.expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ScriptError("无法读取提示词文件：{}".format(exc)) from exc
    prompt = prompt.strip()
    if not prompt:
        raise ScriptError("提示词不能为空")
    return prompt


def resolve_image_source(source: str) -> str:
    source = source.strip()
    if not source:
        raise ScriptError("--image 不能为空")
    if source.startswith(("https://", "http://", "asset://")):
        return source

    path = Path(source).expanduser()
    if not path.is_file():
        raise ScriptError("参考图不存在或不是文件：{}".format(path))
    mime = IMAGE_MIME_OVERRIDES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    if not mime or not mime.startswith("image/"):
        raise ScriptError("不支持的参考图格式：{}".format(path.suffix or "无扩展名"))
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise ScriptError("无法读取参考图：{}".format(exc)) from exc
    return "data:{};base64,{}".format(mime, encoded)


def build_payload(args: argparse.Namespace, prompt: str) -> Dict[str, Any]:
    content = [{"type": "text", "text": prompt}]
    if args.image:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": resolve_image_source(args.image)},
                "role": args.image_role,
            }
        )
    return {
        "model": args.model.strip(),
        "content": content,
        "generate_audio": True,
        "ratio": args.ratio.strip(),
        "duration": args.duration,
        "watermark": False,
    }


def validate_args(args: argparse.Namespace) -> None:
    if args.duration <= 0:
        raise ScriptError("--duration 必须大于 0")
    if args.poll_interval <= 0:
        raise ScriptError("--poll-interval 必须大于 0")
    if args.timeout <= 0:
        raise ScriptError("--timeout 必须大于 0")
    if not args.model.strip():
        raise ScriptError("--model 不能为空")
    if not args.ratio.strip():
        raise ScriptError("--ratio 不能为空")
    if not args.base_url.strip():
        raise ScriptError("--base-url 不能为空")


def safe_payload_for_display(payload: Dict[str, Any]) -> Dict[str, Any]:
    safe = json.loads(json.dumps(payload))
    for item in safe.get("content", []):
        image_url = item.get("image_url") if isinstance(item, dict) else None
        url = image_url.get("url") if isinstance(image_url, dict) else None
        if isinstance(url, str) and url.startswith("data:"):
            header, encoded = url.split(",", 1)
            image_url["url"] = "{},<{} base64 chars omitted>".format(header, len(encoded))
    return safe


def describe_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        error = data.get("error") if isinstance(data.get("error"), dict) else data
        details = []
        for key in ("code", "message", "request_id", "requestId"):
            value = error.get(key)
            if value and str(value) not in details:
                details.append(str(value))
        if details:
            return "HTTP {}: {}".format(exc.code, " | ".join(details))
    summary = raw.strip().replace("\n", " ")[:500]
    return "HTTP {}{}".format(exc.code, ": " + summary if summary else "")


def request_json(
    method: str,
    url: str,
    api_key: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    body = None
    headers = {"Authorization": "Bearer " + api_key}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise ScriptError(describe_http_error(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ScriptError("连接失败：{}".format(exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScriptError("API 返回的不是合法 JSON：{}".format(raw[:500])) from exc
    if not isinstance(data, dict):
        raise ScriptError("API 返回格式异常：顶层不是 JSON 对象")
    return data


def extract_video_url(data: Dict[str, Any]) -> Optional[str]:
    content = data.get("content")
    if isinstance(content, dict):
        url = content.get("video_url") or content.get("videoUrl")
        if url:
            return str(url)
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "video_url":
                continue
            value = item.get("video_url")
            if isinstance(value, dict) and value.get("url"):
                return str(value["url"])
            if isinstance(value, str) and value:
                return value
    nested = data.get("data")
    if isinstance(nested, dict):
        nested_url = extract_video_url(nested)
        if nested_url:
            return nested_url
    for key in ("video_url", "videoUrl"):
        if data.get(key):
            return str(data[key])
    output = data.get("output")
    if isinstance(output, dict):
        url = output.get("video_url") or output.get("videoUrl")
        if url:
            return str(url)
    elif isinstance(output, str) and output:
        return output
    return None


def task_error_summary(data: Dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        values = [error.get("code"), error.get("message")]
        summary = " | ".join(str(value) for value in values if value)
        if summary:
            return summary
    for key in ("failure_reason", "message"):
        if data.get(key):
            return str(data[key])
    return json.dumps(data, ensure_ascii=False)[:1000]


def download_video(url: str, output: Path) -> None:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ScriptError("输出文件已存在，不会覆盖：{}".format(output))
    part = output.with_name(output.name + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "BytePlus-Seedance-Test/1.0"})
        with urllib.request.urlopen(request, timeout=300) as response, part.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        if not part.exists() or part.stat().st_size == 0:
            raise ScriptError("下载结果为空")
        part.replace(output)
    except ScriptError:
        if part.exists():
            part.unlink()
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if part.exists():
            part.unlink()
        raise ScriptError("视频下载失败：{}".format(exc)) from exc


def default_output_path() -> Path:
    return Path("seedance-{}.mp4".format(time.strftime("%Y%m%d-%H%M%S")))


def run(args: argparse.Namespace) -> Path:
    validate_args(args)
    prompt = read_prompt(args)
    payload = build_payload(args, prompt)
    base_url = args.base_url.rstrip("/")
    create_url = base_url + "/contents/generations/tasks"

    if args.dry_run:
        print("POST {}".format(create_url))
        print(json.dumps(safe_payload_for_display(payload), ensure_ascii=False, indent=2))
        return Path()

    api_key = os.environ.get("ARK_API_KEY", "").strip()
    if not api_key:
        raise ScriptError("缺少环境变量 ARK_API_KEY")

    output = (args.output or default_output_path()).expanduser().resolve()
    if output.exists():
        raise ScriptError("输出文件已存在，不会提交付费任务：{}".format(output))

    created = request_json("POST", create_url, api_key, payload, timeout=60)
    task_id = created.get("id") or created.get("task_id")
    if not task_id:
        raise ScriptError("提交成功但没有返回任务 ID：{}".format(json.dumps(created, ensure_ascii=False)[:1000]))
    task_id = str(task_id)
    print("任务 ID：{}".format(task_id), flush=True)

    status_url = create_url + "/" + task_id
    started = time.monotonic()
    previous_status = None
    while True:
        if time.monotonic() - started >= args.timeout:
            raise ScriptError(
                "任务 {} 在 {} 秒内未完成；可使用任务 ID 在控制台继续查询".format(
                    task_id, args.timeout
                )
            )
        status_data = request_json("GET", status_url, api_key, timeout=60)
        status = str(status_data.get("status") or "unknown").strip().lower()
        if status != previous_status:
            print("状态：{}".format(status), flush=True)
            previous_status = status
        if status in TERMINAL_STATUSES:
            if status != "succeeded":
                raise ScriptError(
                    "任务 {} 结束为 {}：{}".format(task_id, status, task_error_summary(status_data))
                )
            video_url = extract_video_url(status_data)
            if not video_url:
                raise ScriptError("任务 {} 已成功，但响应中没有视频 URL".format(task_id))
            print("正在下载视频……", flush=True)
            download_video(video_url, output)
            print("已保存：{}（{} bytes）".format(output, output.stat().st_size), flush=True)
            return output
        time.sleep(args.poll_interval)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        run(args)
        return 0
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except ScriptError as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
