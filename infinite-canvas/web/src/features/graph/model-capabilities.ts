import type { ModelInputPort, ModelSpec } from "@/api/contracts";
import type { GraphInputPortDescriptor } from "./contracts";

const portLabels: Record<string, string> = { prompt: "提示词", reference_images: "参考图片", first_frame: "首帧", last_frame: "尾帧", reference_video: "参考视频", reference_audio: "参考音频" };

const REQUIRED_PROMPT_PORT: Readonly<ModelInputPort> = Object.freeze({ port_id: "prompt", media_type: "text", min_items: 1, max_items: 1 });

export function declaredModelPorts(model: ModelSpec): ModelInputPort[] {
    const seen = new Set<string>();
    const ports = (model.input_ports ?? []).filter((port) => {
        const valid = /^[A-Za-z][A-Za-z0-9._:-]{0,63}$/.test(port.port_id)
            && ["text", "image", "video", "audio"].includes(port.media_type)
            && Number.isInteger(port.min_items) && Number.isInteger(port.max_items)
            && port.min_items >= 0 && port.max_items >= Math.max(1, port.min_items) && port.max_items <= 64
            && !seen.has(port.port_id);
        if (valid) seen.add(port.port_id);
        return valid;
    });
    // 任务编译始终要求恰好一条提示词连线;消费文本但未声明文本端口的模型(如本地演示模型)补一个标准提示词端口。
    if (model.input_media.includes("text") && !ports.some((port) => port.media_type === "text")) ports.unshift({ ...REQUIRED_PROMPT_PORT });
    return ports;
}

export function graphPortsForModel(model: ModelSpec): GraphInputPortDescriptor[] {
    return declaredModelPorts(model).map((port) => ({
        id: port.port_id,
        accepts: port.media_type === "text" ? "prompt" : port.media_type,
        label: portLabels[port.port_id] ?? port.port_id,
    }));
}
