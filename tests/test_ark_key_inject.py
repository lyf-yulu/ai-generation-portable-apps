"""Shared Volcengine Ark key injection for the image-generation sub-app."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "portal"
if str(PORTAL) not in sys.path:
    sys.path.insert(0, str(PORTAL))


def _load_portal(tmp_path: Path):
    old_data_dir = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("portal_ark_key_test", PORTAL / "app.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = old_data_dir


def test_app_spec_parses_ark_key_capability():
    from app_spec import _spec_from_dict

    default = _spec_from_dict(
        {"name": "plain", "port_env": "PLAIN_PORT", "port_default": 1}, ROOT
    )
    enabled = _spec_from_dict(
        {
            "name": "image-app",
            "port_env": "IMAGE_PORT",
            "port_default": 2,
            "needs_ark_key": True,
        },
        ROOT,
    )

    assert default.needs_ark_key is False
    assert enabled.needs_ark_key is True


def test_nano_banana_declares_shared_ark_key_requirement():
    from app_spec import load_specs

    specs = load_specs(PORTAL / "apps.json", ROOT)
    nano = next(item for item in specs if item.name == "nano-banana")
    assert nano.display_name == "图像生成模块"
    assert nano.needs_ark_key is True


def test_read_ark_key_uses_seedance_server_secret(tmp_path):
    module = _load_portal(tmp_path)
    secret_dir = tmp_path / "seedance"
    (secret_dir / "state").mkdir(parents=True)
    (secret_dir / "state" / "secrets.json").write_text(
        json.dumps({"volcengine_api_key": "ark-test-key"}), encoding="utf-8"
    )
    manager = module.AppManager()

    with mock.patch.dict(
        module.SPEC_BY_NAME,
        {"seedance": SimpleNamespace(dir_path=secret_dir)},
        clear=True,
    ):
        assert manager._read_ark_key() == "ark-test-key"


def test_start_app_injects_ark_key_only_into_child_environment(tmp_path):
    module = _load_portal(tmp_path)
    manager = module.AppManager()
    proc = mock.Mock(pid=43210)
    proc.poll.return_value = None
    spec = SimpleNamespace(
        managed=True,
        needs_tos_creds=False,
        needs_ark_key=True,
    )
    config = {"dir": ROOT / "nano-banana", "port": 18797, "spec": spec}

    with mock.patch.object(manager, "_kill_port_squatter"), \
         mock.patch.object(manager, "_read_ark_key", return_value="ark-child-key"), \
         mock.patch.object(module.subprocess, "Popen", return_value=proc) as popen:
        manager.start_app("nano-banana", config)

    child_env = popen.call_args.kwargs["env"]
    assert child_env["VOLCENGINE_ARK_API_KEY"] == "ark-child-key"
    assert os.environ.get("VOLCENGINE_ARK_API_KEY") != "ark-child-key"
    manager.log_handles["nano-banana"].close()
