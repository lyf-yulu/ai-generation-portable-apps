import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class NoopThread:
    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        return None


class JobWorkspaceOwnershipTests(unittest.TestCase):
    def _create_job(self, module, lock_name: str):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            module.STATE_DIR = state_dir
            module.ACTIVITY_PATH = state_dir / "activity_log.json"
            module.JOBS.clear()

            with mock.patch.object(module.threading, "Thread", NoopThread):
                job_id = module.create_job(
                    {"prompt": "workspace isolation", "api_key": "secret"},
                    {},
                    "page",
                    "multipart",
                    {"values": {"prompt": "workspace isolation"}, "files": {}},
                    "ws-a",
                    username="alice",
                )

            lock = getattr(module, lock_name)
            with lock:
                stored_job = dict(module.JOBS[job_id])
            records = module.read_activity_log()
            module.JOBS.clear()

        return stored_job, records

    def test_seedance_job_keeps_submitting_workspace(self):
        module = load_module("seedance_workspace_owner", ROOT / "seedance" / "app.py")
        stored_job, _ = self._create_job(module, "JOBS_LOCK")
        self.assertEqual(stored_job.get("workspace_id"), "ws-a")

    def test_seedance_activity_keeps_submitting_workspace(self):
        module = load_module("seedance_activity_owner", ROOT / "seedance" / "app.py")
        _, records = self._create_job(module, "JOBS_LOCK")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].get("workspace_id"), "ws-a")

    def test_nano_job_keeps_submitting_workspace(self):
        module = load_module("nano_workspace_owner", ROOT / "nano-banana" / "app.py")
        stored_job, _ = self._create_job(module, "LOCK")
        self.assertEqual(stored_job.get("workspace_id"), "ws-a")

    def test_nano_activity_keeps_submitting_workspace(self):
        module = load_module("nano_activity_owner", ROOT / "nano-banana" / "app.py")
        _, records = self._create_job(module, "LOCK")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].get("workspace_id"), "ws-a")


if __name__ == "__main__":
    unittest.main()
