import { describe, expect, it } from "vitest";

import { parameterControls } from "@/components/model-picker";


describe("parameterControls", () => {
    it("turns an x-ark-size string property into a preset control", () => {
        const controls = parameterControls({
            type: "object",
            properties: {
                size: {
                    type: "string",
                    default: "2K",
                    title: "尺寸档位",
                    "x-ark-size": { presets: ["1K", "1.5K", "2K"], min_pixels: 921600, max_pixels: 4624220, min_ratio: 0.0625, max_ratio: 16 },
                },
            },
            additionalProperties: false,
        });
        expect(controls).toEqual([{ name: "size", type: "preset", required: false, presets: ["1K", "1.5K", "2K"], default: "2K", title: "尺寸档位" }]);
    });

    it("falls back to a plain string control when x-ark-size presets are malformed", () => {
        const controls = parameterControls({
            type: "object",
            properties: { size: { type: "string", "x-ark-size": { presets: [] } } },
            additionalProperties: false,
        });
        expect(controls).toEqual([{ name: "size", type: "string", required: false }]);
    });

    it("keeps enum controls for the ratio property", () => {
        const controls = parameterControls({
            type: "object",
            properties: { ratio: { type: "string", enum: ["1:1", "16:9"], default: "1:1", title: "比例" } },
            additionalProperties: false,
        });
        expect(controls[0]).toMatchObject({ name: "ratio", type: "enum", default: "1:1", title: "比例" });
    });
});
