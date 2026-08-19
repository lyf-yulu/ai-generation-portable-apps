import { useEffect, useMemo, useRef, useState } from "react";
import { saveAs } from "file-saver";

import {
    changeAdminComfyWorkflowLifecycle,
    exportAdminComfyWorkflow,
    fetchAdminComfyWorkflow,
    fetchAdminComfyWorkflowCapabilities,
    fetchAdminComfyWorkflows,
    replaceAdminUserComfyWorkflows,
    type AdminComfyWorkflow,
    type ComfyWorkflowCapabilities,
    type WorkflowFormat,
    type WorkflowRevision,
} from "@/api/comfy-workflows";
import { fetchAdminUsers, type AdminUser } from "@/api/admin";
import { WorkflowImport } from "@/components/comfy/workflow-import";
import { WorkflowPreview } from "@/components/comfy/workflow-preview";

const lifecycleLabel = (workflow: AdminComfyWorkflow) => (workflow.lifecycle.archived ? "已归档" : workflow.lifecycle.enabled ? "已启用" : "已停用");
const unavailableCapabilities: ComfyWorkflowCapabilities = {
    assignments: { available: false, reason: "PORTAL_USER_DIRECTORY_UNAVAILABLE" },
    services: [],
};

function compatibilityMessage(workflow: AdminComfyWorkflow, revision: WorkflowRevision, capabilities: ComfyWorkflowCapabilities) {
    const service = capabilities.services.find((item) => item.service_id === workflow.service_id);
    if (!service) return "服务未配置，无法验证节点兼容性。";
    if (service.status === "unavailable") return "服务不可用，无法验证节点兼容性。";
    if (service.status === "misconfigured") return "服务配置不可用，无法验证节点兼容性。";
    const missing = revision.dependencies.node_types.filter((dependency) => !service.node_types.includes(dependency.type));
    if (missing.length) return `缺少节点：${missing.map((dependency) => dependency.type).join("、")}。`;
    return "服务健康，所需节点已验证；执行仍在后续切片中保持禁用。";
}

