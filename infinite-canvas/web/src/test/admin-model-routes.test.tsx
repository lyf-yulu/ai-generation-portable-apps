import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ModelCallSettings } from "@/components/admin/model-call-settings";
import { callingPresetsForModel, routeMatchesCallingPreset } from "@/components/admin/model-templates";
import type { AdminCredentialPool, AdminLogicalModel, AdminModelRoute } from "@/api/admin";

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

const bananaContract = {
    operation: "image.edit" as const,
    input_ports: [
        { port_id: "prompt", media_type: "text" as const, min_items: 1, max_items: 1 },
        { port_id: "reference_images", media_type: "image" as const, min_items: 1, max_items: 10 },
    ],
    output_media_type: "image" as const,
    parameter_schema: {
        type: "object",
        "x-aicc-profile": "banana",
        properties: {
            aspect_ratio: { type: "string", enum: ["1:1", "16:9", "9:16", "4:3", "3:4"], default: "1:1", title: "画面比例" },
            image_size: { type: "string", enum: ["1K", "2K", "4K"], default: "2K", title: "图片尺寸" },
        },
        required: ["aspect_ratio", "image_size"],
        additionalProperties: false,
    },
    parameter_mappings: { aspect_ratio: "aspectRatio", image_size: "imageSize" },
};

const model = (profile: "banana" | "gpt_image2" | "seedream" | "seedance" = "banana"): AdminLogicalModel => ({
    model_id: profile === "gpt_image2" ? "gpt-image2" : profile,
    display_name: profile,
    introduction: "managed model",
    modality: profile === "seedance" ? "video" : "image",
    operation_contracts: [
        {
            ...bananaContract,
            operation: profile === "seedance" ? "video.generate" : "image.edit",
            output_media_type: profile === "seedance" ? "video" : "image",
            parameter_schema: { ...bananaContract.parameter_schema, "x-aicc-profile": profile, properties: profile === "banana" || profile === "gpt_image2" ? bananaContract.parameter_schema.properties : {} },
            parameter_mappings: profile === "banana" || profile === "gpt_image2" ? bananaContract.parameter_mappings : {},
        },
    ],
    enabled: true,
    archived_at: null,
    revision: 1,
});

const pools: AdminCredentialPool[] = [
    {
        pool_id: "chiyun-banana",
        provider_id: "chiyun-banana",
        adapter_type: "chiyun_gemini_images",
        group: "banana",
        allowed_families: ["nano-banana"],
        revision_digest: "a".repeat(64),
        key_count: 2,
        total_capacity: 2,
        capacity_status: "available",
        available_count: 2,
        busy_count: 0,
        circuit_status: "unsupported",
        circuit_open_count: null,
    },
    {
        pool_id: "t8-gemini",
        provider_id: "t8star",
        adapter_type: "chiyun_openai_images",
        group: "gemini",
        allowed_families: ["nano-banana"],
        revision_digest: "b".repeat(64),
        key_count: 1,
        total_capacity: 1,
        capacity_status: "available",
        available_count: 1,
        busy_count: 0,
        circuit_status: "unsupported",
        circuit_open_count: null,
    },
    {
        pool_id: "t8-cc",
        provider_id: "t8star",
        adapter_type: "chiyun_openai_images",
        group: "cc",
        allowed_families: ["claude"],
        revision_digest: "c".repeat(64),
        key_count: 1,
        total_capacity: 1,
        capacity_status: "available",
        available_count: 1,
        busy_count: 0,
        circuit_status: "unsupported",
        circuit_open_count: null,
    },
    {
        pool_id: "chiyun-gpt",
        provider_id: "chiyun-gpt-image2",
        adapter_type: "chiyun_openai_images",
        group: "gpt-image",
        allowed_families: ["gpt-image"],
        revision_digest: "d".repeat(64),
        key_count: 1,
        total_capacity: 1,
        capacity_status: "available",
        available_count: 1,
        busy_count: 0,
        circuit_status: "unsupported",
        circuit_open_count: null,
    },
    {
        pool_id: "ark-image",
        provider_id: "ark",
        adapter_type: "ark",
        group: "official",
        allowed_families: ["seedream"],
        revision_digest: "e".repeat(64),
        key_count: 1,
        total_capacity: 1,
        capacity_status: "available",
        available_count: 1,
        busy_count: 0,
        circuit_status: "unsupported",
        circuit_open_count: null,
    },
    {
        pool_id: "ark-video",
        provider_id: "ark",
        adapter_type: "ark",
        group: "official",
        allowed_families: ["seedance"],
        revision_digest: "f".repeat(64),
        key_count: 1,
        total_capacity: 1,
        capacity_status: "available",
        available_count: 1,
        busy_count: 0,
        circuit_status: "unsupported",
        circuit_open_count: null,
    },
];

