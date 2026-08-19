import { builtinNodeDefinitions } from "./builtins";
import type { NodeDefinition } from "./types";

export type NodeRegistry = {
    registerNode: (definition: NodeDefinition) => void;
    unregisterNode: (id: string) => boolean;
    listNodes: () => readonly NodeDefinition[];
    getNode: (id: string) => NodeDefinition | undefined;
    getSnapshot: () => number;
    subscribe: (listener: () => void) => () => void;
};

function validate(definition: NodeDefinition) {
    if (!definition.id || !Number.isInteger(definition.version) || definition.version < 1) throw new Error("node definition requires a stable id and positive integer version");
    validatePorts(definition.inputs, "accepts");
    validatePorts(definition.outputs, "provides");
}

const MAX_PORTS = 32;
const SAFE_PORT_ID = /^[A-Za-z][A-Za-z0-9._:-]{0,63}$/;
const PORT_VALUE_TYPES = new Set(["prompt", "image", "video", "audio", "result", "any"]);

function validatePorts(ports: NodeDefinition["inputs"] | NodeDefinition["outputs"], valueKey: "accepts" | "provides") {
    if (!Array.isArray(ports) || ports.length > MAX_PORTS) throw new Error("invalid node port declaration");
    const ids = new Set<string>();
    for (const declaration of ports) {
        const id = typeof declaration === "string" ? declaration : declaration?.id;
        const valueType = typeof declaration === "string" ? "any" : declaration?.[valueKey as keyof typeof declaration];
        const label = typeof declaration === "string" ? undefined : declaration?.label;
        const invalidLabel = label !== undefined && (typeof label !== "string" || label.length === 0 || label.length > 64 || /[\u0000-\u001f\u007f]/.test(label));
        if (typeof id !== "string" || !SAFE_PORT_ID.test(id) || ids.has(id) || !PORT_VALUE_TYPES.has(valueType as string) || invalidLabel) {
            throw new Error("invalid node port declaration");
        }
        ids.add(id);
    }
}

function freezeData<T>(value: T): T {
    if (Array.isArray(value)) return Object.freeze(value.map(freezeData)) as T;
    if (!value || typeof value !== "object" || "$$typeof" in value || Object.getPrototypeOf(value) !== Object.prototype) return value;
    return Object.freeze(Object.fromEntries(Object.entries(value).map(([key, item]) => [key, freezeData(item)]))) as T;
}

export function createNodeRegistry(): NodeRegistry {
    const nodes = new Map<string, NodeDefinition>();
    const listeners = new Set<() => void>();
    let revision = 0;
    const publish = () => {
        revision += 1;
        listeners.forEach((listener) => listener());
    };
    return {
        registerNode(definition) {
            validate(definition);
            if (nodes.has(definition.id)) throw new Error(`duplicate node: ${definition.id}`);
            nodes.set(definition.id, freezeData(definition));
            publish();
        },
        unregisterNode(id) {
            if (!nodes.delete(id)) return false;
            publish();
            return true;
        },
        listNodes: () => Object.freeze([...nodes.values()]),
        getNode: (id) => nodes.get(id),
        getSnapshot: () => revision,
        subscribe(listener) {
            listeners.add(listener);
            return () => listeners.delete(listener);
        },
    };
}

export const nodeRegistry = createNodeRegistry();
builtinNodeDefinitions.forEach(nodeRegistry.registerNode);
export const registerNode = nodeRegistry.registerNode;
export const listNodes = nodeRegistry.listNodes;
export const getNode = nodeRegistry.getNode;
export const subscribeToNodeRegistry = nodeRegistry.subscribe;
