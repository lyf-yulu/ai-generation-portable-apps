import { create } from "zustand";

import { getNode, listNodes, registerNode, subscribeToNodeRegistry } from "@/features/nodes/registry";
import type { CanvasNodeDefinition } from "@/features/nodes/types";
import { CanvasNodeType } from "@/types/canvas";

// 注册表版本号,注册/卸载时自增,驱动创建菜单等 UI 重渲染
export const useNodeRegistryVersion = create<{ version: number }>(() => ({ version: 0 }));
function bump() {
    useNodeRegistryVersion.setState((state) => ({ version: state.version + 1 }));
}
subscribeToNodeRegistry(bump);

export function registerNodeDefinitions(defs: CanvasNodeDefinition[]) {
    defs.forEach((def) => {
        registerNode(def);
    });
}

export function getNodeDefinition(type: string) {
    return getNode(type);
}

export function listNodeDefinitions() {
    return listNodes();
}

export function isRegisteredNodeType(type: string) {
    return Boolean(getNode(type));
}

const FALLBACK_SPEC = { width: 340, height: 240, title: "节点", metadata: {} };

// 提供默认尺寸/标题/初始 metadata,createCanvasNode 与 agent-ops 复用
export function getNodeSpec(type: string) {
    const def = getNode(type);
    if (!def) return FALLBACK_SPEC;
    return { width: def.defaultSize?.width ?? FALLBACK_SPEC.width, height: def.defaultSize?.height ?? FALLBACK_SPEC.height, title: def.title, metadata: def.createMetadata() };
}

export function isBuiltinNodeType(type: string) {
    return (Object.values(CanvasNodeType) as string[]).includes(type);
}
