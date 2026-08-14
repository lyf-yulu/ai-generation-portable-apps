import type { AdminLogicalModel, AdminOperationContract } from "@/api/admin";

const prompt = { port_id: "prompt", media_type: "text" as const, min_items: 1, max_items: 1 };
const objectSchema = (properties: Record<string, unknown>, required: string[] = [], profile?: ModelProfileId) => ({
    type: "object",
    ...(profile ? { "x-aicc-profile": profile } : {}),
    properties,
    ...(required.length ? { required } : {}),
    additionalProperties: false,
});
const size = { type: "string", default: "2K", title: "尺寸", description: "可选择预设，也可输入宽x高", "x-ark-size": { presets: ["1K", "1.5K", "2K"], min_pixels: 921600, max_pixels: 4624220, min_ratio: 0.0625, max_ratio: 16 } };

const arkImageProperties = {
    size,
    output_format: { type: "string", enum: ["png", "jpeg"], default: "png", title: "图片格式" },
    prompt_optimization: { type: "string", enum: ["standard", "fast"], default: "standard", title: "提示词优化" },
    watermark: { type: "boolean", default: false, title: "添加水印" },
};
const arkImageMappings = { size: "size", watermark: "watermark", output_format: "output_format", prompt_optimization: "optimize_prompt_options.mode" };
const chiyunProperties = {
    size: { type: "string", enum: ["auto", "1024x1024", "1024x1536", "1536x1024"], default: "auto" },
    output_count: { type: "integer", minimum: 1, maximum: 4, default: 1 },
};
const chiyunGeminiProperties = {
    aspect_ratio: { type: "string", enum: ["1:1", "16:9", "9:16", "4:3", "3:4"], default: "1:1", title: "画面比例" },
    image_size: { type: "string", enum: ["1K", "2K", "4K"], default: "2K", title: "图片尺寸" },
};
const videoProperties = {
    ratio: { type: "string", enum: ["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"], default: "16:9", title: "画面比例" },
    resolution: { type: "string", enum: ["480p", "720p"], default: "720p", title: "分辨率" },
    duration: { type: "integer", minimum: 4, maximum: 30, default: 5, title: "时长（秒）" },
    generate_audio: { type: "boolean", default: true, title: "生成声音" },
    output_format: { type: "string", enum: ["mp4", "mov"], default: "mp4", title: "视频格式" },
    watermark: { type: "boolean", default: false, title: "添加水印" },
};
const videoMappings = Object.fromEntries(Object.keys(videoProperties).map((key) => [key, key]));

export type CapabilityId = "multi_image" | "multi_video";
export type ModelProfileId = "seedream" | "banana" | "gpt_image2" | "seedance";
export type TemplateId = ModelProfileId;
export type AdminTemplate = {
    id: ModelProfileId;
    capability: CapabilityId;
    label: string;
    routeLabel: string;
    modality: "image" | "video";
    adapter_type: "ark" | "chiyun_gemini_images" | "chiyun_openai_images";
    familyHint: string;
    contract: AdminOperationContract;
};

export type AdminCallingPreset = {
    id: string;
    label: string;
    providerId: string;
    providerModelName: string;
    adapterType: AdminTemplate["adapter_type"];
    family: string;
    contract: AdminOperationContract;
    template: AdminTemplate;
};

export const CAPABILITY_TEMPLATES = [
    { id: "multi_image" as const, label: "多参生图" },
    { id: "multi_video" as const, label: "多参生视频" },
] as const;

