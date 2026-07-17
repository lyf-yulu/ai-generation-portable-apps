from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    run_id: str
    thread_id: str
    source_url: str
    source_type: str
    source_token: str
    document_id: str
    document_title: str
    document_revision: int
    status: str
    requester_open_id: str | None
    trigger_type: str
    reply_context: dict[str, str]
    requirement_request: dict[str, Any]
    source_document: dict[str, Any]
    normalized_document: dict[str, Any]
    media_assets: list[dict[str, Any]]
    source_revision: int
    vision_descriptions: list[dict[str, Any]]
    vision_issues: list[str]
    draft_plan: dict[str, Any]
    task_plan: dict[str, Any]
    audit_report: dict[str, Any]
    validation_issues: list[str]
    planner_feedback: str | None
    approval_decision: dict[str, Any] | None
    approval_revision: int
    approved_tasks: list[dict[str, Any]]
    execution_records: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    delivery_record: dict[str, Any] | None
    last_error: dict[str, Any] | None
    error: dict[str, Any]
