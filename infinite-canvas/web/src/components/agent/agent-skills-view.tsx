import { useEffect, useMemo, useRef, useState } from "react";
import { App, Button, Collapse, Dropdown, Form, Input, Modal, Select, Switch, Tooltip } from "antd";
import type { MenuProps } from "antd";
import { Check, ChevronDown, CircleAlert, FilePenLine, LoaderCircle, LockKeyhole, MessageSquareText, Plus, RefreshCw, Search, Sparkles, Trash2, Workflow } from "lucide-react";

import { canvasThemes } from "@/lib/canvas-theme";
import { createCodexSkill, createCodexSkillDraft, deleteCodexSkill, fetchCodexSkill, postState, setCodexSkillEnabled, updateCodexSkill, type AgentSkillDetail, type AgentSkillDraft, type AgentSkillInterface, type AgentSkillScope, type AgentSkillSummary } from "@/services/api/canvas-agent";
import { useAgentSkillStore } from "@/stores/use-agent-skill-store";
import { useAgentStore, type AgentChatItem } from "@/stores/use-agent-store";
import { useThemeStore } from "@/stores/use-theme-store";

type ScopeFilter = "all" | AgentSkillScope;
type SkillDraftSource = "conversation" | "canvas";
type SkillEditor = { mode: "create"; values?: SkillFormValues } | { mode: "edit"; detail: AgentSkillDetail };
type SkillFormValues = { name: string; description: string; instructions: string; displayName?: string; shortDescription?: string; defaultPrompt?: string };

const scopeLabels: Record<AgentSkillScope, string> = { repo: "项目", user: "个人", system: "系统", admin: "管理员" };
const skillNamePattern = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