export const ADMIN_MODEL_TEMPLATES: readonly AdminTemplate[] = [
    {
        id: "seedream",
        capability: "multi_image",
        label: "Seedream（Ark 官方）",
        routeLabel: "Seedream · Ark 官方",
        modality: "image",
        adapter_type: "ark",
        familyHint: "seedream",
        contract: {
            operation: "image.edit",
            input_ports: [prompt, { port_id: "reference_images", media_type: "image", min_items: 1, max_items: 10 }],
            output_media_type: "image",
            parameter_schema: objectSchema(arkImageProperties),
            parameter_mappings: arkImageMappings,
        },
    },
    {
        id: "banana",
        capability: "multi_image",
        label: "Banana（Chiyun）",
        routeLabel: "Banana · Chiyun Gemini",
        modality: "image",
        adapter_type: "chiyun_gemini_images",
        familyHint: "nano-banana",
        contract: {
            operation: "image.edit",
            input_ports: [prompt, { port_id: "reference_images", media_type: "image", min_items: 1, max_items: 10 }],
            output_media_type: "image",
            parameter_schema: objectSchema(chiyunGeminiProperties, ["aspect_ratio", "image_size"], "banana"),
            parameter_mappings: { aspect_ratio: "aspectRatio", image_size: "imageSize" },
        },
    },
    {
        id: "gpt_image2",
        capability: "multi_image",
        label: "GPT-Image2（Chiyun）",
        routeLabel: "GPT-Image2 · Chiyun",
        modality: "image",
        adapter_type: "chiyun_openai_images",
        familyHint: "gpt-image",
        contract: {
            operation: "image.edit",
            input_ports: [prompt, { port_id: "reference_images", media_type: "image", min_items: 1, max_items: 10 }],
            output_media_type: "image",
            parameter_schema: objectSchema(chiyunProperties, ["size", "output_count"], "gpt_image2"),
            parameter_mappings: { size: "size", output_count: "n" },
        },
    },
    {
        id: "seedance",
        capability: "multi_video",
        label: "Seedance（Ark 官方）",
        routeLabel: "Seedance · Ark 官方",
        modality: "video",
        adapter_type: "ark",
        familyHint: "seedance",
        contract: {
            operation: "video.generate",
            input_ports: [
                prompt,
                { port_id: "reference_images", media_type: "image", min_items: 0, max_items: 30 },
                { port_id: "first_frame", media_type: "image", min_items: 0, max_items: 1 },
                { port_id: "last_frame", media_type: "image", min_items: 0, max_items: 1 },
                { port_id: "reference_audio", media_type: "audio", min_items: 0, max_items: 10 },
            ],
            output_media_type: "video",
            parameter_schema: objectSchema(videoProperties),
            parameter_mappings: videoMappings,
        },
    },
];

const profileFromContract = (contract: AdminOperationContract | undefined): ModelProfileId | null => {
    const marker = contract?.parameter_schema?.["x-aicc-profile"];
    return typeof marker === "string" && ADMIN_MODEL_TEMPLATES.some((item) => item.id === marker) ? (marker as ModelProfileId) : null;
};

export const templateForModel = (model: AdminLogicalModel | null): AdminTemplate => {
    const contract = model?.operation_contracts?.[0];
    const marked = profileFromContract(contract);
    if (marked) return ADMIN_MODEL_TEMPLATES.find((item) => item.id === marked)!;
    if (contract?.operation === "video.generate") return ADMIN_MODEL_TEMPLATES.find((item) => item.id === "seedance")!;
    if (contract?.operation === "image.generate") return ADMIN_MODEL_TEMPLATES.find((item) => item.id === "seedream")!;
    if (Object.prototype.hasOwnProperty.call((contract?.parameter_schema.properties || {}) as object, "output_count")) return ADMIN_MODEL_TEMPLATES.find((item) => item.id === "banana")!;
    return ADMIN_MODEL_TEMPLATES.find((item) => item.id === "seedream")!;
};

export const routeTemplatesForModel = (model: AdminLogicalModel) => [templateForModel(model)];

const template = (id: ModelProfileId) => ADMIN_MODEL_TEMPLATES.find((item) => item.id === id)!;
const callingPreset = (id: string, label: string, providerId: string, providerModelName: string, templateId: ModelProfileId, family: string): AdminCallingPreset => {
    const trustedTemplate = template(templateId);
    return { id, label, providerId, providerModelName, adapterType: trustedTemplate.adapter_type, family, contract: trustedTemplate.contract, template: trustedTemplate };
};

