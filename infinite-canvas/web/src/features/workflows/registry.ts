import { portraitVideoWorkflow } from "./portrait-video";
import type { WorkflowDefinition } from "./types";

export type WorkflowRegistry = {
    registerWorkflow: (definition: WorkflowDefinition<never, unknown>) => void;
    getWorkflow: (id: string) => WorkflowDefinition<never, unknown> | undefined;
};

function freezeData<T>(value: T): T {
    if (Array.isArray(value)) return Object.freeze(value.map(freezeData)) as T;
    if (!value || typeof value !== "object" || Object.getPrototypeOf(value) !== Object.prototype) return value;
    return Object.freeze(Object.fromEntries(Object.entries(value).map(([key, item]) => [key, freezeData(item)]))) as T;
}

function validate(definition: WorkflowDefinition<never, unknown>) {
    if (!definition.id || !Number.isInteger(definition.version) || definition.version < 1) throw new Error("workflow definition requires a stable id and positive integer version");
}

export function createWorkflowRegistry(): WorkflowRegistry {
    const workflows = new Map<string, WorkflowDefinition<never, unknown>>();
    return {
        registerWorkflow(definition) {
            validate(definition);
            if (workflows.has(definition.id)) throw new Error(`duplicate workflow: ${definition.id}`);
            workflows.set(definition.id, freezeData(definition));
        },
        getWorkflow: (id) => workflows.get(id),
    };
}

export const workflowRegistry = createWorkflowRegistry();
workflowRegistry.registerWorkflow(portraitVideoWorkflow);
export const registerWorkflow = workflowRegistry.registerWorkflow;
export const getWorkflow = workflowRegistry.getWorkflow;
