"""SQLite 存储：画布存档（projects）与素材（assets）。

只有两张表，用标准库 sqlite3，不引 ORM（与仓库既有子应用风格一致）。
两张表的主键都含 user_id —— 归属隔离是结构性的，查询漏带 user_id 会查不到
而不是查到别人的数据。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
DB_PATH = STATE_DIR / "canvas.sqlite3"
UPLOAD_DIR = STATE_DIR / "uploads"

# 单个画布文档序列化上限。画布是本地优先的，超大文档通常意味着有节点在
# 内联 base64 而不是引用 asset。
MAX_DOCUMENT_BYTES = 1 * 1024 * 1024
MAX_NODES = 1000
MAX_CONNECTIONS = 2000

_local = threading.local()


class ConflictError(Exception):
    """乐观锁版本不匹配。调用方必须转成 code=PROJECT_CONFLICT 的 409。"""


class NotFoundError(Exception):
    pass


class DocumentTooLarge(Exception):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect() -> sqlite3.Connection:
    """每线程一个连接。WAL 是必须的：uvicorn 并发下没有它会频繁 database is locked。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _local.conn = conn
    return conn


def init_schema() -> None:
    conn = connect()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canvas_projects (
                project_id    TEXT NOT NULL,
                user_id       TEXT NOT NULL,
                title         TEXT NOT NULL,
                document_json TEXT NOT NULL,
                version       INTEGER NOT NULL,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (user_id, project_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canvas_assets (
                asset_id   TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                media_type TEXT NOT NULL,
                mime_type  TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                path       TEXT NOT NULL,
                origin     TEXT NOT NULL DEFAULT 'upload',
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, asset_id)
            )
            """
        )
        # 素材库（kind=library）相关列的增量迁移：老库没有这些列，ALTER 补齐。
        # 只支持单次 ADD COLUMN，逐个判断。
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(canvas_assets)")}
        if "kind" not in columns:
            conn.execute("ALTER TABLE canvas_assets ADD COLUMN kind TEXT NOT NULL DEFAULT 'reference'")
        if "status" not in columns:
            conn.execute("ALTER TABLE canvas_assets ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "service_id" not in columns:
            conn.execute("ALTER TABLE canvas_assets ADD COLUMN service_id TEXT")
        if "upstream_asset_id" not in columns:
            conn.execute("ALTER TABLE canvas_assets ADD COLUMN upstream_asset_id TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canvas_jobs (
                job_id        TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                username      TEXT NOT NULL,
                app           TEXT NOT NULL,
                upstream_id   TEXT,
                operation     TEXT NOT NULL,
                task_type     TEXT NOT NULL,
                status        TEXT NOT NULL,
                done          INTEGER NOT NULL DEFAULT 0,
                total         INTEGER NOT NULL DEFAULT 0,
                duration      INTEGER NOT NULL DEFAULT 0,
                results_json  TEXT NOT NULL DEFAULT '[]',
                error_message TEXT,
                idempotency_key TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_idem ON canvas_jobs(user_id, idempotency_key)"
        )
        # ComfyUI 工作流库（与上游 canvas_comfy_workflows* 表同构）。
        # access 表为保持 schema 一致保留；当前放行策略是团队全员可见（见
        # assigned_comfy_workflows 的说明），不需要逐人授权记录。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canvas_comfy_workflows (
                workflow_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, description TEXT NOT NULL,
                service_id TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                archived_at TEXT, revision INTEGER NOT NULL CHECK(revision >= 1),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canvas_comfy_workflow_revisions (
                workflow_id TEXT NOT NULL REFERENCES canvas_comfy_workflows(workflow_id) ON DELETE RESTRICT,
                revision INTEGER NOT NULL, source_filename TEXT NOT NULL, editor_json TEXT, api_json TEXT,
                editor_checksum TEXT, api_checksum TEXT, node_inventory_json TEXT NOT NULL,
                dependency_inventory_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(workflow_id, revision)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canvas_comfy_workflow_access (
                user_id TEXT NOT NULL, workflow_id TEXT NOT NULL REFERENCES canvas_comfy_workflows(workflow_id) ON DELETE RESTRICT,
                granted_by TEXT NOT NULL, granted_at TEXT NOT NULL, revoked_at TEXT,
                PRIMARY KEY(user_id, workflow_id)
            )
            """
        )