export default function AdminComfyWorkflowsPage() {
    const [workflows, setWorkflows] = useState<AdminComfyWorkflow[]>([]);
    const [users, setUsers] = useState<AdminUser[]>([]);
    const [capabilities, setCapabilities] = useState<ComfyWorkflowCapabilities>(unavailableCapabilities);
    const [selectedId, setSelectedId] = useState("");
    const [revision, setRevision] = useState<WorkflowRevision | null>(null);
    const [assignments, setAssignments] = useState<Record<string, string[]>>({});
    const [dirtyUsers, setDirtyUsers] = useState<Set<string>>(new Set());
    const [status, setStatus] = useState<"loading" | "ready" | "saving" | "failed">("loading");
    const requestVersion = useRef(0);
    const selected = useMemo(() => workflows.find((workflow) => workflow.workflow_id === selectedId) || null, [workflows, selectedId]);

    const load = async () => {
        const version = ++requestVersion.current;
        setStatus("loading");
        try {
            const [nextWorkflows, nextCapabilities] = await Promise.all([
                fetchAdminComfyWorkflows(),
                fetchAdminComfyWorkflowCapabilities().catch(() => unavailableCapabilities),
            ]);
            const nextUsers = nextCapabilities.assignments.available ? await fetchAdminUsers() : [];
            if (requestVersion.current !== version) return;
            setWorkflows(nextWorkflows);
            setCapabilities(nextCapabilities);
            setUsers(nextUsers);
            setAssignments(Object.fromEntries(nextUsers.map((user) => [user.user_id, [...user.comfy_workflow_ids]])));
            setStatus("ready");
        } catch {
            if (requestVersion.current === version) setStatus("failed");
        }
    };
    useEffect(() => {
        void load();
        return () => {
            requestVersion.current += 1;
        };
    }, []);
    useEffect(() => {
        if (!selectedId) {
            setRevision(null);
            return;
        }
        const version = ++requestVersion.current;
        setRevision(null);
        void fetchAdminComfyWorkflow(selectedId)
            .then((workflow) => {
                if (requestVersion.current !== version) return;
                setWorkflows((current) => current.map((item) => (item.workflow_id === workflow.workflow_id ? workflow : item)));
                setRevision(workflow.current_revision || null);
            })
            .catch(() => {
                if (requestVersion.current === version) setStatus("failed");
            });
    }, [selectedId]);

    const replaceWorkflow = (updated: AdminComfyWorkflow) => {
        setWorkflows((current) => current.map((item) => (item.workflow_id === updated.workflow_id ? { ...item, ...updated } : item)));
        setRevision(null);
        setSelectedId("");
        queueMicrotask(() => setSelectedId(updated.workflow_id));
    };
    const lifecycle = async (action: "enable" | "disable" | "archive" | "restore") => {
        if (!selected) return;
        setStatus("saving");
        try {
            replaceWorkflow(await changeAdminComfyWorkflowLifecycle(selected.workflow_id, action, selected.lifecycle_revision));
            setStatus("ready");
        } catch {
            setStatus("failed");
        }
    };
    const download = async (format: WorkflowFormat) => {
        if (!selected || !revision) return;
        setStatus("saving");
        try {
            const { blob, filename } = await exportAdminComfyWorkflow(selected.workflow_id, revision.revision, format);
            saveAs(blob, filename);
            setStatus("ready");
        } catch {
            setStatus("failed");
        }
    };
    const toggleAssignment = (userId: string) => {
        if (!selected) return;
        setAssignments((current) => {
            const currentIds = current[userId] || [];
            const workflowIds = currentIds.includes(selected.workflow_id) ? currentIds.filter((id) => id !== selected.workflow_id) : [...currentIds, selected.workflow_id];
            return { ...current, [userId]: workflowIds };
        });
        setDirtyUsers((current) => new Set(current).add(userId));
    };
    const saveAssignments = async () => {
        if (!dirtyUsers.size) return;
        setStatus("saving");
        try {
            await Promise.all([...dirtyUsers].map((userId) => replaceAdminUserComfyWorkflows(userId, assignments[userId] || [])));
            setDirtyUsers(new Set());
            setStatus("ready");
        } catch {
            setStatus("failed");
        }
    };

    return (
        <div className="mx-auto max-w-7xl p-5 md:p-8">
            <header>
                <h1 className="text-2xl font-semibold">ComfyUI 工作流库</h1>
                <p className="mt-1 text-sm text-[#86a991]">管理员管理受控模板、只读预览、兼容性和账号派发。工作流不会在浏览器中执行。</p>
            </header>
            <div className="mt-6">
                <WorkflowImport
                    onImported={(imported) => {
                        setWorkflows((current) => [...current, imported]);
                        setSelectedId(imported.workflow_id);
                    }}
                />
            </div>
            {status === "failed" && (
                <p role="alert" className="mt-4 text-sm text-[#ffbd73]">
                    操作未完成，请重试。
                </p>
            )}
            <div className="mt-6 grid gap-5 lg:grid-cols-[18rem_1fr]">
                <section className="rounded-xl border border-[#245a35] bg-[#07110b] p-4">
                    <h2 className="font-semibold">模板列表</h2>
                    <div className="mt-3 space-y-2">
                        {workflows.map((workflow) => (
                            <button
                                type="button"
                                key={workflow.workflow_id}
                                onClick={() => setSelectedId(workflow.workflow_id)}
                                className={`w-full rounded border p-3 text-left text-sm ${selectedId === workflow.workflow_id ? "border-[#58ed87] bg-[#102619]" : "border-[#1e482b] hover:bg-[#0d1b12]"}`}
                            >
                                <span className="block font-medium">{workflow.display_name}</span>
                                <span className="mt-1 block text-xs text-[#86a991]">
                                    r{workflow.revision} · {lifecycleLabel(workflow)}
                                </span>
                            </button>
                        ))}
                    </div>
                    {status === "loading" && <p className="mt-3 text-sm text-[#86a991]">正在加载…</p>}
                    {status === "ready" && !workflows.length && <p className="mt-3 text-sm text-[#86a991]">尚未导入工作流。</p>}
                </section>
                <section className="min-w-0 rounded-xl border border-[#245a35] bg-[#07110b] p-4">
                    {!selected && <p className="text-sm text-[#86a991]">选择一个工作流以查看安全投影。</p>}
                    {selected && (
                        <>
                            <div className="flex flex-wrap items-start justify-between gap-3">
                                <div>
                                    <h2 className="text-xl font-semibold">{selected.display_name}</h2>
                                    <p className="mt-1 text-sm text-[#a9c6b0]">
                                        版本 r{selected.revision} · {lifecycleLabel(selected)} · 服务标识 {selected.service_id}
                                    </p>
                                </div>
                                <div className="flex flex-wrap gap-2">
                                    {selected.lifecycle.archived ? (
                                        <button type="button" onClick={() => void lifecycle("restore")} className="rounded border border-[#3a7650] px-3 py-1.5 text-sm text-[#8ff0aa]">
                                            恢复
                                        </button>
                                    ) : (
                                        <>
                                            {selected.lifecycle.enabled ? (
                                                <button type="button" onClick={() => void lifecycle("disable")} className="rounded border border-[#3a7650] px-3 py-1.5 text-sm">
                                                    停用
                                                </button>
                                            ) : (
                                                <button type="button" onClick={() => void lifecycle("enable")} className="rounded border border-[#3a7650] px-3 py-1.5 text-sm text-[#8ff0aa]">
                                                    启用
                                                </button>
                                            )}
                                            <button type="button" onClick={() => void lifecycle("archive")} className="rounded border border-[#6c6131] px-3 py-1.5 text-sm text-[#eadc91]">
                                                归档
                                            </button>
                                        </>
                                    )}
                                </div>
                            </div>
                            {!revision && <p className="mt-5 text-sm text-[#86a991]">正在加载安全预览…</p>}
                            {revision && (
                                <div className="mt-5 space-y-5">
                                    <dl className="grid gap-3 text-sm sm:grid-cols-3">
                                        <div>
                                            <dt className="text-[#86a991]">格式</dt>
                                            <dd>{revision.formats.join("、")}</dd>
                                        </div>
                                        <div>
                                            <dt className="text-[#86a991]">校验和前缀</dt>
                                            <dd className="font-mono">{revision.checksum_prefix}</dd>
                                        </div>
                                        <div>
                                            <dt className="text-[#86a991]">执行状态</dt>
                                            <dd>{revision.execution_available ? "可用" : "当前切片未启用执行"}</dd>
                                        </div>
                                    </dl>
                                    <p className="rounded border border-[#285038] bg-[#0b1710] px-3 py-2 text-sm text-[#b9d0c0]">
                                        兼容性：{compatibilityMessage(selected, revision, capabilities)}
                                    </p>
                                    <WorkflowPreview preview={revision.preview} />
                                    <section>
                                        <h3 className="font-semibold">依赖状态</h3>
                                        <ul className="mt-2 flex flex-wrap gap-2">
                                            {revision.dependencies.node_types.map((dependency) => (
                                                <li key={dependency.type} className="rounded border border-[#285038] px-2 py-1 text-xs">
                                                    {dependency.type} · <span className={dependency.is_core ? "text-[#8ff0aa]" : "text-[#eadc91]"}>{dependency.is_core ? "核心节点" : "需确认"}</span>
                                                </li>
                                            ))}
                                        </ul>
                                    </section>
                                    <section>
                                        <h3 className="font-semibold">导出</h3>
                                        <div className="mt-2 flex flex-wrap gap-2">
                                            {revision.formats.map((format) => (
                                                <button key={format} type="button" onClick={() => void download(format)} className="rounded border border-[#3a7650] px-3 py-1.5 text-sm">
                                                    下载 {format} JSON
                                                </button>
                                            ))}
                                        </div>
                                    </section>
                                    <section className="border-t border-[#1e482b] pt-5">
                                        <h3 className="font-semibold">账号派发</h3>
                                        {capabilities.assignments.available ? (
                                            <>
                                                <p className="mt-1 text-xs text-[#86a991]">只有已派发且启用的工作流会出现在普通用户的可用模板中。</p>
                                                <div className="mt-3 space-y-2">
                                                    {users
                                                        .filter((user) => user.role !== "admin")
                                                        .map((user) => (
                                                            <label key={user.user_id} className="flex items-center gap-2 text-sm">
                                                                <input
                                                                    aria-label={`向${user.display_name}派发 ${selected.display_name}`}
                                                                    type="checkbox"
                                                                    checked={(assignments[user.user_id] || []).includes(selected.workflow_id)}
                                                                    onChange={() => toggleAssignment(user.user_id)}
                                                                    disabled={status === "saving"}
                                                                    className="accent-[#58ed87]"
                                                                />
                                                                {user.display_name} <span className="text-xs text-[#86a991]">{user.username}</span>
                                                            </label>
                                                        ))}
                                                </div>
                                                <button
                                                    type="button"
                                                    disabled={!dirtyUsers.size || status === "saving"}
                                                    onClick={() => void saveAssignments()}
                                                    className="mt-3 rounded bg-[#42d977] px-4 py-2 text-sm font-semibold text-[#041008] disabled:opacity-40"
                                                >
                                                    {status === "saving" ? "正在保存派发…" : "保存派发"}
                                                </button>
                                            </>
                                        ) : (
                                            <p className="mt-1 text-xs text-[#eadc91]">派发不可用：当前 Portal 身份只能验证请求用户，尚未配置受验证的用户目录。</p>
                                        )}
                                    </section>
                                </div>
                            )}
                        </>
                    )}
                </section>
            </div>
        </div>
    );
}
