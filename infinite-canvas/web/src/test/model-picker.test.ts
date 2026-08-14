import { expect, it } from "vitest";
import { modelSupportsOperation, parameterControls } from "@/components/model-picker";

it("derives capabilities from the catalog even when a model name is misleading", () => {
    const model = { model_id: "banana-video", service_id: "s", display_name: "Definitely An Image Model", operations: ["video.image_to_video" as const], input_media: ["image" as const], requires_asset_kind: "portrait" as const, parameter_schema: {} };
    expect(modelSupportsOperation(model, "video.image_to_video")).toBe(true);
    expect(modelSupportsOperation(model, "image.generate")).toBe(false);
});

it("renders only the safe local parameter-schema subset", () => {
    const controls = parameterControls({ steps: { type: "integer", minimum: 1, maximum: 8, default: 4, script: "alert(1)" }, evil: { type: "object", component: "<img>" } });
    expect(controls).toEqual([{ name: "steps", type: "integer", required: false, minimum: 1, maximum: 8, default: 4 }]);
});

it("keeps bounded user-facing labels and conditional hints as inert data", () => {
    const controls = parameterControls({
        type: "object",
        properties: {
            sequence_mode: { type: "string", enum: ["disabled", "auto"], default: "disabled", title: "组图模式", description: "自动生成一组相关图片" },
            max_images: { type: "integer", minimum: 1, maximum: 15, default: 4, title: "最多生成张数", "x-ui-visible-when": { name: "sequence_mode", equals: "auto" } },
        },
        additionalProperties: false,
    });
    expect(controls).toEqual([
        { name: "sequence_mode", type: "enum", required: false, enum: ["disabled", "auto"], default: "disabled", title: "组图模式", description: "自动生成一组相关图片" },
        { name: "max_images", type: "integer", required: false, minimum: 1, maximum: 15, default: 4, title: "最多生成张数", visibleWhen: { name: "sequence_mode", equals: "auto" } },
    ]);

    expect(parameterControls({ evil: { type: "string", title: "x".repeat(129), description: "<script>alert(1)</script>" } })[0]).toEqual({ name: "evil", type: "string", required: false });
});
