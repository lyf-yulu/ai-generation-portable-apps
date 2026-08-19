import { Boxes } from "lucide-react";
import { nanoid } from "nanoid";

import { GRAPH_SCHEMA_VERSION, assertSafeGraphInputPorts, assertSafeGraphPortId, type GraphInputPortDescriptor } from "@/features/graph/contracts";
import type { NodeDefinition } from "@/features/nodes/types";
import type { CanvasNodeData, CanvasNodeMetadata, Position } from "@/types/canvas";

export const COMFY_WORKFLOW_NODE_ID = "comfy.workflow";
const DEFAULT_OUTPUT_PORT_ID = "result";
const DEFAULT_POSITION = Object.freeze({ x: 96, y: 112 });
const DEFAULT_SIZE = Object.freeze({ width: 340, height: 200 });

export type ComfyWorkflowNodeTemplate = Readonly<{
    workflowId: string;
    revision: number;
    title: string;
    inputs: readonly GraphInputPortDescriptor[];
    executionEnabled: false;
}>;

function validateTemplate(template: ComfyWorkflowNodeTemplate) {
    assertSafeGraphPortId(template.workflowId);
    if (!Number.isInteger(template.revision) || template.revision < 1) throw new TypeError("ComfyUI workflow revision must be a positive integer");
    if (typeof template.title !== "string" || template.title.length === 0 || template.title.length > 128 || /[\u0000-\u001f\u007f]/.test(template.title)) throw new TypeError("ComfyUI workflow title is invalid");
    assertSafeGraphInputPorts(template.inputs);
    if (template.executionEnabled !== false) throw new TypeError("ComfyUI execution is unavailable in this slice");
}

function graphMetadata(template: ComfyWorkflowNodeTemplate) {
    validateTemplate(template);
    return {
        schemaVersion: GRAPH_SCHEMA_VERSION,
        role: "comfy-workflow" as const,
        workflowId: template.workflowId,
        workflowRevision: template.revision,
        inputPorts: template.inputs.map((port) => ({ ...port })),
        outputPortId: DEFAULT_OUTPUT_PORT_ID,
        executionEnabled: false as const,
    };
}

const placeholderTemplate: ComfyWorkflowNodeTemplate = Object.freeze({
    workflowId: "unassigned",
    revision: 1,
    title: "ComfyUI 工作流",
    inputs: Object.freeze([]),
    executionEnabled: false,
});

export function createComfyWorkflowNode(template: ComfyWorkflowNodeTemplate, position: Position = DEFAULT_POSITION): CanvasNodeData {
    const graph = graphMetadata(template);
    return {
        id: `${COMFY_WORKFLOW_NODE_ID}-${nanoid()}`,
        type: COMFY_WORKFLOW_NODE_ID,
        title: template.title,
        position: { ...position },
        width: DEFAULT_SIZE.width,
        height: DEFAULT_SIZE.height,
        metadata: { status: "idle", graph },
    };
}

/** Creates the sole generic platform node; template selection remains a later controlled slice. */
export function createUnassignedComfyWorkflowNode(position?: Position) {
    return createComfyWorkflowNode(placeholderTemplate, position);
}

export function ComfyWorkflowNodeCard({ node }: { node: CanvasNodeData }) {
    const graph = node.metadata?.graph;
    if (graph?.role !== "comfy-workflow") return null;
    return (
        <section className="flex h-full min-h-[200px] flex-col rounded-xl border border-[#356b48] bg-[#0b1710] p-4 text-[#dceee1]" aria-label="ComfyUI 工作流节点">
            <div className="flex items-center gap-2 text-sm font-semibold"><Boxes className="size-4 text-[#58ed87]" />{node.title}</div>
            <p className="mt-3 text-xs text-[#9ab5a2]">工作流版本：{graph.workflowRevision}</p>
            <p className="mt-1 text-xs text-[#9ab5a2]">执行状态：{graph.executionEnabled ? "可执行" : "暂不可执行"}</p>
            <button type="button" disabled className="mt-auto rounded border border-[#356b48] px-3 py-2 text-left text-xs text-[#9ab5a2] disabled:cursor-not-allowed disabled:opacity-80" title="执行将在 ComfyUI 执行切片启用">
                执行将在 ComfyUI 执行切片启用
            </button>
        </section>
    );
}

export const comfyWorkflowNodeDefinition: NodeDefinition = Object.freeze({
    id: COMFY_WORKFLOW_NODE_ID,
    version: 1,
    title: "ComfyUI 工作流",
    description: "管理员派发的受控工作流模板",
    inputs: Object.freeze([]),
    outputs: Object.freeze([{ id: DEFAULT_OUTPUT_PORT_ID, provides: "result" as const }]),
    createMetadata: (): CanvasNodeMetadata => ({ status: "idle", graph: graphMetadata(placeholderTemplate) }),
    render: (node) => <ComfyWorkflowNodeCard node={node} />,
    icon: <Boxes className="size-5" />,
    defaultSize: DEFAULT_SIZE,
    showInCreateMenu: true,
    minimapColor: "#38bdf8",
});
