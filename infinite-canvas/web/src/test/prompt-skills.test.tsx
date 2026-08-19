import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as promptApi from "@/api/prompt-skills";
import { PromptNodeCard } from "@/components/canvas/prompt-node-card";
import { GRAPH_SCHEMA_VERSION } from "@/features/graph/contracts";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";

const node: CanvasNodeData = {
    id: "prompt-a", type: CanvasNodeType.Text, title: "提示词", position: { x: 0, y: 0 }, width: 320, height: 180,
    metadata: { graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "雨夜街道", outputPortId: "prompt" } },
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); promptApi.resetPromptSkillCacheForTests(); });

describe("prompt optimization skills", () => {
    it("previews one selected skill and only changes the node after confirmation", async () => {
        const fetchSkills = vi.spyOn(promptApi, "fetchPromptSkills").mockResolvedValue([{ skill_id: "cinematic-video", title: "电影镜头", description: "强化镜头和运动", source_url: "https://github.com/danielmiessler/Fabric", source_commit: "a".repeat(40), license: "MIT", available: true }]);
        vi.spyOn(promptApi, "optimizePrompt").mockResolvedValue({ skill_id: "cinematic-video", optimized_prompt: "雨夜街道，低机位推进" });
        const onTextChange = vi.fn();
        render(<PromptNodeCard node={node} onTextChange={onTextChange} />);
        expect(fetchSkills).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole("button", { name: "提示词优化" }));
        expect(await screen.findByRole("option", { name: "电影镜头" })).toBeInTheDocument();
        fireEvent.click(screen.getByRole("button", { name: "一键优化" }));
        expect(await screen.findByDisplayValue("雨夜街道，低机位推进")).toBeInTheDocument();
        expect(onTextChange).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole("button", { name: "应用优化" }));
        expect(onTextChange).toHaveBeenCalledWith("雨夜街道，低机位推进");
    });

    it("keeps the original prompt on failure and explains unavailable service", async () => {
        vi.spyOn(promptApi, "fetchPromptSkills").mockResolvedValue([{ skill_id: "photo", title: "摄影写实", description: "摄影语言", source_url: "https://github.com/dair-ai/Prompt-Engineering-Guide", source_commit: "b".repeat(40), license: "MIT", available: false }]);
        const optimize = vi.spyOn(promptApi, "optimizePrompt");
        render(<PromptNodeCard node={node} onTextChange={vi.fn()} />);
        fireEvent.click(screen.getByRole("button", { name: "提示词优化" }));
        await screen.findByRole("option", { name: "摄影写实" });
        expect(screen.getByRole("button", { name: "一键优化" })).toBeDisabled();
        expect(screen.getByText("管理员尚未启用提示词优化服务")).toBeInTheDocument();
        expect(optimize).not.toHaveBeenCalled();
    });

    it("does not apply a late result after the node changes", async () => {
        vi.spyOn(promptApi, "fetchPromptSkills").mockResolvedValue([{ skill_id: "photo", title: "摄影写实", description: "摄影语言", source_url: "https://github.com/x", source_commit: "b".repeat(40), license: "MIT", available: true }]);
        let resolve!: (value: { skill_id: string; optimized_prompt: string }) => void;
        vi.spyOn(promptApi, "optimizePrompt").mockReturnValue(new Promise((done) => { resolve = done; }));
        const onTextChange = vi.fn();
        const view = render(<PromptNodeCard node={node} onTextChange={onTextChange} />);
        fireEvent.click(screen.getByRole("button", { name: "提示词优化" }));
        await screen.findByRole("option", { name: "摄影写实" });
        fireEvent.click(screen.getByRole("button", { name: "一键优化" }));
        view.rerender(<PromptNodeCard node={{ ...node, id: "prompt-b" }} onTextChange={onTextChange} />);
        resolve({ skill_id: "photo", optimized_prompt: "旧节点结果" });
        await waitFor(() => expect(screen.queryByDisplayValue("旧节点结果")).not.toBeInTheDocument());
    });
});
