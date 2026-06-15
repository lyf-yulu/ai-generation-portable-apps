import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PORTAL_APP = ROOT / "portal" / "app.py"


def load_portal_module():
    spec = importlib.util.spec_from_file_location("portal_app_under_test", PORTAL_APP)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PortalStartupTests(unittest.TestCase):
    def test_main_does_not_start_subapps_when_portal_bind_fails(self):
        module = load_portal_module()

        class FakeManager:
            started = False

            def start_all(self):
                self.started = True

            def shutdown(self):
                pass

        fake_manager = FakeManager()

        def fail_bind(*args, **kwargs):
            raise OSError("address already in use")

        with mock.patch.object(module, "manager", fake_manager), \
             mock.patch.object(module, "ensure_certs", return_value=None), \
             mock.patch.object(module, "get_lan_ip", return_value="127.0.0.1"), \
             mock.patch.object(module.time, "sleep", return_value=None), \
             mock.patch.object(module, "ThreadingHTTPServer", side_effect=fail_bind):
            with self.assertRaises(OSError):
                module.main()

        self.assertFalse(fake_manager.started)

    def test_start_app_does_not_pipe_child_output_without_reader(self):
        module = load_portal_module()
        manager = module.AppManager()
        captured = {}

        class FakeProc:
            pid = 1234

            def poll(self):
                return None

        def fake_popen(*args, **kwargs):
            captured.update(kwargs)
            return FakeProc()

        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            (app_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
            with mock.patch.object(module.subprocess, "Popen", side_effect=fake_popen):
                manager.start_app("demo", {"dir": app_dir, "port": 9999})

        self.assertNotEqual(captured.get("stdout"), subprocess.PIPE)
        self.assertNotEqual(captured.get("stderr"), subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
