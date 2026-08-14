import { CircleCheck, CircleX, Download, LoaderCircle, Trash2 } from "lucide-react";

import type { CanvasNodeData } from "@/types/canvas";

export function GenerationNodeCard({ node, onRetry, onDelete }: { node: CanvasNodeData; onRetry?: (token: string) => void; onDelete?: () => void }) {
    const status = node.metadata?.status || "idle";
    const result = node.metadata?.sourceJobId;
    return (
        <article data-testid={result ? `result-node-${result}` : `generation-node-${node.id}`} className="overflow-hidden rounded-xl border border-[#285038] bg-[#0a140e] text-xs text-[#dceee1] shadow-xl">
            <header className="flex items-center gap-2 border-b border-[#1c3826] px-3 py-2">
                <span className="text-[#58ed87]">{status === "success" ? <CircleCheck className="size-4" /> : status === "error" ? <CircleX className="size-4 text-[#ff8c82]" /> : <LoaderCircle className="size-4 animate-spin" />}</span>
                <strong>{node.title}</strong>
            </header>
            {status === "success" && node.type === "image" && node.metadata?.content ? (
                <div className="media-surface m-3 overflow-hidden rounded-lg">
                    <img src={node.metadata.content} alt="生成结果" className="block h-auto w-full" />
                </div>
            ) : status === "success" && node.type === "video" && node.metadata?.content ? (
                <div className="media-surface m-3 overflow-hidden rounded-lg">
                    <video aria-label="生成视频结果" className="block h-auto w-full" controls preload="metadata" src={node.metadata.content}>
                        当前浏览器无法播放该视频。
                    </video>
                </div>
            ) : (
                <div className="space-y-2 p-3">
                    <p className="whitespace-pre-wrap text-[#b8cdbd]">{status === "error" ? node.metadata?.errorDetails : node.metadata?.prompt || node.metadata?.content}</p>
                    {node.metadata?.requestId ? <p className="text-[#688371]">请求编号：{node.metadata.requestId}</p> : null}
                    {status === "error" && node.metadata?.idempotencyKey && onRetry ? (
                        <button className="rounded border border-[#356b48] px-2 py-1 text-[#65e98d]" onClick={() => onRetry(node.metadata!.idempotencyKey!)}>
                            重试
                        </button>
                    ) : null}
                </div>
            )}
            {status === "success" && node.metadata?.content ? (
                <footer className="flex gap-2 border-t border-[#1c3826] p-2">
                    <a href={node.metadata.content} download={`生成结果-${node.metadata.sourceResultIndex ?? 0}.${node.type === "video" ? "mp4" : "png"}`} className="flex items-center gap-1 rounded border border-[#356b48] px-2 py-1 text-[#65e98d]">
                        <Download className="size-3" />
                        下载
                    </a>
                    {onDelete ? (
                        <button type="button" onClick={onDelete} className="flex items-center gap-1 rounded border border-[#6b3535] px-2 py-1 text-[#ff9b92]">
                            <Trash2 className="size-3" />
                            删除
                        </button>
                    ) : null}
                </footer>
            ) : null}
        </article>
    );
}
