"""AppSpec: declarative sub-app registry.

Loaded once at portal startup from portal/apps.json. Backend and frontend
both read from this single source of truth — adding a new sub-app becomes
"drop a folder + append one JSON entry" instead of touching a dozen
hardcoded call sites.

L2 abstraction (2026-07-16): the fields express *capabilities* (needs TOS
creds? uses AK/SK? has an admin key page?) so portal can dispatch without
`if name == "seedance"` branches.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional


Mount = Literal["iframe", "component"]
CredentialScheme = Literal["api_key", "ak_sk", "none"]
JobType = Literal["image", "video", "dynamic"]
StatsCombine = Literal["images_or_seconds", "images_and_seconds"]
Metric = Literal["images", "seconds"]


@dataclass(frozen=True)
class JobTypeRule:
    """For job_type=dynamic: if any of the keywords appears in the proxied
    target_path, classify the job as `type`."""
    keywords: tuple[str, ...]
    type: Literal["image", "video"]


@dataclass(frozen=True)
class AppSpec:
    name: str
    display_name: str
    dir_path: Path                       # absolute path to sub-app directory
    port_env: str                        # e.g. "SEEDANCE_PORT"
    port_default: int                    # e.g. 8787

    mount: Mount = "iframe"
    iframe_url: Optional[str] = None     # mount=iframe
    component_factory: Optional[str] = None  # mount=component (documentation only, not runtime)

    color: str = "#666"
    admin_permission: Optional[str] = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    personal_key_disabled: bool = False
    credential_scheme: CredentialScheme = "api_key"
    company_key_endpoint: Optional[str] = None  # e.g. "portrait-key" -> /api/platform/portrait-key
    needs_tos_creds: bool = False
    is_tos_source: bool = False
    job_type: JobType = "image"
    job_type_rules: tuple[JobTypeRule, ...] = ()
    metrics: tuple[Metric, ...] = ("images",)
    unit_label: str = "张"
    stats_combine: StatsCombine = "images_or_seconds"

    @property
    def port(self) -> int:
        """Live port with env override — matches the legacy
        `int(os.environ.get("SEEDANCE_PORT", "8787"))` idiom."""
        try:
            return int(os.environ.get(self.port_env, str(self.port_default)))
        except (TypeError, ValueError):
            return self.port_default


def _rule_from_dict(d: dict[str, Any]) -> JobTypeRule:
    return JobTypeRule(
        keywords=tuple(d.get("keywords") or ()),
        type=d.get("type", "image"),
    )


def _spec_from_dict(d: dict[str, Any], repo_root: Path) -> AppSpec:
    name = d["name"]
    dir_str = d.get("dir") or name
    return AppSpec(
        name=name,
        display_name=d.get("display_name") or name,
        dir_path=repo_root / dir_str,
        port_env=d["port_env"],
        port_default=int(d["port_default"]),
        mount=d.get("mount", "iframe"),
        iframe_url=d.get("iframe_url"),
        component_factory=d.get("component_factory"),
        color=d.get("color", "#666"),
        admin_permission=d.get("admin_permission"),
        extra_headers=dict(d.get("extra_headers") or {}),
        personal_key_disabled=bool(d.get("personal_key_disabled", False)),
        credential_scheme=d.get("credential_scheme", "api_key"),
        company_key_endpoint=d.get("company_key_endpoint"),
        needs_tos_creds=bool(d.get("needs_tos_creds", False)),
        is_tos_source=bool(d.get("is_tos_source", False)),
        job_type=d.get("job_type", "image"),
        job_type_rules=tuple(_rule_from_dict(r) for r in (d.get("job_type_rules") or [])),
        metrics=tuple(d.get("metrics") or ["images"]),
        unit_label=d.get("unit_label", "张"),
        stats_combine=d.get("stats_combine", "images_or_seconds"),
    )


def load_specs(json_path: Path, repo_root: Path) -> list[AppSpec]:
    """Load and validate apps.json. Order is preserved — it drives the tab
    order in the frontend."""
    raw = json.loads(json_path.read_text("utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{json_path}: expected top-level JSON array")
    specs = [_spec_from_dict(d, repo_root) for d in raw]
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError(f"{json_path}: duplicate app names: {names}")
    return specs


def classify_job_type(spec: AppSpec, target_path: str) -> Literal["image", "video"]:
    """Map an app's job to image|video for usage stats. Matches the legacy
    hardcoded logic in _proxy (portal/app.py 1751-1757) exactly."""
    if spec.job_type == "image":
        return "image"
    if spec.job_type == "video":
        return "video"
    for rule in spec.job_type_rules:
        if any(k in target_path for k in rule.keywords):
            return rule.type
    return "video"  # dreamina default: anything not matched (frame/video/etc) is video


def resolve_extra_headers(spec: AppSpec, user: dict, has_permission) -> dict[str, str]:
    """Expand `{perm:xxx}` placeholders in extra_headers values.

    Example: {"X-Dreamina-Manage": "{perm:use_apps}"} becomes
    {"X-Dreamina-Manage": "1"} if user has use_apps, else it's dropped.
    """
    resolved: dict[str, str] = {}
    for k, v in spec.extra_headers.items():
        if v.startswith("{perm:") and v.endswith("}"):
            perm = v[len("{perm:"):-1]
            if has_permission(user, perm):
                resolved[k] = "1"
        else:
            resolved[k] = v
    return resolved