const renderSettings = (logicalModel = model(), routes: AdminModelRoute[] = [], save = vi.fn()) => render(<ModelCallSettings model={logicalModel} routes={routes} pools={pools} onCreate={save} onUpdate={save} onLifecycle={vi.fn()} onSaved={vi.fn()} />);

it("shows only the trusted calling cards for each logical model", () => {
    const view = renderSettings();
    expect(screen.getByRole("heading", { name: "Chiyun" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "T8Star" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Ark 官方" })).toBeNull();

    view.rerender(<ModelCallSettings model={model("gpt_image2")} routes={[]} pools={pools} onCreate={vi.fn()} onUpdate={vi.fn()} onLifecycle={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Chiyun" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "T8Star" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Ark 官方" })).toBeNull();

    view.rerender(<ModelCallSettings model={model("seedream")} routes={[]} pools={pools} onCreate={vi.fn()} onUpdate={vi.fn()} onLifecycle={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Ark 官方" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Chiyun" })).toBeNull();

    view.rerender(<ModelCallSettings model={model("seedance")} routes={[]} pools={pools} onCreate={vi.fn()} onUpdate={vi.fn()} onLifecycle={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Ark 官方" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "T8Star" })).toBeNull();
});

it("keeps route identity and contracts out of editable controls", () => {
    const { container } = renderSettings();
    for (const label of ["线路 ID", "线路模板", "Provider", "模型族", "供应商模型名"]) {
        expect(screen.queryByLabelText(label)).toBeNull();
        expect(container).not.toHaveTextContent(label);
    }
    expect(container).not.toHaveTextContent(/api key|base url|fixture-secret|凭据引用/i);
});

it("offers only pools that exactly match each preset provider, adapter and family", () => {
    renderSettings();
    expect(screen.getByLabelText("Chiyun 凭据池")).toHaveTextContent("chiyun-banana");
    expect(screen.getByLabelText("Chiyun 凭据池")).not.toHaveTextContent(/t8-gemini|t8-cc/);
});

it("saves only administrator choices while compiling trusted preset identity", async () => {
    const save = vi.fn().mockResolvedValue({ route_id: "banana-chiyun", model_id: "banana", enabled: true, archived_at: null, revision: 1 });
    renderSettings(model(), [], save);
    fireEvent.change(screen.getByLabelText("Chiyun 凭据池"), { target: { value: "chiyun-banana" } });
    fireEvent.change(screen.getByLabelText("Chiyun 优先级"), { target: { value: "9" } });
    fireEvent.change(screen.getByLabelText("Chiyun 最大并发"), { target: { value: "3" } });
    fireEvent.click(screen.getByLabelText("启用 Chiyun"));
    fireEvent.click(screen.getByRole("button", { name: "保存 Chiyun 设置" }));

    expect(save).toHaveBeenCalledWith(
        expect.objectContaining({
            route_id: "banana-chiyun",
            model_id: "banana",
            provider_id: "chiyun-banana",
            provider_model_name: "banana2-ssvip",
            adapter_type: "chiyun_gemini_images",
            family: "nano-banana",
            credential_pool_ref: "chiyun-banana",
            priority: 9,
            max_concurrency: 3,
            enabled: true,
        }),
    );
    expect(save.mock.calls[0][0].operation_contracts).toEqual([bananaContract]);
});

it("reports duplicate routes for a preset instead of choosing one silently", () => {
    const routes: AdminModelRoute[] = [1, 2].map((revision) => ({
        route_id: `banana-chiyun-${revision}`,
        model_id: "banana",
        provider_id: "chiyun-banana",
        provider_model_name: "banana2-ssvip",
        adapter_type: "chiyun_gemini_images",
        credential_pool_ref: "chiyun-banana",
        family: "nano-banana",
        operation_contracts: [bananaContract],
        priority: 1,
        max_concurrency: 1,
        enabled: false,
        archived_at: null,
        revision,
    }));
    renderSettings(model(), routes);
    expect(screen.getByRole("alert")).toHaveTextContent("发现 2 条匹配的 Chiyun 线路");
    expect(screen.queryByLabelText("Chiyun 凭据池")).toBeNull();
    const preset = callingPresetsForModel(model()).find((item) => item.id === "chiyun")!;
    expect(routes.filter((route) => routeMatchesCallingPreset(route, preset))).toHaveLength(2);
});

it("fails closed when a route has preset identity but a different trusted contract", () => {
    const preset = callingPresetsForModel(model()).find((item) => item.id === "chiyun")!;
    const route = (operation_contracts: AdminModelRoute["operation_contracts"]): AdminModelRoute => ({
        route_id: "banana-chiyun",
        model_id: "banana",
        provider_id: "chiyun-banana",
        provider_model_name: "banana2-ssvip",
        adapter_type: "chiyun_gemini_images",
        credential_pool_ref: "chiyun-banana",
        family: "nano-banana",
        operation_contracts,
        priority: 1,
        max_concurrency: 1,
        enabled: false,
        archived_at: null,
        revision: 1,
    });
    const reordered = {
        ...bananaContract,
        input_ports: [...bananaContract.input_ports].reverse(),
        parameter_schema: {
            additionalProperties: false,
            required: [...bananaContract.parameter_schema.required].reverse(),
            properties: { image_size: bananaContract.parameter_schema.properties.image_size, aspect_ratio: bananaContract.parameter_schema.properties.aspect_ratio },
            "x-aicc-profile": "banana",
            type: "object",
        },
        parameter_mappings: { image_size: "imageSize", aspect_ratio: "aspectRatio" },
    };
    const wrongPorts = { ...bananaContract, input_ports: bananaContract.input_ports.slice(0, 1) };
    const wrongSchema = { ...bananaContract, parameter_schema: { ...bananaContract.parameter_schema, properties: { ...bananaContract.parameter_schema.properties, unsafe: { type: "string" } } } };
    const wrongMappings = { ...bananaContract, parameter_mappings: { aspect_ratio: "ratio", image_size: "imageSize" } };

    expect(routeMatchesCallingPreset(route([reordered]), preset)).toBe(true);
    for (const contract of [wrongPorts, wrongSchema, wrongMappings]) expect(routeMatchesCallingPreset(route([contract]), preset)).toBe(false);
});

it("does not publish a pending preset save after unmount", async () => {
    let resolveSave!: (value: AdminModelRoute) => void;
    const saved = vi.fn();
    const save = vi.fn(
        () =>
            new Promise<AdminModelRoute>((resolve) => {
                resolveSave = resolve;
            }),
    );
    const view = render(<ModelCallSettings model={model()} routes={[]} pools={pools} onCreate={save} onUpdate={save} onLifecycle={vi.fn()} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText("Chiyun 凭据池"), { target: { value: "chiyun-banana" } });
    fireEvent.click(screen.getByRole("button", { name: "保存 Chiyun 设置" }));
    expect(save).toHaveBeenCalledTimes(1);
    view.unmount();
    resolveSave({ route_id: "banana-chiyun", model_id: "banana", enabled: false, archived_at: null, revision: 1 });
    await Promise.resolve();
    expect(saved).not.toHaveBeenCalled();
});

it("cannot enable an existing disabled route after its compatible pool disappears", () => {
    const preset = callingPresetsForModel(model()).find((item) => item.id === "chiyun")!;
    const route: AdminModelRoute = {
        route_id: "banana-chiyun", model_id: "banana", provider_id: preset.providerId,
        provider_model_name: preset.providerModelName, adapter_type: preset.adapterType,
        credential_pool_ref: "removed-pool", family: preset.family,
        operation_contracts: [preset.contract], priority: 1, max_concurrency: 1,
        enabled: false, archived_at: null, revision: 1,
    };
    render(<ModelCallSettings model={model()} routes={[route]} pools={[]} onCreate={vi.fn()} onUpdate={vi.fn()} onLifecycle={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByLabelText("启用 Chiyun")).toBeDisabled();
    expect(screen.getAllByRole("alert").some((item) => item.textContent?.includes("不能启用"))).toBe(true);
});
