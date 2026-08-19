import { describe, expect, it } from "vitest";

import type { ModelSpec } from "@/api/contracts";
import { declaredModelPorts, graphPortsForModel } from "@/features/graph/model-capabilities";

describe("model capability ports", () => {
    it("synthesizes a prompt port for text models whose declaration omits ports", () => {
        const portless: ModelSpec = {
            model_id: "demo-image-v1",
            service_id: "demo-image",
            display_name: "本地演示图片",
            operations: ["image.generate"],
            input_media: ["text"],
            parameter_schema: { type: "object", properties: {}, additionalProperties: false },
        };
        expect(declaredModelPorts(portless)).toEqual([{ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 }]);
        expect(graphPortsForModel(portless)).toEqual([{ id: "prompt", accepts: "prompt", label: "提示词" }]);
    });

    it("keeps declared ports untouched when a text port already exists", () => {
        const declared: ModelSpec = {
            model_id: "seedream",
            service_id: "ark-image",
            display_name: "Seedream",
            operations: ["image.generate", "image.edit"],
            input_media: ["text", "image"],
            input_ports: [
                { port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 },
                { port_id: "reference_images", media_type: "image", min_items: 0, max_items: 10 },
            ],
            parameter_schema: { type: "object", properties: {}, additionalProperties: false },
        };
        expect(declaredModelPorts(declared)).toEqual(declared.input_ports);
    });

    it("does not synthesize a prompt port for models that never consume text", () => {
        const imageOnly: ModelSpec = {
            model_id: "frame-to-video",
            service_id: "ark-video",
            display_name: "Frame to video",
            operations: ["video.generate"],
            input_media: ["image"],
            input_ports: [{ port_id: "first_frame", media_type: "image", min_items: 1, max_items: 1 }],
            parameter_schema: { type: "object", properties: {}, additionalProperties: false },
        };
        expect(declaredModelPorts(imageOnly)).toEqual(imageOnly.input_ports);
    });
});