# ---------------------------------------------------------------- projects

def _validate_document(document: dict) -> str:
    nodes = document.get("nodes")
    connections = document.get("connections")
    if isinstance(nodes, list) and len(nodes) > MAX_NODES:
        raise DocumentTooLarge(f"节点数超过上限 {MAX_NODES}")
    if isinstance(connections, list) and len(connections) > MAX_CONNECTIONS:
        raise DocumentTooLarge(f"连线数超过上限 {MAX_CONNECTIONS}")
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLarge("画布文档超过 1 MiB")
    return encoded


def list_projects(user_id: str) -> list[dict]:
    rows = connect().execute(
        "SELECT project_id, document_json, version FROM canvas_projects "
        "WHERE user_id=? ORDER BY updated_at DESC",
        (user_id,),
    ).fetchall()
    return [
        {"project": json.loads(row["document_json"]), "version": int(row["version"])}
        for row in rows
    ]


def get_project(user_id: str, project_id: str) -> dict:
    row = connect().execute(
        "SELECT document_json, version FROM canvas_projects WHERE user_id=? AND project_id=?",
        (user_id, project_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(project_id)
    return {"project": json.loads(row["document_json"]), "version": int(row["version"])}


def create_project(user_id: str, document: dict) -> tuple[dict, bool]:
    """返回 (envelope, created)。已存在且内容相同视为幂等重试，返回现有版本。"""
    project_id = str(document.get("id") or "")
    title = str(document.get("title") or "")[:200]
    encoded = _validate_document(document)
    conn = connect()
    now = _now()
    existing = conn.execute(
        "SELECT document_json, version FROM canvas_projects WHERE user_id=? AND project_id=?",
        (user_id, project_id),
    ).fetchone()
    if existing is not None:
        # 幂等重试：内容一致就回现有版本，不报冲突。
        if existing["document_json"] == encoded:
            return {"project": json.loads(existing["document_json"]), "version": int(existing["version"])}, False
        raise ConflictError(project_id)
    with conn:
        conn.execute(
            "INSERT INTO canvas_projects (project_id,user_id,title,document_json,version,created_at,updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            (project_id, user_id, title, encoded, now, now),
        )
    return {"project": document, "version": 1}, True


def update_project(user_id: str, project_id: str, document: dict, expected_version: int) -> dict:
    title = str(document.get("title") or "")[:200]
    encoded = _validate_document(document)
    conn = connect()
    with conn:
        # 单条带版本条件的 UPDATE 即原子比较交换，不需要显式读-改-写事务。
        cur = conn.execute(
            "UPDATE canvas_projects SET title=?, document_json=?, version=version+1, updated_at=? "
            "WHERE user_id=? AND project_id=? AND version=?",
            (title, encoded, _now(), user_id, project_id, expected_version),
        )
    if cur.rowcount == 0:
        raise ConflictError(project_id)
    return {"project": document, "version": expected_version + 1}


def delete_project(user_id: str, project_id: str) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "DELETE FROM canvas_projects WHERE user_id=? AND project_id=?",
            (user_id, project_id),
        )


# ------------------------------------------------------------------ assets

def insert_asset(user_id: str, asset_id: str, media_type: str, mime_type: str,
                 size_bytes: int, path: str, origin: str = "upload",
                 kind: str = "reference", status: str = "active",
                 service_id: str | None = None, upstream_asset_id: str | None = None) -> dict:
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO canvas_assets (asset_id,user_id,media_type,mime_type,size_bytes,path,origin,"
            "created_at,kind,status,service_id,upstream_asset_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset_id, user_id, media_type, mime_type, size_bytes, path, origin, _now(),
             kind, status, service_id, upstream_asset_id),
        )
    return {
        "asset_id": asset_id,
        "kind": kind,
        "status": status,
        "media_type": media_type,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
    }


