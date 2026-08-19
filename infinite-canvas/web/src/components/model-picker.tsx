import type { ModelOperation, ModelSpec } from "@/api/contracts";

export type ParameterControl = {
    name: string;
    type: "number" | "integer" | "string" | "boolean" | "enum" | "preset";
    required?: boolean;
    minimum?: number;
    maximum?: number;
    default?: string | number | boolean;
    enum?: readonly (string | number)[];
    presets?: readonly string[];
    title?: string;
    description?: string;
    visibleWhen?: { name: string; equals: string | number | boolean };
};

function presentation(value: Record<string, unknown>): Pick<ParameterControl, "title" | "description" | "visibleWhen"> {
    const result: Pick<ParameterControl, "title" | "description" | "visibleWhen"> = {};
    if (typeof value.title === "string" && value.title.length > 0 && value.title.length <= 128 && !/[<>]/.test(value.title)) result.title = value.title;
    if (typeof value.description === "string" && value.description.length > 0 && value.description.length <= 256 && !/[<>]/.test(value.description)) result.description = value.description;
    const condition = value["x-ui-visible-when"];
    if (condition && typeof condition === "object" && !Array.isArray(condition)) {
        const candidate = condition as Record<string, unknown>;
        if (/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(String(candidate.name ?? "")) && (typeof candidate.equals === "string" || typeof candidate.equals === "number" && Number.isFinite(candidate.equals) || typeof candidate.equals === "boolean")) {
            result.visibleWhen = { name: String(candidate.name), equals: candidate.equals };
        }
    }
    return result;
}

/** The catalog is data, never executable UI. Unknown JSON-schema fields are ignored. */
export function parameterControls(schema: Record<string, unknown>): ParameterControl[] {
    const objectSchema = schema.type === "object" && schema.properties && typeof schema.properties === "object" && !Array.isArray(schema.properties);
    const entries = Object.entries((objectSchema ? schema.properties : schema) as Record<string, unknown>);
    const required = objectSchema && Array.isArray(schema.required) ? new Set(schema.required.filter((name): name is string => typeof name === "string")) : new Set<string>();
    return entries.flatMap(([name, raw]) => {
        if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(name) || !raw || typeof raw !== "object" || Array.isArray(raw)) return [];
        const value = raw as Record<string, unknown>;
        const type = value.type;
        if (Array.isArray(value.enum)) {
            if (!value.enum.length || !value.enum.every((item) => typeof item === "string" || typeof item === "number") || (type !== undefined && type !== "string" && type !== "number" && type !== "integer")) return [];
            const values = value.enum as (string | number)[];
            if ((type === "string" && values.some((item) => typeof item !== "string")) || ((type === "number" || type === "integer") && values.some((item) => typeof item !== "number" || (type === "integer" && !Number.isInteger(item)))) || (value.default !== undefined && !values.some((item) => Object.is(item, value.default)))) return [];
            return [{ name, type: "enum", required: required.has(name) || value.required === true, enum: values, default: value.default as string | number | undefined, ...presentation(value) }];
        }
        if (type === "string") {
            const constraint = value["x-ark-size"];
            const presets = constraint && typeof constraint === "object" && !Array.isArray(constraint) ? (constraint as Record<string, unknown>).presets : undefined;
            if (Array.isArray(presets) && presets.length > 0 && presets.length <= 16 && presets.every((item) => typeof item === "string" && item.length > 0 && item.length <= 16) && new Set(presets).size === presets.length) {
                const fallback = value.default;
                const validDefault = typeof fallback === "string" && presets.includes(fallback);
                return [{ name, type: "preset", required: required.has(name) || value.required === true, presets: presets as string[], ...(validDefault ? { default: fallback as string } : {}), ...presentation(value) }];
            }
        }
        if (type === "number" || type === "integer" || type === "string" || type === "boolean") {
            const result: ParameterControl = { name, type, required: required.has(name) || value.required === true, ...presentation(value) };
            if ((type === "number" || type === "integer") && typeof value.minimum === "number") result.minimum = value.minimum;
            if ((type === "number" || type === "integer") && typeof value.maximum === "number") result.maximum = value.maximum;
            const fallback = value.default;
            const validDefault = (type === "string" && typeof fallback === "string") || (type === "boolean" && typeof fallback === "boolean") || ((type === "number" || type === "integer") && typeof fallback === "number" && Number.isFinite(fallback) && (type !== "integer" || Number.isInteger(fallback)) && (result.minimum === undefined || fallback >= result.minimum) && (result.maximum === undefined || fallback <= result.maximum));
            if (validDefault) result.default = fallback as string | number | boolean;
            return [result];
        }
        return [];
    });
}

export function modelSupportsOperation(model: ModelSpec, operation: ModelOperation) {
    return model.operations.includes(operation);
}

export function modelsForOperation(models: readonly ModelSpec[], operation: ModelOperation, inputMedia?: "text" | "image") {
    return models.filter((model) => modelSupportsOperation(model, operation) && (!inputMedia || model.input_media.includes(inputMedia)));
}
