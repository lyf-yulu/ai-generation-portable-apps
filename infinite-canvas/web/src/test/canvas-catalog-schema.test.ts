import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { parameterControls } from "@/components/model-picker";
import type { ModelSpec } from "@/api/contracts";

/** 翻译层 GET /api/v1/models 的产出必须能被前端参数渲染器完整接受。
 *
 *  fixture 是从真实运行的翻译层抓取的快照（nano-banana 8797 + seedance 8787
 *  的 /api/config 翻译结果）。改动 translate.py 的 schema 生成逻辑后，
 *  用下面的命令重新抓取：
 *
 *    curl -s -H "<Portal 签名头>" http://127.0.0.1:8894/api/v1/models \
 *      | python3 -m json.tool > src/test/fixtures/model-catalog.json
 *
 *  见 docs/infinite-canvas/03-契约翻译层.md。
 */
const fixture = resolve(dirname(fileURLToPath(import.meta.url)), "fixtures/model-catalog.json");
const models: ModelSpec[] = JSON.parse(readFileSync(fixture, "utf8")).models;

describe("翻译层模型目录", () => {
    it("快照非空且覆盖图片与视频两类服务", () => {
        expect(models.length).toBeGreaterThan(0);
        expect(models.some((m) => m.service_id === "nano-banana")).toBe(true);
        expect(models.some((m) => m.service_id === "seedance")).toBe(true);
    });

    it("每个模型都产出至少一个可渲染控件", () => {
        for (const model of models) {
            expect(parameterControls(model.parameter_schema).length).toBeGreaterThan(0);
        }
    });

    it("声明的参数不会被渲染器静默丢弃", () => {
        // enum 校验很严（model-picker.tsx:40-42）：default 不在 enum 内时
        // 整个控件被静默丢弃，界面上就是「参数不显示」。
        for (const model of models) {
            const declared = Object.keys(
                (model.parameter_schema as { properties?: Record<string, unknown> }).properties || {},
            );
            const rendered = parameterControls(model.parameter_schema).map((c) => c.name);
            expect({ id: model.model_id, dropped: declared.filter((d) => !rendered.includes(d)) })
                .toEqual({ id: model.model_id, dropped: [] });
        }
    });

    it("enum 控件的默认值必须落在候选项内", () => {
        for (const model of models) {
            for (const control of parameterControls(model.parameter_schema)) {
                if (control.type === "enum" && control.default !== undefined) {
                    expect(control.enum).toContain(control.default);
                }
            }
        }
    });

    it("model_id 是 <service>:<provider>:<model> 三段式且与 service_id 一致", () => {
        // 翻译层靠拆这个 id 反查目标子应用，格式错了会导致提交被拒。
        for (const model of models) {
            const parts = model.model_id.split(":");
            expect(parts.length).toBeGreaterThanOrEqual(3);
            expect(parts[0]).toBe(model.service_id);
        }
    });

    it("operations 与服务类型匹配", () => {
        for (const model of models) {
            const expected = model.service_id === "nano-banana"
                ? ["image.generate", "image.edit"]
                : ["video.generate", "video.image_to_video"];
            expect(model.operations).toEqual(expected);
        }
    });
});