export function AgentSkillsView({ clientId }: { clientId: string }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { message, modal } = App.useApp();
    const connected = useAgentStore((state) => state.connected);
    const url = useAgentStore((state) => state.url);
    const token = useAgentStore((state) => state.token);
    const activeThreadId = useAgentStore((state) => state.activeThreadId);
    const hasConversation = useAgentStore((state) => hasSettledConversation(state.messages, state.activeThreadId));
    const hasCanvas = useAgentStore((state) => Boolean(state.canvasContext));
    const sending = useAgentStore((state) => state.sending);
    const waiting = useAgentStore((state) => state.waiting);
    const setAgentState = useAgentStore((state) => state.setAgentState);
    const skills = useAgentSkillStore((state) => state.skills);
    const selectedSkill = useAgentSkillStore((state) => state.selectedSkill);
    const loading = useAgentSkillStore((state) => state.loading);
    const loaded = useAgentSkillStore((state) => state.loaded);
    const errors = useAgentSkillStore((state) => state.errors);
    const draft = useAgentSkillStore((state) => state.draft);
    const generatingSource = useAgentSkillStore((state) => state.generatingSource);
    const loadSkills = useAgentSkillStore((state) => state.loadSkills);
    const selectSkill = useAgentSkillStore((state) => state.selectSkill);
    const clearSelection = useAgentSkillStore((state) => state.clearSelection);
    const setDraft = useAgentSkillStore((state) => state.setDraft);
    const setGeneratingSource = useAgentSkillStore((state) => state.setGeneratingSource);
    const [query, setQuery] = useState("");
    const [scope, setScope] = useState<ScopeFilter>("all");
    const [editor, setEditor] = useState<SkillEditor | null>(null);
    const [saving, setSaving] = useState(false);
    const [advancedOpen, setAdvancedOpen] = useState(false);
    const [createMenuOpen, setCreateMenuOpen] = useState(false);
    const [busySkill, setBusySkill] = useState("");
    const [errorsOpen, setErrorsOpen] = useState(false);
    const confirmRef = useRef<{ destroy: () => void } | null>(null);
    const [form] = Form.useForm<SkillFormValues>();
    const endpoint = url.trim().replace(/\/$/, "");
    const filteredSkills = useMemo(() => {
        const keyword = query.trim().toLowerCase();
        return skills.filter((skill) => {
            if (scope !== "all" && skill.scope !== scope) return false;
            return !keyword || [skill.name, skill.description, skill.interface?.displayName, skill.interface?.shortDescription, skill.shortDescription].some((value) => value?.toLowerCase().includes(keyword));
        });
    }, [query, scope, skills]);

    const editorValues = editor?.mode === "edit" ? skillFormValues(editor.detail) : editor?.values;

    const refresh = (forceReload = true) => loadSkills(endpoint, token, forceReload);
    const connectionIsCurrent = (revision: number) => {
        const agent = useAgentStore.getState();
        const skillsState = useAgentSkillStore.getState();
        return skillsState.connectionRevision === revision && agent.connected && agent.url.trim().replace(/\/$/, "") === endpoint && agent.token === token;
    };
    useEffect(() => {
        if (draft) setEditor((current) => current || { mode: "create", values: draftFormValues(draft) });
    }, [draft]);
    useEffect(() => {
        if (connected) return;
        confirmRef.current?.destroy();
        confirmRef.current = null;
        setEditor(null);
        setSaving(false);
        setAdvancedOpen(false);
        setCreateMenuOpen(false);
        setBusySkill("");
        setErrorsOpen(false);
        form.resetFields();
    }, [connected, form]);
    const useSkill = (skill: AgentSkillSummary) => {
        selectSkill(skill);
        setAgentState({ activeTab: "chat" });
    };
    const generateDraft = async (source: SkillDraftSource) => {
        const agent = useAgentStore.getState();
        if (agent.sending || agent.waiting) return message.warning("Codex 正在运行，请完成当前任务后再提炼 Skill");
        if (source === "conversation" && !hasSettledConversation(agent.messages, agent.activeThreadId)) return message.warning("当前对话还没有可提炼的已完成内容");
        if (source === "canvas" && !agent.canvasContext) return message.warning("当前页面没有可提炼的画布");
        if (!clientId) return message.warning("当前页面仍在连接 Agent，请稍后再试");
        const connectionRevision = useAgentSkillStore.getState().connectionRevision;
        setGeneratingSource(source);
        try {
            if (source === "canvas") {
                const synced = await postState(endpoint, token, clientId, agent.canvasContext?.snapshot || null);
                if (!synced) throw new Error("同步当前画布失败，请检查 Agent 连接后重试");
            }
            if (!connectionIsCurrent(connectionRevision)) return;
            const response = await createCodexSkillDraft(endpoint, token, {
                source,
                threadId: agent.activeThreadId,
                clientId,
                ...(agent.model ? { model: agent.model } : {}),
                ...(agent.reasoningEffort ? { effort: agent.reasoningEffort } : {}),
            });
            if (!connectionIsCurrent(connectionRevision)) return;
            if (!response.data) throw new Error("未生成 Skill 草稿");
            setDraft(response.data);
            message.success("草稿已生成，可在技能页确认后创建");
        } catch (error) {
            if (connectionIsCurrent(connectionRevision)) message.error(error instanceof Error ? error.message : "生成 Skill 草稿失败");
        } finally {
            if (connectionIsCurrent(connectionRevision)) setGeneratingSource(null);
        }
    };
    const openEdit = async (skill: AgentSkillSummary) => {
        if (!skill.managed || busySkill || useAgentSkillStore.getState().generatingSource) return;
        const connectionRevision = useAgentSkillStore.getState().connectionRevision;
        setBusySkill(skill.path);
        try {
            const response = await fetchCodexSkill(endpoint, token, skill.name);
            if (!connectionIsCurrent(connectionRevision)) return;
            if (!response.data) throw new Error("未读取到 Skill 内容");
            setEditor({ mode: "edit", detail: response.data });
        } catch (error) {
            if (connectionIsCurrent(connectionRevision)) message.error(error instanceof Error ? error.message : "读取 Skill 失败");
        } finally {
            if (connectionIsCurrent(connectionRevision)) setBusySkill("");
        }
    };
    const saveSkill = async () => {
        if (!editor) return;
        let values: SkillFormValues;
        try {
            values = await form.validateFields();
        } catch {
            const firstError = form.getFieldsError().find((field) => field.errors.length);
            if (firstError?.name.some((name) => name === "shortDescription" || name === "defaultPrompt")) setAdvancedOpen(true);
            if (firstError) requestAnimationFrame(() => form.scrollToField(firstError.name, { block: "center" }));
            return;
        }
        const name = editor.mode === "edit" ? editor.detail.name : values.name.trim();
        const skillInterface = compactInterface(values);
        if (skillInterface?.defaultPrompt && !mentionsSkill(skillInterface.defaultPrompt, name)) {
            form.setFields([{ name: "defaultPrompt", errors: [`默认提示词需要包含 $${name}`] }]);
            setAdvancedOpen(true);
            requestAnimationFrame(() => form.scrollToField("defaultPrompt", { block: "center" }));
            return;
        }
        const connectionRevision = useAgentSkillStore.getState().connectionRevision;
        if (!connectionIsCurrent(connectionRevision)) return;
        setSaving(true);
        try {
            const input = { description: values.description.trim(), instructions: values.instructions.trim(), interface: skillInterface || null };
            if (editor.mode === "create") await createCodexSkill(endpoint, token, { name, ...input });
            else await updateCodexSkill(endpoint, token, name, { ...input, expectedRevision: editor.detail.revision });
            if (!connectionIsCurrent(connectionRevision)) return;
            setDraft(null);
            setEditor(null);
            setAdvancedOpen(false);
            await refresh();
            if (!connectionIsCurrent(connectionRevision)) return;
            message.success(editor.mode === "create" ? "Skill 已创建" : "Skill 已更新");
        } catch (error) {
            if (connectionIsCurrent(connectionRevision)) message.error(error instanceof Error ? error.message : "保存 Skill 失败");
        } finally {
            if (connectionIsCurrent(connectionRevision)) setSaving(false);
        }
    };
    const confirmDelete = (skill: AgentSkillSummary) => {
        const connectionRevision = useAgentSkillStore.getState().connectionRevision;
        confirmRef.current = modal.confirm({
            title: `删除 ${skill.interface?.displayName || skill.name}`,
            content: "删除后本地文件无法恢复，确定继续吗？",
            okText: "删除",
            okType: "danger",
            cancelText: "取消",
            onOk: async () => {
                if (!connectionIsCurrent(connectionRevision)) return;
                setBusySkill(skill.path);
                try {
                    const response = await fetchCodexSkill(endpoint, token, skill.name);
                    if (!connectionIsCurrent(connectionRevision)) return;
                    if (!response.data) throw new Error("未读取到 Skill 内容");
                    await deleteCodexSkill(endpoint, token, skill.name, response.data.revision);
                    if (!connectionIsCurrent(connectionRevision)) return;
                    if (selectedSkill?.name === skill.name && selectedSkill.path === skill.path) clearSelection();
                    await refresh();
                    if (!connectionIsCurrent(connectionRevision)) return;
                    message.success("Skill 已删除");
                } catch (error) {
                    if (!connectionIsCurrent(connectionRevision)) return;
                    message.error(error instanceof Error ? error.message : "删除 Skill 失败");
                    throw error;
                } finally {
                    if (connectionIsCurrent(connectionRevision)) setBusySkill("");
                }
            },
            afterClose: () => {
                confirmRef.current = null;
            },
        });
    };
    const toggleEnabled = async (skill: AgentSkillSummary, enabled: boolean) => {
        const connectionRevision = useAgentSkillStore.getState().connectionRevision;
        if (!connectionIsCurrent(connectionRevision)) return;
        setBusySkill(skill.path);
        try {
            await setCodexSkillEnabled(endpoint, token, skill, enabled);
            if (!connectionIsCurrent(connectionRevision)) return;
            if (!enabled && selectedSkill?.name === skill.name && selectedSkill.path === skill.path) clearSelection();
            await refresh();
        } catch (error) {
            if (connectionIsCurrent(connectionRevision)) message.error(error instanceof Error ? error.message : "更新 Skill 状态失败");
        } finally {
            if (connectionIsCurrent(connectionRevision)) setBusySkill("");
        }
    };
    const codexBusy = sending || waiting;
    const createMenu: MenuProps = {
        items: [
            {
                key: "conversation",
                icon: <MessageSquareText className="size-4" />,
                disabled: codexBusy || !hasConversation,
                label: (
                    <div className="py-0.5">
                        <div className="text-sm">从当前对话生成草稿</div>
                        <div className="mt-0.5 text-xs" style={{ color: theme.node.muted }}>{codexBusy ? "Codex 运行结束后可用" : hasConversation ? "整理当前对话中的可复用流程" : activeThreadId ? "当前对话还没有已完成内容" : "请先开始一段对话"}</div>
                    </div>
                ),
            },
            {
                key: "canvas",
                icon: <Workflow className="size-4" />,
                disabled: codexBusy || !hasCanvas,
                label: (
                    <div className="py-0.5">
                        <div className="text-sm">从当前画布生成草稿</div>
                        <div className="mt-0.5 text-xs" style={{ color: theme.node.muted }}>{codexBusy ? "Codex 运行结束后可用" : hasCanvas ? "整理当前页面的节点与生成流程" : "当前页面没有可用画布"}</div>
                    </div>
                ),
            },
            { type: "divider" as const },
            {
                key: "manual",
                icon: <FilePenLine className="size-4" />,
                label: (
                    <div className="py-0.5">
                        <div className="text-sm">空白创建</div>
                        <div className="mt-0.5 text-xs" style={{ color: theme.node.muted }}>从空白表单开始编写</div>
                    </div>
                ),
            },
        ],
        onClick: ({ key }) => {
            if (key === "manual") {
                setDraft(null);
                setEditor({ mode: "create" });
            }
            else void generateDraft(key as SkillDraftSource);
        },
    };

    return (
        <div className="flex min-h-0 flex-1 flex-col">
            <div className="shrink-0 border-b px-4 py-3" style={{ borderColor: theme.node.stroke }}>
                <div className="flex items-center justify-between gap-3">
                    <div>
                        <div className="text-sm font-semibold">本地 Skill</div>
                        <div className="mt-0.5 text-xs" style={{ color: theme.node.muted }}>安装在本机，由 Codex 直接执行</div>
                    </div>
                    <div className="flex items-center gap-1">
                        <Tooltip title="重新读取">
                            <Button type="text" shape="circle" className="!h-8 !w-8 !min-w-8" aria-label="重新读取 Skill" disabled={!connected || loading} icon={<RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />} onClick={() => void refresh()} />
                        </Tooltip>
                        <Dropdown trigger={["click"]} placement="bottomRight" open={createMenuOpen} onOpenChange={setCreateMenuOpen} disabled={!connected || !clientId || Boolean(generatingSource)} menu={createMenu}>
                            <Button type="text" className="!h-8 !px-2" aria-haspopup="menu" aria-expanded={createMenuOpen} disabled={!connected || !clientId} loading={Boolean(generatingSource)} icon={<Plus className="size-4" />}>
                                创建 Skill <ChevronDown className="size-3.5 opacity-60" />
                            </Button>
                        </Dropdown>
                    </div>
                </div>
                <div className="mt-3 flex gap-2">
                    <Input aria-label="搜索 Skill" className="min-w-0 flex-1" allowClear disabled={!connected} value={query} onChange={(event) => setQuery(event.target.value)} prefix={<Search className="size-3.5" />} placeholder="搜索 Skill" />
                    <Select<ScopeFilter>
                        size="small"
                        variant="borderless"
                        aria-label="按来源筛选 Skill"
                        className="w-28 shrink-0"
                        disabled={!connected}
                        value={scope}
                        onChange={setScope}
                        options={[{ value: "all", label: "全部来源" }, ...Object.entries(scopeLabels).map(([value, label]) => ({ value: value as AgentSkillScope, label }))]}
                    />
                </div>
                {errors.length ? (
                    <Button danger type="text" size="small" className="!mt-1 !h-7 !px-1 text-xs" icon={<CircleAlert className="size-3.5" />} onClick={() => setErrorsOpen(true)}>{errors.length} 个 Skill 未能加载</Button>
                ) : null}
            </div>
            <div className="thin-scrollbar min-h-0 flex-1 overflow-y-auto px-4">
                {loading && !loaded ? (
                    <div className="flex h-40 items-center justify-center gap-2 text-sm" style={{ color: theme.node.muted }}><LoaderCircle className="size-4 animate-spin" />正在读取 Skill</div>
                ) : filteredSkills.length ? (
                    <div className="divide-y" style={{ borderColor: theme.node.stroke }}>
                        {filteredSkills.map((skill) => {
                            const selected = selectedSkill?.name === skill.name && selectedSkill.path === skill.path;
                            const busy = busySkill === skill.path;
                            return (
                                <div key={`${skill.name}:${skill.path}`} className={`py-3 transition-opacity ${skill.enabled ? "" : "opacity-55"}`} style={{ borderColor: theme.node.stroke }}>
                                    <div className="flex items-start gap-3">
                                        <Sparkles className="mt-0.5 size-4 shrink-0" style={{ color: selected ? theme.node.text : theme.node.muted }} />
                                        <div className="min-w-0 flex-1">
                                            <div className="flex min-w-0 items-center gap-2">
                                                <span className="truncate text-sm font-medium">{skill.interface?.displayName || skill.name}</span>
                                                {!skill.managed ? <Tooltip title="外部 Skill 只能使用或启停"><LockKeyhole className="size-3.5 shrink-0" style={{ color: theme.node.faint }} /></Tooltip> : null}
                                            </div>
                                            <div className="mt-1 line-clamp-2 text-xs leading-5" style={{ color: theme.node.muted }}>{skill.interface?.shortDescription || skill.shortDescription || skill.description || "暂无说明"}</div>
                                            <Tooltip title={skill.path}>
                                                <div className="mt-1.5 truncate text-[11px]" style={{ color: theme.node.faint }}>{scopeLabels[skill.scope] || skill.scope} · {skill.name}</div>
                                            </Tooltip>
                                        </div>
                                    </div>
                                    <div className="mt-2 flex items-center justify-between gap-2 pl-7">
                                        <label className="inline-flex items-center gap-2 text-xs" style={{ color: theme.node.muted }}>
                                            <Switch size="small" checked={skill.enabled} loading={busy} disabled={!connected || Boolean(busySkill) || Boolean(generatingSource)} onChange={(enabled) => void toggleEnabled(skill, enabled)} />
                                            {skill.enabled ? "已启用" : "已停用"}
                                        </label>
                                        <div className="flex items-center gap-0.5">
                                            <Button type="text" size="small" disabled={!connected || !skill.enabled || Boolean(busySkill)} icon={selected ? <Check className="size-3.5" /> : <Sparkles className="size-3.5" />} onClick={() => useSkill(skill)}>{selected ? "已选择" : "使用"}</Button>
                                            {skill.managed ? (
                                                <>
                                                    <Tooltip title="编辑"><Button type="text" shape="circle" size="small" aria-label={`编辑 ${skill.interface?.displayName || skill.name}`} disabled={!connected || Boolean(busySkill) || Boolean(generatingSource)} icon={<FilePenLine className="size-3.5" />} onClick={() => void openEdit(skill)} /></Tooltip>
                                                    <Tooltip title="删除"><Button danger type="text" shape="circle" size="small" aria-label={`删除 ${skill.interface?.displayName || skill.name}`} disabled={!connected || Boolean(busySkill) || Boolean(generatingSource)} icon={<Trash2 className="size-3.5" />} onClick={() => confirmDelete(skill)} /></Tooltip>
                                                </>
                                            ) : null}
                                        </div>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                ) : (
                    <div className="flex h-48 flex-col items-center justify-center text-center">
                        <Sparkles className="size-5" style={{ color: theme.node.faint }} />
                        <div className="mt-3 text-sm font-medium">{!connected ? "连接 Agent 后查看 Skill" : skills.length ? "没有匹配的 Skill" : "还没有本地 Skill"}</div>
                        <div className="mt-1 text-xs" style={{ color: theme.node.muted }}>{!connected ? "连接成功后会读取本机已安装的 Skill" : skills.length ? "换个关键词或来源试试" : "创建一个，或在本机安装后刷新"}</div>
                    </div>
                )}
            </div>

            <Modal title={`${errors.length} 个 Skill 未能加载`} open={errorsOpen} footer={null} width={720} onCancel={() => setErrorsOpen(false)}>
                <div className="thin-scrollbar mt-4 max-h-[60vh] overflow-y-auto rounded-md border px-3 py-2 text-xs leading-5" style={{ borderColor: theme.node.stroke }}>
                    {errors.map((error, index) => <div key={`${index}:${error}`} className="break-all py-1" style={{ color: theme.node.muted }}>{error}</div>)}
                </div>
            </Modal>

            <Modal
                title={editor?.mode === "edit" ? `编辑 ${editor.detail.interface?.displayName || editor.detail.name}` : "创建 Skill"}
                open={Boolean(editor)}
                okText={editor?.mode === "edit" ? "保存更改" : "创建 Skill"}
                cancelText="取消"
                confirmLoading={saving}
                width={680}
                centered
                destroyOnHidden
                styles={{ body: { maxHeight: "calc(100vh - 220px)", overflowY: "auto" } }}
                onCancel={() => {
                    if (saving) return;
                    setDraft(null);
                    setEditor(null);
                    setAdvancedOpen(false);
                }}
                onOk={() => void saveSkill()}
            >
                <div className="mb-5 text-xs" style={{ color: theme.node.muted }}>保存到 本地 Agent 工作区 · <span className="font-mono">.agents/skills</span></div>
                <Form key={editor?.mode === "edit" ? editor.detail.revision : `create:${editor?.values?.name || "blank"}`} form={form} initialValues={editorValues} layout="vertical" requiredMark="optional" preserve={false}>
                    <div className="mb-3 text-xs font-medium" style={{ color: theme.node.muted }}>基本信息</div>
                    <div className="grid grid-cols-1 gap-x-4 sm:grid-cols-2">
                        <Form.Item name="name" label="Skill 标识" extra="用于文件夹名和 $skill-name 调用。" rules={[{ required: true, message: "请输入 Skill 标识" }, { max: 64, message: "Skill 标识不能超过 64 个字符" }, { pattern: skillNamePattern, message: "仅支持小写字母、数字和连字符，连字符不能连续或位于首尾" }]}>
                            <Input maxLength={64} disabled={editor?.mode === "edit"} placeholder="例如 product-grid" />
                        </Form.Item>
                        <Form.Item name="displayName" label="显示名称" rules={[{ max: 64, message: "显示名称不能超过 64 个字符" }]}><Input maxLength={64} placeholder="例如 产品九宫格生成" /></Form.Item>
                    </div>
                    <Form.Item name="description" label="何时使用" extra="说明这个 Skill 的能力和适用场景，Codex 会据此判断是否调用。" rules={[{ required: true, message: "请输入使用场景" }, { max: 1024, message: "使用场景不能超过 1024 个字符" }, { validator: (_, value) => typeof value === "string" && /[<>]/.test(value) ? Promise.reject(new Error("使用场景不能包含尖括号")) : Promise.resolve() }]}><Input.TextArea maxLength={1024} autoSize={{ minRows: 2, maxRows: 4 }} placeholder="例如：当用户需要基于商品信息规划并生成一组产品图时使用" /></Form.Item>
                    <Form.Item name="instructions" label="执行说明" extra="按实际执行顺序写清步骤、约束和输出要求。" rules={[{ required: true, message: "请输入执行说明" }]}><Input.TextArea className="!leading-6" autoSize={{ minRows: 6, maxRows: 10 }} placeholder="写清楚执行步骤、必要检查和最终输出" /></Form.Item>
                    <Collapse
                        ghost
                        size="small"
                        activeKey={advancedOpen ? ["advanced"] : []}
                        expandIconPlacement="end"
                        onChange={(keys) => setAdvancedOpen((Array.isArray(keys) ? keys : [keys]).includes("advanced"))}
                        items={[{
                            key: "advanced",
                            forceRender: true,
                            label: <span className="text-sm font-medium">高级设置</span>,
                            children: (
                                <>
                                    <Form.Item name="shortDescription" label="卡片短说明" extra="填写时控制在 25–64 个字符，便于快速浏览。" rules={[{ min: 25, message: "卡片短说明不能少于 25 个字符" }, { max: 64, message: "卡片短说明不能超过 64 个字符" }]}><Input maxLength={64} showCount placeholder="可选，用于列表展示" /></Form.Item>
                                    <Form.Item name="defaultPrompt" label="默认提示词" extra="填写时必须准确包含 $skill-name，例如 $product-grid。" rules={[{ max: 1024, message: "默认提示词不能超过 1024 个字符" }]}><Input.TextArea maxLength={1024} autoSize={{ minRows: 2, maxRows: 4 }} placeholder="可选，选择 Skill 时预填到输入框" /></Form.Item>
                                </>
                            ),
                        }]}
                    />
                </Form>
            </Modal>
        </div>
    );
}

function skillFormValues(detail: AgentSkillDetail): SkillFormValues {
    return {
        name: detail.name,
        description: detail.description,
        instructions: detail.instructions,
        displayName: detail.interface?.displayName || undefined,
        shortDescription: detail.interface?.shortDescription || undefined,
        defaultPrompt: detail.interface?.defaultPrompt || undefined,
    };
}

function draftFormValues(draft: AgentSkillDraft): SkillFormValues {
    return {
        name: draft.name,
        description: draft.description,
        instructions: draft.instructions,
        displayName: draft.displayName || undefined,
        shortDescription: draft.shortDescription || undefined,
        defaultPrompt: draft.defaultPrompt || undefined,
    };
}

function hasSettledConversation(messages: AgentChatItem[], threadId: string) {
    return Boolean(threadId && messages.some((item) => item.role === "user" && item.threadId === threadId && item.turnId));
}

function compactInterface(values: SkillFormValues): AgentSkillInterface | undefined {
    const skillInterface = {
        displayName: values.displayName?.trim() || undefined,
        shortDescription: values.shortDescription?.trim() || undefined,
        defaultPrompt: values.defaultPrompt?.trim() || undefined,
    };
    return Object.values(skillInterface).some(Boolean) ? skillInterface : undefined;
}

function mentionsSkill(prompt: string, name: string) {
    return new RegExp(`\\$${name}(?![A-Za-z0-9_-]|:[A-Za-z0-9_-])`).test(prompt);
}
