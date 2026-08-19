"""Feishu client against a fake opener: assert request shapes + token retry.

No network. We inject an `opener(req, timeout)` that records each urllib
Request and returns a canned JSON body, mimicking the Feishu API envelope
{"code":0,"data":{...}}.
"""
import io
import json

import pytest

from feishu import FeishuClient, FeishuError, TOKEN_INVALID_CODE


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class FakeFeishu:
    """Scripted opener. Maps path -> list of responses (popped in order)."""
    def __init__(self):
        self.calls = []            # list of (method, path, headers, body_bytes)
        self._responses = {}       # path -> list[dict]

    def queue(self, path, *payloads):
        self._responses.setdefault(path, []).extend(payloads)

    def __call__(self, req, timeout=None):
        method = req.get_method()
        full = req.full_url
        # strip host for matching; record full path (with query) for asserts
        path = full.split("open.feishu.cn", 1)[-1]
        self.calls.append((method, path, dict(req.headers), req.data))
        # queue lookup ignores the query string, mirroring a real router that
        # dispatches on path and reads ?type= as a param.
        route = path.split("?", 1)[0]
        # token endpoint default
        if route.endswith("/tenant_access_token/internal"):
            payload = {"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
        else:
            queue = self._responses.get(route)
            if not queue:
                raise AssertionError(f"no scripted response for {method} {path}")
            payload = queue.pop(0)
        return _FakeResp(json.dumps(payload).encode("utf-8"))

    def paths(self):
        return [p for (_, p, _, _) in self.calls]

    def body_for(self, path_suffix):
        for (_, p, _, data) in self.calls:
            route = p.split("?", 1)[0]
            if route.endswith(path_suffix) and data:
                return json.loads(data.decode("utf-8"))
        return None


def _client(fake):
    return FeishuClient("cli_x", "secret", folder_token="fld_1", opener=fake)


class _FakeRegistry:
    def __init__(self):
        self.bases = {}

    def get_user_base(self, user):
        return self.bases.get(user)

    def save_user_base(self, user, app_token, table_ids, authorized,
                       open_id=None):
        self.bases[user] = {
            "app_token": app_token, "table_ids": table_ids,
            "authorized": authorized,
        }


def test_create_base_app_sends_name_and_folder():
    fake = FakeFeishu()
    fake.queue("/open-apis/bitable/v1/apps",
               {"code": 0, "data": {"app": {"app_token": "app_1"}}})
    tok = _client(fake).create_base_app("苏湘的AI产出")
    assert tok == "app_1"
    body = fake.body_for("/bitable/v1/apps")
    assert body["name"] == "苏湘的AI产出"
    assert body["folder_token"] == "fld_1"


def test_create_table_posts_fields():
    fake = FakeFeishu()
    fake.queue("/open-apis/bitable/v1/apps/app_1/tables",
               {"code": 0, "data": {"table_id": "tbl_1"}})
    tid = _client(fake).create_table("app_1", "Seedance(视频)")
    assert tid == "tbl_1"
    body = fake.body_for("/apps/app_1/tables")
    assert body["table"]["name"] == "Seedance(视频)"
    names = [f["field_name"] for f in body["table"]["fields"]]
    assert "结果" in names and "文件名" in names and "子应用" in names


def test_set_org_editable_shape():
    fake = FakeFeishu()
    fake.queue("/open-apis/drive/v1/permissions/app_1/public",
               {"code": 0, "data": {}})
    _client(fake).set_org_editable("app_1")
    body = fake.body_for("/permissions/app_1/public")
    assert body["link_share_entity"] == "tenant_editable"
    # regression: type=bitable MUST be a query param or Feishu 400s
    assert any("/permissions/app_1/public?type=bitable" in p
               for p in fake.paths())


def test_ensure_base_full_flow_creates_and_shares_org():
    fake = FakeFeishu()
    fake.queue("/open-apis/bitable/v1/apps",
               {"code": 0, "data": {"app": {"app_token": "app_1"}}})
    for _ in range(4):  # four sub-app tables
        fake.queue("/open-apis/bitable/v1/apps/app_1/tables",
                   {"code": 0, "data": {"table_id": "tbl"}})
    fake.queue("/open-apis/drive/v1/permissions/app_1/public",
               {"code": 0, "data": {}})
    reg = _FakeRegistry()
    app_token, tables = _client(fake).ensure_base_for_user("苏湘", reg)
    assert app_token == "app_1"
    assert set(tables) == {"seedance", "nano-banana", "dreamina",
                           "volcengine-portrait"}
    assert reg.bases["苏湘"]["authorized"] is True
    # org-share must have been called
    assert any("/permissions/app_1/public" in p for p in fake.paths())


def test_ensure_base_reuses_existing():
    fake = FakeFeishu()
    reg = _FakeRegistry()
    reg.bases["苏湘"] = {"app_token": "old", "table_ids": {"seedance": "t"},
                        "authorized": True}
    app_token, tables = _client(fake).ensure_base_for_user("苏湘", reg)
    assert app_token == "old"
    # no create calls at all
    assert not any("/bitable/v1/apps" in p for p in fake.paths())


def test_ensure_base_retries_failed_org_share():
    # base exists but authorized=False (org-share failed last round) -> retry
    fake = FakeFeishu()
    fake.queue("/open-apis/drive/v1/permissions/app_x/public",
               {"code": 0, "data": {}})
    reg = _FakeRegistry()
    reg.bases["苏湘"] = {"app_token": "app_x",
                        "table_ids": {"seedance": "t"}, "authorized": False}
    _client(fake).ensure_base_for_user("苏湘", reg)
    assert reg.bases["苏湘"]["authorized"] is True
    assert any("/permissions/app_x/public" in p for p in fake.paths())


def test_add_record_wraps_attachment_token():
    fake = FakeFeishu()
    fake.queue("/open-apis/bitable/v1/apps/app_1/tables/tbl_1/records",
               {"code": 0, "data": {"record": {"record_id": "rec_1"}}})
    rid = _client(fake).add_record(
        "app_1", "tbl_1", {"文件名": "x.mp4", "子应用": "seedance"}, "ftok_1"
    )
    assert rid == "rec_1"
    body = fake.body_for("/tbl_1/records")
    assert body["fields"]["结果"] == [{"file_token": "ftok_1"}]
    assert body["fields"]["文件名"] == "x.mp4"


def test_token_invalid_triggers_refresh_and_retry():
    fake = FakeFeishu()
    # first records call returns token-invalid, second succeeds
    fake.queue("/open-apis/bitable/v1/apps/app_1/tables/tbl_1/records",
               {"code": TOKEN_INVALID_CODE, "msg": "invalid token"},
               {"code": 0, "data": {"record": {"record_id": "rec_ok"}}})
    rid = _client(fake).add_record("app_1", "tbl_1", {"文件名": "x"}, "ft")
    assert rid == "rec_ok"
    # token endpoint hit at least twice (initial + refresh)
    token_calls = [p for p in fake.paths()
                   if p.endswith("/tenant_access_token/internal")]
    assert len(token_calls) >= 2


def test_api_error_raises():
    fake = FakeFeishu()
    fake.queue("/open-apis/bitable/v1/apps",
               {"code": 1254001, "msg": "no permission"})
    with pytest.raises(FeishuError) as ei:
        _client(fake).create_base_app("x")
    assert ei.value.code == 1254001


def test_upload_all_small_file(tmp_path):
    fake = FakeFeishu()
    fake.queue("/open-apis/drive/v1/medias/upload_all",
               {"code": 0, "data": {"file_token": "ft_small"}})
    f = tmp_path / "x.png"
    f.write_bytes(b"imagedata")
    tok = _client(fake).upload_media("app_1", f)
    assert tok == "ft_small"
    # multipart body carries parent_node = app_token
    assert any("/medias/upload_all" in p for p in fake.paths())
