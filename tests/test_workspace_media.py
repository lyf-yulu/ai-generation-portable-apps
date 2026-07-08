import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Field:
    filename = None

    def __init__(self, value: str):
        self.value = value


class FakeThread:
    last_args = None

    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon
        FakeThread.last_args = args

    def start(self):
        pass


class FakeHandler:
    headers = {}
    path = "/?ws=client-a"
    client_address = ("127.0.0.1", 12345)


class WorkspaceMediaTests(unittest.TestCase):
    def assert_reads_workspace_saved_media(self, module, field_name: str):
        with tempfile.TemporaryDirectory() as tmp:
            module.STATE_DIR = Path(tmp)
            ws_id = "client-a"
            media_dir = module._ws_media_dir(ws_id)
            media_dir.mkdir(parents=True)
            (media_dir / "sample.png").write_bytes(b"workspace-bytes")
            form = {
                "saved_media": Field(json.dumps({
                    field_name: {"stored": "sample.png", "filename": "sample.png"}
                }))
            }

            self.assertEqual(
                module.get_file_or_saved(form, field_name, ws_id),
                ("sample.png", b"workspace-bytes"),
            )

    def test_seedance_reads_saved_media_from_workspace(self):
        module = load_module("seedance_under_test", ROOT / "seedance" / "app.py")
        self.assert_reads_workspace_saved_media(module, "first_frame")

    def test_nano_reads_saved_media_from_workspace(self):
        module = load_module("nano_under_test", ROOT / "nano-banana" / "app.py")
        self.assert_reads_workspace_saved_media(module, "image_1")

    def assert_create_job_passes_workspace_to_worker(self, module):
        FakeThread.last_args = None
        with mock.patch.object(module, "record_activity"), \
             mock.patch.object(module.threading, "Thread", FakeThread):
            module.create_job(
                {"prompt": "test", "api_key": "secret"},
                {},
                "page",
                "multipart",
                {"values": {}, "files": {}},
                "client-a",
            )

        self.assertIsNotNone(FakeThread.last_args)
        self.assertEqual(FakeThread.last_args[-1], "client-a")

    def test_seedance_create_job_passes_workspace_to_worker(self):
        module = load_module("seedance_job_under_test", ROOT / "seedance" / "app.py")
        self.assert_create_job_passes_workspace_to_worker(module)

    def test_nano_create_job_passes_workspace_to_worker(self):
        module = load_module("nano_job_under_test", ROOT / "nano-banana" / "app.py")
        self.assert_create_job_passes_workspace_to_worker(module)

    def assert_loads_legacy_archive_into_workspace(self, module, suffix: str):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            module.STATE_DIR = base / "state"
            module.ARCHIVE_DIR = base / "archives"
            module.ARCHIVE_DIR.mkdir(parents=True)
            legacy = module.ARCHIVE_DIR / f"legacy.{suffix}"
            with zipfile.ZipFile(legacy, "w") as zf:
                zf.writestr("preset.json", json.dumps({"values": {"prompt": "old"}, "media": {}}))

            result = module.load_archive_file("legacy", FakeHandler())

            self.assertEqual(result["values"]["prompt"], "old")
            self.assertTrue(module.archive_path("legacy", "client-a").exists())

    def test_seedance_loads_legacy_archive_into_workspace(self):
        module = load_module("seedance_archive_under_test", ROOT / "seedance" / "app.py")
        self.assert_loads_legacy_archive_into_workspace(module, "seedance")

    def test_nano_loads_legacy_archive_into_workspace(self):
        module = load_module("nano_archive_under_test", ROOT / "nano-banana" / "app.py")
        self.assert_loads_legacy_archive_into_workspace(module, "nanobanana")


if __name__ == "__main__":
    unittest.main()
