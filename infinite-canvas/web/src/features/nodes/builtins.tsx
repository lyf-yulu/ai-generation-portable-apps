import { FileText, Group, Image as ImageIcon, Music2, Settings2, Video } from "lucide-react";

import { graphInputPortDescriptor } from "@/features/graph/contracts";
import { CanvasNodeType, type CanvasNodeData } from "@/types/canvas";
import type { NodeDefinition } from "./types";

const iconClass = "size-5";
const BuiltinNodeRenderer = () => null;

function builtinResource(node: CanvasNodeData): ReturnType<NonNullable<NodeDefinition["resource"]>> {
    if (node.type === CanvasNodeType.Image && node.metadata?.content) return { kind: "image", url: node.metadata.content };
    if (node.type === CanvasNodeType.Video && node.metadata?.content) return { kind: "video", url: node.metadata.content };
    if (node.type === CanvasNodeType.Audio && node.metadata?.content) return { kind: "audio", url: node.metadata.content };
    if (node.type === CanvasNodeType.Text && (node.metadata?.content || node.metadata?.prompt)) return { kind: "text", text: node.metadata.content || node.metadata.prompt };
    return null;
}

const definitions: NodeDefinition[] = [
    { id: CanvasNodeType.Text, version: 1, title: "文本", connectionTitle: "文本生成", description: "脚本、广告词、品牌文案", inputs: Object.freeze([]), outputs: Object.freeze([{ id: "prompt", provides: "prompt" as const }]), createMetadata: () => ({ content: "", status: "idle", fontSize: 14 }), render: BuiltinNodeRenderer, icon: <FileText className={iconClass} />, defaultSize: Object.freeze({ width: 340, height: 240 }), resource: builtinResource },
    { id: CanvasNodeType.Image, version: 1, title: "图片", connectionTitle: "图片生成", inputs: Object.freeze([]), outputs: Object.freeze([{ id: "media", provides: "image" as const }]), createMetadata: () => ({ content: "", status: "idle" }), render: BuiltinNodeRenderer, icon: <ImageIcon className={iconClass} />, defaultSize: Object.freeze({ width: 340, height: 240 }), minimapColor: "#10b981", keepAspectRatio: (node) => !node.metadata?.freeResize, resource: builtinResource },
    { id: CanvasNodeType.Video, version: 1, title: "视频", connectionTitle: "视频生成", inputs: Object.freeze([]), outputs: Object.freeze([{ id: "media", provides: "video" as const }]), createMetadata: () => ({ content: "", status: "idle" }), render: BuiltinNodeRenderer, icon: <Video className={iconClass} />, defaultSize: Object.freeze({ width: 420, height: 236 }), minimapColor: "#f97316", keepAspectRatio: () => true, resource: builtinResource },
    { id: CanvasNodeType.Audio, version: 1, title: "音频", connectionTitle: "音频参考", inputs: Object.freeze([]), outputs: Object.freeze([{ id: "media", provides: "audio" as const }]), createMetadata: () => ({ content: "", status: "idle" }), render: BuiltinNodeRenderer, icon: <Music2 className={iconClass} />, defaultSize: Object.freeze({ width: 340, height: 120 }), minimapColor: "#a855f7", resource: builtinResource },
    { id: CanvasNodeType.Config, version: 1, title: "生成配置", connectionTitle: "配置节点", description: "模型、尺寸、数量和输入顺序", inputs: Object.freeze(["prompt", "reference_images", "first_frame", "last_frame", "reference_video", "reference_audio"].map(graphInputPortDescriptor)), outputs: Object.freeze([{ id: "result", provides: "any" as const }]), createMetadata: () => ({ content: "", status: "idle", generationMode: "image" }), render: BuiltinNodeRenderer, icon: <Settings2 className={iconClass} />, defaultSize: Object.freeze({ width: 340, height: 240 }), minimapColor: "#60a5fa", hasSourceHandle: false, resource: builtinResource },
    { id: CanvasNodeType.Group, version: 1, title: "组", connectionTitle: "组", inputs: Object.freeze([]), outputs: Object.freeze([]), createMetadata: () => ({ status: "idle" }), render: BuiltinNodeRenderer, icon: <Group className={iconClass} />, defaultSize: Object.freeze({ width: 760, height: 480 }), minimapColor: "#94a3b8", resource: builtinResource },
];

/** Static local built-in data. Registration is performed only by the registry singleton. */
export const builtinNodeDefinitions = Object.freeze(definitions.map((definition) => Object.freeze(definition)));
