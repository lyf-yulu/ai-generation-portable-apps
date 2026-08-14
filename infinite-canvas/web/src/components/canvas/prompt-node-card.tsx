import { useEffect, useLayoutEffect, useRef, useState, type ChangeEvent, type PointerEvent } from "react";
import { FileText, Sparkles } from "lucide-react";

import { fetchPromptSkills, optimizePrompt } from "@/api/prompt-skills";
import type { PromptSkill } from "@/api/contracts";
import type { CanvasNodeData } from "@/types/canvas";

const MAX_PROMPT_FILE_BYTES = 1024 * 1024;

type PromptNodeCardProps = {
    node: CanvasNodeData;
    disabled?: boolean;
    onTextChange: (text: string) => void;
};

export function PromptNodeCard({ node, disabled = false, onTextChange }: PromptNodeCardProps) {
    const graph = node.metadata?.graph;
    const text = graph?.role === "prompt" ? graph.text : node.metadata?.content ?? "";
    const [error, setError] = useState<string | null>(null);
    const [skills, setSkills] = useState<PromptSkill[]>([]);
    const [skillPanelOpen, setSkillPanelOpen] = useState(false);
    const [skillsLoading, setSkillsLoading] = useState(false);
    const [selectedSkill, setSelectedSkill] = useState("");
    const [optimized, setOptimized] = useState<string | null>(null);
    const [optimizing, setOptimizing] = useState(false);
    const [skillError, setSkillError] = useState<string | null>(null);
    const mountedRef = useRef(true);
    const importSequenceRef = useRef(0);
    const nodeIdRef = useRef(node.id);
    const disabledRef = useRef(disabled);
    const optimizeSequenceRef = useRef(0);

    useLayoutEffect(() => {
        if (nodeIdRef.current === node.id) return;
        nodeIdRef.current = node.id;
        importSequenceRef.current += 1;
        optimizeSequenceRef.current += 1;
        setOptimized(null);
        setOptimizing(false);
        setSkillError(null);
    }, [node.id]);

    useLayoutEffect(() => {
        if (disabled && !disabledRef.current) {
            importSequenceRef.current += 1;
            optimizeSequenceRef.current += 1;
            setOptimized(null);
            setOptimizing(false);
        }
        disabledRef.current = disabled;
    }, [disabled]);

    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            importSequenceRef.current += 1;
            optimizeSequenceRef.current += 1;
        };
    }, []);

    const importTxt = async (event: ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        event.target.value = "";
        if (!file || disabled) return;
        const sequence = importSequenceRef.current + 1;
        importSequenceRef.current = sequence;
        const sourceNodeId = node.id;
        const isLatest = () => mountedRef.current && !disabledRef.current && importSequenceRef.current === sequence && nodeIdRef.current === sourceNodeId;
        setError(null);
        if (!file.name.toLocaleLowerCase().endsWith(".txt") || (file.type && file.type !== "text/plain")) {
            if (isLatest()) setError("请选择纯文本 TXT 文件。");
            return;
        }
        if (file.size > MAX_PROMPT_FILE_BYTES) {
            if (isLatest()) setError("TXT 文件不能超过 1 MB。");
            return;
        }
        let bytes: ArrayBuffer;
        try {
            bytes = await file.arrayBuffer();
        } catch {
            if (isLatest()) setError("无法读取这个 TXT 文件，请重新选择。");
            return;
        }
        if (!isLatest()) return;
        try {
            const imported = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
            if (isLatest()) onTextChange(imported.replace(/^\uFEFF/, ""));
        } catch {
            if (isLatest()) setError("TXT 文件必须使用 UTF-8 编码。");
        }
    };

    const stopEditingGesture = (event: PointerEvent<HTMLElement>) => event.stopPropagation();
    const selected = skills.find((skill) => skill.skill_id === selectedSkill);
    const openSkillPanel = async () => {
        if (skillPanelOpen) {
            setSkillPanelOpen(false);
            return;
        }
        setSkillPanelOpen(true);
        if (skills.length || skillsLoading) return;
        setSkillsLoading(true);
        setSkillError(null);
        const sourceNodeId = node.id;
        try {
            const rawItems = await fetchPromptSkills();
            if (!mountedRef.current || nodeIdRef.current !== sourceNodeId) return;
            const items = Array.isArray(rawItems) ? rawItems : [];
            setSkills(items);
            setSelectedSkill(items[0]?.skill_id ?? "");
            if (!items.length) setSkillError("暂时无法读取提示词优化 Skill。");
        } catch {
            if (mountedRef.current && nodeIdRef.current === sourceNodeId) setSkillError("暂时无法读取提示词优化 Skill。");
        } finally {
            if (mountedRef.current && nodeIdRef.current === sourceNodeId) setSkillsLoading(false);
        }
    };
    const runOptimization = async () => {
        if (!selected?.available || disabled || optimizing || !text.trim()) return;
        const sequence = optimizeSequenceRef.current + 1;
        optimizeSequenceRef.current = sequence;
        const sourceNodeId = node.id;
        setOptimizing(true);
        setSkillError(null);
        setOptimized(null);
        try {
            const result = await optimizePrompt(selected.skill_id, text);
            if (mountedRef.current && !disabledRef.current && nodeIdRef.current === sourceNodeId && optimizeSequenceRef.current === sequence) setOptimized(result.optimized_prompt);
        } catch {
            if (mountedRef.current && nodeIdRef.current === sourceNodeId && optimizeSequenceRef.current === sequence) setSkillError("提示词优化失败，原文没有改变。请稍后重试。");
        } finally {
            if (mountedRef.current && nodeIdRef.current === sourceNodeId && optimizeSequenceRef.current === sequence) setOptimizing(false);
        }
    };

    return (
        <article className="max-w-full overflow-hidden rounded-xl border border-[#285038] bg-[#0a140e] text-xs text-[#dceee1] shadow-xl">
            <header className="flex items-center gap-2 border-b border-[#1c3826] px-3 py-2">
                <FileText className="size-4 text-[#58ed87]" />
                <strong>{node.title}</strong>
            </header>
            <div className="space-y-3 p-3">
                <label className="block text-[11px] text-[#8fa596]" htmlFor={`prompt-node-${node.id}`}>提示词内容</label>
                <textarea
                    id={`prompt-node-${node.id}`}
                    aria-label="提示词内容"
                    disabled={disabled}
                    value={text}
                    placeholder="在这里输入提示词，也可以导入本地 TXT 文件"
                    className="min-h-24 w-full resize-y rounded-lg border border-[#285038] bg-[#050806] p-2.5 text-sm leading-6 text-[#dceee1] placeholder:text-[#58705f] disabled:cursor-not-allowed disabled:opacity-60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#58ed87]"
                    onPointerDown={stopEditingGesture}
                    onChange={(event) => onTextChange(event.target.value)}
                />
                <label className="block text-[11px] text-[#8fa596]">
                    导入 TXT
                    <input
                        aria-label="导入 TXT"
                        disabled={disabled}
                        type="file"
                        accept="text/plain,.txt"
                        className="mt-1 block w-full max-w-full text-[11px] text-[#9fb5a5] file:mr-2 file:rounded-md file:border file:border-[#356b48] file:bg-[#102319] file:px-2 file:py-1 file:text-[#65e98d] disabled:opacity-60"
                        onPointerDown={stopEditingGesture}
                        onChange={(event) => void importTxt(event)}
                    />
                </label>
                <section className="space-y-2 rounded-lg border border-[#285038] bg-[#07100a] p-2.5" data-canvas-no-drag>
                    <button type="button" aria-expanded={skillPanelOpen} disabled={disabled} onClick={() => void openSkillPanel()} className="flex w-full items-center justify-between gap-2 text-[11px] font-medium text-[#65e98d] disabled:opacity-40"><span className="flex items-center gap-1.5"><Sparkles className="size-3.5" />提示词优化</span><span aria-hidden="true">{skillPanelOpen ? "−" : "+"}</span></button>
                    {skillPanelOpen ? <div className="space-y-2">
                        <select aria-label="优化 Skill" disabled={disabled || optimizing || skillsLoading || !skills.length} value={selectedSkill} onChange={(event) => { setSelectedSkill(event.target.value); setOptimized(null); setSkillError(null); }} className="block w-full rounded-md border border-[#285038] bg-[#050806] p-2 text-[#dceee1] disabled:opacity-50">
                            {skills.length ? skills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>{skill.title}</option>) : <option value="">{skillsLoading ? "正在读取 Skill…" : "暂无可用 Skill"}</option>}
                        </select>
                        {selected ? <p className="text-[10px] leading-4 text-[#8fa596]">{selected.description}</p> : null}
                        {!selected?.available && selected ? <p className="text-[10px] text-[#d6a35d]">管理员尚未启用提示词优化服务</p> : null}
                        <button type="button" disabled={disabled || optimizing || !selected?.available || !text.trim()} onClick={() => void runOptimization()} className="w-full rounded-md border border-[#356b48] px-2 py-1.5 text-[#65e98d] disabled:opacity-40">{optimizing ? "正在优化…" : "一键优化"}</button>
                    {optimized !== null ? <div className="space-y-2">
                        <label className="block text-[10px] text-[#8fa596]">优化预览
                            <textarea aria-label="优化预览" readOnly value={optimized} className="mt-1 min-h-24 w-full resize-y rounded-md border border-[#356b48] bg-[#050806] p-2 text-xs leading-5 text-[#dceee1]" />
                        </label>
                        <div className="flex gap-2">
                            <button type="button" onClick={() => { onTextChange(optimized); setOptimized(null); }} className="flex-1 rounded-md bg-[#47d978] px-2 py-1.5 font-medium text-[#041008]">应用优化</button>
                            <button type="button" onClick={() => setOptimized(null)} className="flex-1 rounded-md border border-[#435348] px-2 py-1.5">放弃</button>
                        </div>
                    </div> : null}
                        {skillError ? <p role="status" className="text-[10px] text-[#ff9b91]">{skillError}</p> : null}
                    </div> : null}
                </section>
                {error ? <p role="alert" className="rounded-md border border-[#743c36] bg-[#2a110f] px-2 py-1.5 text-[#ff9b91]">{error}</p> : null}
            </div>
        </article>
    );
}
