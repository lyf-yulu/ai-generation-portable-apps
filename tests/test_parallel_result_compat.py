from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(relative_path: str, name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_nano_generic_image_saver_accepts_unpadded_base64(tmp_path):
    module = _load("nano-banana/app.py", "nano_parallel_result_test")
    raw = b"five!"
    unpadded = base64.b64encode(raw).decode("ascii").rstrip("=")

    source_url, local_path = module.save_image_item(
        {"b64_json": unpadded}, tmp_path, "parallel", 1
    )

    assert source_url == ""
    assert Path(local_path).read_bytes() == raw


def test_seedance_extracts_video_url_from_output_object():
    module = _load("seedance/app.py", "seedance_parallel_result_test")

    assert module.extract_video_url({
        "output": {"video_url": "https://cdn.example/result.mp4"}
    }) == "https://cdn.example/result.mp4"


def test_portrait_partial_missing_video_url_marks_whole_job_failed(tmp_path, monkeypatch):
    module = _load("volcengine-portrait/app.py", "portrait_parallel_result_test")
    responses = iter([
        {"id": "task-1"},
        {
            "status": "succeeded",
            "output": {"video_url": "https://cdn.example/result.mp4"},
        },
        {"id": "task-2"},
        {"status": "succeeded"},
    ])
    monkeypatch.setattr(module, "ark_v3_call", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(module, "_asset_content_item", lambda *args, **kwargs: {
        "type": "image_url",
        "image_url": {"url": "asset://asset-1"},
        "role": "reference_image",
    })
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(module, "download_video", lambda *args, **kwargs: tmp_path / "result.mp4")
    monkeypatch.setattr(module, "save_files_map", lambda: None)
    monkeypatch.setattr(module, "update_activity", lambda *args, **kwargs: None)
    job = {
        "api_key": "key",
        "asset_id": "asset-1",
        "prompt": "test",
        "total": 2,
        "done": 0,
        "events": [],
        "results": [],
        "errors": [],
    }

    module._run_virtual_job_impl("job-1", job)

    assert job["status"] == "failed"
    assert len(job["results"]) == 1
    assert job["done"] == 2
    assert len(job["errors"]) == 1
    assert "没有视频地址" in job["errors"][0]


def test_dreamina_partial_parallel_failure_marks_whole_job_failed(monkeypatch):
    module = _load("dreamina/app.py", "dreamina_parallel_result_test")
    response_lock = threading.Lock()
    responses = [
        {"returncode": 0, "stdout": json.dumps({"ok": True}), "stderr": ""},
        {"returncode": 1, "stdout": "", "stderr": "one child failed"},
    ]

    def fake_run_cmd(*args, **kwargs):
        with response_lock:
            return responses.pop()

    callback_statuses = []
    activity_updates = []
    monkeypatch.setattr(module, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(module, "report_final_to_portal", lambda job_id, status: callback_statuses.append(status))
    monkeypatch.setattr(module, "update_activity", lambda activity_id, **updates: activity_updates.append(updates))
    module.JOBS["job-1"] = {
        "status": "queued",
        "events": [],
        "errors": [],
        "results": [],
        "done": 0,
        "total": 2,
        "concurrency": 2,
        "activity_id": "activity-1",
    }

    module._execute_task_impl("job-1", "image", ["dreamina", "generate"], {})

    job = module.JOBS["job-1"]
    assert job["status"] == "failed"
    assert len(job["results"]) == 1
    assert len(job["errors"]) == 1
    assert "one child failed" in job["error"]
    assert callback_statuses == ["failed"]
    assert activity_updates[-1]["status"] == "failed"
