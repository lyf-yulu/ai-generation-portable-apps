import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
    @staticmethod
    def _app_configs():
        return {
            "managed-demo": {
                "dir": ROOT / "seedance",
                "port": 18787,
                "spec": SimpleNamespace(managed=True),
            },
            "external-agent": {
                "dir": ROOT / "feishu-generation-agent",
                "port": 8765,
                "spec": SimpleNamespace(managed=False),
            },
        }

    def test_start_all_starts_only_managed_apps_and_probes_external_apps(self):
        module = load_portal_module()
        manager = module.AppManager()
        configs = self._app_configs()

        with mock.patch.object(module, "APPS", configs), \
             mock.patch.object(manager, "start_app") as start_app, \
             mock.patch.object(manager, "_kill_port_squatter") as kill_port, \
             mock.patch.object(manager, "_tcp_probe", side_effect=lambda port: port == 8765), \
             mock.patch.object(module.threading, "Thread"):
            manager.start_all()

        start_app.assert_called_once_with("managed-demo", configs["managed-demo"])
        kill_port.assert_not_called()
        self.assertEqual(manager.status["external-agent"], {"status": "running", "port": 8765})
        self.assertNotIn("external-agent", manager.processes)

    def test_unhealthy_external_app_is_unavailable_without_restart_or_port_cleanup(self):
        module = load_portal_module()
        manager = module.AppManager()
        configs = {"external-agent": self._app_configs()["external-agent"]}
        sleeps = 0

        def advance_three_health_cycles(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps >= 3:
                manager._stop_event.set()

        with mock.patch.object(module, "APPS", configs), \
             mock.patch.object(manager, "start_app") as start_app, \
             mock.patch.object(manager, "_kill_port_squatter") as kill_port, \
             mock.patch.object(manager, "_tcp_probe", return_value=False), \
             mock.patch.object(module.time, "sleep", side_effect=advance_three_health_cycles):
            manager._health_loop()

        self.assertEqual(manager.status["external-agent"], {"status": "unavailable", "port": 8765})
        start_app.assert_not_called()
        kill_port.assert_not_called()

    def test_shutdown_never_signals_an_external_app(self):
        module = load_portal_module()
        manager = module.AppManager()
        managed_proc = mock.Mock(pid=12345)
        managed_proc.poll.return_value = 0
        external_proc = mock.Mock(pid=8765)
        manager.processes["managed-demo"] = managed_proc

        manager.shutdown()

        external_proc.terminate.assert_not_called()
        external_proc.kill.assert_not_called()

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
            with mock.patch.object(module.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(manager, "_kill_port_squatter"):
                manager.start_app("demo", {"dir": app_dir, "port": 9999})

        self.assertNotEqual(captured.get("stdout"), subprocess.PIPE)
        self.assertNotEqual(captured.get("stderr"), subprocess.PIPE)
        manager.log_handles["demo"].close()


if __name__ == "__main__":
    unittest.main()