const CALLING_PRESETS: Record<ModelProfileId, readonly AdminCallingPreset[]> = {
    banana: [callingPreset("chiyun", "Chiyun", "chiyun-banana", "banana2-ssvip", "banana", "nano-banana")],
    gpt_image2: [callingPreset("chiyun", "Chiyun", "chiyun-gpt-image2", "gpt-image-2", "gpt_image2", "gpt-image")],
    seedream: [callingPreset("ark", "Ark 官方", "ark", "doubao-seedream-5-0-pro-260628", "seedream", "seedream")],
    seedance: [callingPreset("ark", "Ark 官方", "ark", "doubao-seedance-2-5-260628", "seedance", "seedance")],
};

export const callingPresetsForModel = (model: AdminLogicalModel): readonly AdminCallingPreset[] => CALLING_PRESETS[templateForModel(model).id];

export const templateForRoute = (route: { adapter_type?: string; family?: string; operation_contracts?: AdminOperationContract[] } | null) => {
    if (!route) return undefined;
    if (route.adapter_type === "ark" && route.operation_contracts?.[0]?.operation === "video.generate") return ADMIN_MODEL_TEMPLATES.find((item) => item.id === "seedance");
    if (route.adapter_type === "ark") return ADMIN_MODEL_TEMPLATES.find((item) => item.id === "seedream");
    if (route.adapter_type === "chiyun_openai_images" && route.family === "gpt-image") return ADMIN_MODEL_TEMPLATES.find((item) => item.id === "gpt_image2");
    if (route.adapter_type === "chiyun_gemini_images") return ADMIN_MODEL_TEMPLATES.find((item) => item.id === "banana");
    return undefined;
};

const canonicalJson = (value: unknown): unknown =>
    Array.isArray(value)
        ? value.map(canonicalJson).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)))
        : value && typeof value === "object"
          ? Object.fromEntries(
                Object.entries(value as Record<string, unknown>)
                    .sort(([left], [right]) => left.localeCompare(right))
                    .map(([key, item]) => [key, canonicalJson(item)]),
            )
          : value;

const sameJson = (left: unknown, right: unknown) => JSON.stringify(canonicalJson(left)) === JSON.stringify(canonicalJson(right));

export const routeMatchesCallingPreset = (route: { provider_id?: string; provider_model_name?: string; adapter_type?: string; family?: string; operation_contracts?: AdminOperationContract[] }, preset: AdminCallingPreset) =>
    route.provider_id === preset.providerId && route.provider_model_name === preset.providerModelName && route.adapter_type === preset.adapterType && route.family === preset.family && sameJson(route.operation_contracts, [preset.contract]);

export const routeContractForModel = (template: AdminTemplate, model: AdminLogicalModel): AdminOperationContract => {
    const publicContract = model.operation_contracts?.find((item) => item.operation === template.contract.operation);
    if (!publicContract) return template.contract;
    const publicPorts = new Map(publicContract.input_ports.map((port) => [port.port_id, port]));
    const input_ports = template.contract.input_ports
        .flatMap((port) => {
            const publicPort = publicPorts.get(port.port_id);
            if (!publicPort || publicPort.media_type !== port.media_type) return [];
            return [{ ...port, min_items: Math.max(port.min_items, publicPort.min_items), max_items: Math.min(port.max_items, publicPort.max_items) }];
        })
        .filter((port) => port.min_items <= port.max_items);
    const publicProperties = (publicContract.parameter_schema.properties || {}) as Record<string, unknown>;
    const trustedProperties = (template.contract.parameter_schema.properties || {}) as Record<string, unknown>;
    const properties = Object.fromEntries(Object.entries(trustedProperties).filter(([name, rule]) => sameJson(publicProperties[name], rule)));
    const required = (template.contract.parameter_schema.required as string[] | undefined)?.filter((name) => name in properties) || [];
    const trustedProfile = profileFromContract(template.contract) || undefined;
    return {
        ...template.contract,
        input_ports,
        parameter_schema: objectSchema(properties, required, trustedProfile),
        parameter_mappings: Object.fromEntries(Object.entries(template.contract.parameter_mappings).filter(([name]) => name in properties)),
    };
};
