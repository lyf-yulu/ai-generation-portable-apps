import type { PreviewGraph } from "@/api/comfy-workflows";

type Props = { preview: PreviewGraph };
const MAX_NODES = 500;
const MAX_EDGES = 2_000;
const clamp = (value: number) => Math.max(0, Math.min(1_600, Number.isFinite(value) ? value : 0));
const stableNodes = (nodes: PreviewGraph["nodes"]) => [...nodes].sort((a, b) => a.id.localeCompare(b.id)).slice(0, MAX_NODES);

export function WorkflowPreview({ preview }: Props) {
    const nodes = stableNodes(preview.nodes);
    if (!preview.has_editor_layout)
        return (
            <section aria-label="工作流预览" className="rounded-lg border border-[#245a35] bg-[#061009] p-4">
                <p className="text-sm text-[#a9c6b0]">API 格式不包含编辑器布局，以下为稳定排序的只读摘要。</p>
                <p className="mt-2 text-sm text-[#d8eadd]">
                    节点：{preview.nodes.length} · 连线：{preview.edges.length}
                </p>
                <table aria-label="工作流节点摘要" className="mt-3 w-full text-left text-sm">
                    <thead>
                        <tr className="text-[#86a991]">
                            <th className="py-1">节点 ID</th>
                            <th className="py-1">节点类型</th>
                        </tr>
                    </thead>
                    <tbody>
                        {nodes.map((node) => (
                            <tr key={node.id} className="border-t border-[#193523]">
                                <td className="py-1.5 font-mono text-xs text-[#b9d0c0]">{node.id}</td>
                                <td className="py-1.5">{node.type}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
                {preview.nodes.length > MAX_NODES && <p className="mt-2 text-xs text-[#86a991]">为保证安全，摘要仅显示前 {MAX_NODES} 个节点。</p>}
            </section>
        );

    const positions = new Map(nodes.map((node, index) => [node.id, node.position ? { x: clamp(node.position[0]), y: clamp(node.position[1]) } : { x: (index % 8) * 180, y: Math.floor(index / 8) * 100 }]));
    const edges = preview.edges.filter((edge) => positions.has(edge.source_id) && positions.has(edge.target_id)).slice(0, MAX_EDGES);
    return (
        <section aria-label="工作流预览" className="rounded-lg border border-[#245a35] bg-[#061009] p-4">
            <p className="mb-2 text-sm text-[#a9c6b0]">只读通用图：仅显示节点类型与连线，不显示节点参数、JSON 或服务配置。</p>
            <svg viewBox="0 0 1800 1700" role="img" aria-label={`工作流图，${nodes.length} 个节点，${edges.length} 条连线`} className="max-h-[32rem] w-full rounded border border-[#193523] bg-[#020603]">
                {edges.map((edge, index) => {
                    const source = positions.get(edge.source_id)!;
                    const target = positions.get(edge.target_id)!;
                    return <line key={`${edge.source_id}-${edge.target_id}-${index}`} x1={source.x + 70} y1={source.y + 28} x2={target.x + 70} y2={target.y + 28} stroke="#3a7650" strokeWidth="2" />;
                })}
                {nodes.map((node) => {
                    const position = positions.get(node.id)!;
                    return (
                        <g key={node.id} transform={`translate(${position.x} ${position.y})`}>
                            <rect width="140" height="56" rx="6" fill="#0b1710" stroke="#58ed87" />
                            <text x="10" y="33" fill="#e5f5e9" fontSize="14">
                                {node.type}
                            </text>
                        </g>
                    );
                })}
            </svg>
            {(preview.nodes.length > MAX_NODES || preview.edges.length > MAX_EDGES) && (
                <p className="mt-2 text-xs text-[#86a991]">
                    预览已限制为 {MAX_NODES} 个节点和 {MAX_EDGES} 条连线。
                </p>
            )}
        </section>
    );
}