def update_asset_status(user_id: str, asset_id: str, status: str) -> dict | None:
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE canvas_assets SET status=? WHERE user_id=? AND asset_id=?",
            (status, user_id, asset_id),
        )
    row = conn.execute(
        "SELECT * FROM canvas_assets WHERE user_id=? AND asset_id=?",
        (user_id, asset_id),
    ).fetchone()
    return dict(row) if row is not None else None


def list_library_assets(user_id: str) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM canvas_assets WHERE user_id=? AND kind='library' "
        "ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_asset(user_id: str, asset_id: str) -> dict:
    row = connect().execute(
        "SELECT * FROM canvas_assets WHERE user_id=? AND asset_id=?",
        (user_id, asset_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(asset_id)
    return dict(row)


def list_assets(user_id: str, limit: int = 100) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM canvas_assets WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def delete_asset(user_id: str, asset_id: str) -> None:
    row = connect().execute(
        "SELECT path FROM canvas_assets WHERE user_id=? AND asset_id=?",
        (user_id, asset_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(asset_id)
    conn = connect()
    with conn:
        conn.execute("DELETE FROM canvas_assets WHERE user_id=? AND asset_id=?", (user_id, asset_id))
    try:
        os.unlink(row["path"])
    except OSError:
        pass  # 库记录已删，文件残留不影响正确性


# -------------------------------------------------------------------- jobs

def insert_job(job: dict) -> None:
    conn = connect()
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO canvas_jobs (job_id,user_id,username,app,upstream_id,operation,task_type,"
            "status,done,total,duration,results_json,error_message,idempotency_key,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job["job_id"], job["user_id"], job["username"], job["app"], job.get("upstream_id"),
             job["operation"], job["task_type"], job["status"], job.get("done", 0), job.get("total", 0),
             job.get("duration", 0), json.dumps(job.get("results", []), ensure_ascii=False),
             job.get("error_message"), job.get("idempotency_key"), now, now),
        )


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    if "results" in fields:
        fields["results_json"] = json.dumps(fields.pop("results"), ensure_ascii=False)
    columns = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values())
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE canvas_jobs SET {columns}, updated_at=? WHERE job_id=?",
            (*values, _now(), job_id),
        )


def get_job(job_id: str) -> dict | None:
    row = connect().execute("SELECT * FROM canvas_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["results"] = json.loads(job.pop("results_json") or "[]")
    return job


def find_job_by_idempotency(user_id: str, key: str) -> dict | None:
    if not key:
        return None
    row = connect().execute(
        "SELECT job_id FROM canvas_jobs WHERE user_id=? AND idempotency_key=?",
        (user_id, key),
    ).fetchone()
    return get_job(row["job_id"]) if row else None


def list_jobs(user_id: str, limit: int = 50) -> list[dict]:
    rows = connect().execute(
        "SELECT job_id FROM canvas_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [job for job in (get_job(row["job_id"]) for row in rows) if job]


# ------------------------------------------------------------ comfy workflows

def create_comfy_workflow(workflow_id: str, display_name: str, service_id: str,
                          actor_user_id: str, **revision_values) -> dict:
    """建模板 + revision 1，单事务原子写入。revision_values 见 comfy_lib。"""
    conn = connect()
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO canvas_comfy_workflows "
            "(workflow_id,display_name,description,service_id,enabled,archived_at,revision,created_at,updated_at) "
            "VALUES (?,?,?,?,1,NULL,1,?,?)",
            (workflow_id, display_name, "", service_id, now, now),
        )
        conn.execute(
            "INSERT INTO canvas_comfy_workflow_revisions "
            "(workflow_id,revision,source_filename,editor_json,api_json,editor_checksum,api_checksum,"
            "node_inventory_json,dependency_inventory_json,created_by,created_at) "
            "VALUES (?,1,?,?,?,?,?,?,?,?,?)",
            (workflow_id, revision_values["source_filename"], revision_values["editor_json"],
             revision_values["api_json"], revision_values["editor_checksum"],
             revision_values["api_checksum"], revision_values["node_inventory_json"],
             revision_values["dependency_inventory_json"], actor_user_id, now),
        )
    return get_comfy_workflow(workflow_id)


def add_comfy_workflow_revision(workflow_id: str, expected_revision: int,
                                actor_user_id: str, **revision_values) -> dict:
    """乐观锁追加版本。模板 revision 不匹配抛 WORKFLOW_REVISION_CONFLICT；
    版本号已存在抛 WORKFLOW_DUPLICATE_REVISION（语义与上游 library.py 一致）。"""
    conn = connect()
    now = _now()
    with conn:
        cur = conn.execute(
            "UPDATE canvas_comfy_workflows SET revision=revision+1, updated_at=? "
            "WHERE workflow_id=? AND revision=?",
            (now, workflow_id, expected_revision),
        )
        if cur.rowcount == 0:
            raise ValueError(
                "WORKFLOW_DUPLICATE_REVISION"
                if conn.execute("SELECT 1 FROM canvas_comfy_workflow_revisions "
                                "WHERE workflow_id=? AND revision=?",
                                (workflow_id, expected_revision + 1)).fetchone()
                else "WORKFLOW_REVISION_CONFLICT")
        conn.execute(
            "INSERT INTO canvas_comfy_workflow_revisions "
            "(workflow_id,revision,source_filename,editor_json,api_json,editor_checksum,api_checksum,"
            "node_inventory_json,dependency_inventory_json,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (workflow_id, expected_revision + 1, revision_values["source_filename"],
             revision_values["editor_json"], revision_values["api_json"],
             revision_values["editor_checksum"], revision_values["api_checksum"],
             revision_values["node_inventory_json"],
             revision_values["dependency_inventory_json"], actor_user_id, now),
        )
    return get_comfy_workflow(workflow_id)


def set_comfy_workflow_lifecycle(workflow_id: str, expected_revision: int,
                                 enabled: bool | None = None,
                                 archived: bool | None = None) -> dict:
    conn = connect()
    now = _now()
    with conn:
        sets, values = [], []
        if enabled is not None:
            sets.append("enabled=?")
            values.append(1 if enabled else 0)
        if archived is not None:
            sets.append("archived_at=?")
            values.append(now if archived else None)
        sets.append("updated_at=?")
        values.append(now)
        cur = conn.execute(
            f"UPDATE canvas_comfy_workflows SET {', '.join(sets)} "
            "WHERE workflow_id=? AND revision=?",
            (*values, workflow_id, expected_revision),
        )
        if cur.rowcount == 0:
            raise ValueError("WORKFLOW_REVISION_CONFLICT")
    return get_comfy_workflow(workflow_id)


def get_comfy_workflow(workflow_id: str) -> dict:
    row = connect().execute(
        "SELECT * FROM canvas_comfy_workflows WHERE workflow_id=?",
        (workflow_id,),
    ).fetchone()
    if row is None:
        raise KeyError(workflow_id)
    return dict(row)


def list_comfy_workflows() -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM canvas_comfy_workflows ORDER BY created_at DESC",
    ).fetchall()
    return [dict(row) for row in rows]


def comfy_workflow_revision(workflow_id: str, revision: int) -> dict | None:
    row = connect().execute(
        "SELECT * FROM canvas_comfy_workflow_revisions WHERE workflow_id=? AND revision=?",
        (workflow_id, revision),
    ).fetchone()
    return dict(row) if row is not None else None


def assigned_comfy_workflows() -> list[dict]:
    """当前放行策略：启用且未归档的工作流对全员可见。

    与上游的逐人授权不同（Portal 模式下没有用户目录、授权不可用），
    团队内网部署下全员共享更符合使用场景。日后需要按人授权时，
    改回读 canvas_comfy_workflow_access 即可，表结构已就位。
    """
    rows = connect().execute(
        "SELECT * FROM canvas_comfy_workflows WHERE enabled=1 AND archived_at IS NULL "
        "ORDER BY created_at DESC",
    ).fetchall()
    return [dict(row) for row in rows]
