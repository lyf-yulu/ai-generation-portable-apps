import type { MouseEvent, PointerEvent } from "react";

import type { GraphPortRef } from "@/features/graph/connect";
import { getNodePortAnchor } from "@/lib/canvas/canvas-node-geometry";
import type { CanvasNodeData } from "@/types/canvas";

const PORT_LABELS: Readonly<Record<string, string>> = {
    prompt: "提示词",
    reference_images: "参考图片",
    first_frame: "首帧",
    last_frame: "尾帧",
    reference_video: "参考视频",
    reference_audio: "参考音频",
    result: "结果",
    media: "媒体",
};

type NodePortProps = {
    node: CanvasNodeData;
    port: GraphPortRef;
    active: boolean;
    disabled?: boolean;
    onClick: (port: GraphPortRef, event: MouseEvent<HTMLButtonElement>) => void;
    onPointerDown: (port: GraphPortRef, event: PointerEvent<HTMLButtonElement>) => void;
    onPointerUp: (port: GraphPortRef, event: PointerEvent<HTMLButtonElement>) => void;
};

export function portDisplayName(portId: string) {
    return PORT_LABELS[portId] ?? portId;
}

export function NodePort({ node, port, active, disabled = false, onClick, onPointerDown, onPointerUp }: NodePortProps) {
    const anchor = getNodePortAnchor(node, port.portId, port.direction);
    const directionName = port.direction === "source" ? "输出" : "输入";
    const displayName = port.label ?? portDisplayName(port.portId);
    const label = `${node.title}：${displayName}${directionName}端口`;
    return (
        <button
            type="button"
            aria-label={label}
            aria-pressed={active}
            disabled={disabled}
            data-canvas-no-drag
            data-node-port={port.portId}
            data-port-direction={port.direction}
            className={`absolute z-20 size-5 rounded-full border-2 border-[#08100b] shadow-[0_0_0_1px_rgba(88,237,135,0.55)] transition-transform focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#7bff9f] ${active ? "scale-125 bg-[#7bff9f]" : "bg-[#47d978] hover:scale-110"} disabled:cursor-not-allowed disabled:bg-[#53645a] disabled:opacity-70`}
            style={{
                left: anchor.x - node.position.x,
                top: anchor.y - node.position.y,
                transform: "translate(-50%, -50%)",
            }}
            title={label}
            onClick={(event) => onClick(port, event)}
            onPointerDown={(event) => onPointerDown(port, event)}
            onPointerUp={(event) => onPointerUp(port, event)}
        >
            <span aria-hidden="true" className={`pointer-events-none absolute top-1/2 -translate-y-1/2 whitespace-nowrap rounded bg-[#08100b]/95 px-1.5 py-0.5 text-[10px] font-medium text-[#dceee1] shadow-sm ${port.direction === "source" ? "left-6" : "right-6"}`}>
                {displayName}
            </span>
        </button>
    );
}
