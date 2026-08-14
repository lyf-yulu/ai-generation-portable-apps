import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { ArrowLeft, Film, ImagePlus, MessageSquareText, Music2 } from "lucide-react";
import { nanoid } from "nanoid";

import type { JobState, ModelOperation, ModelSpec } from "@/api/contracts";
import { fetchModels } from "@/api/models";
import { DraggableCanvasNode } from "@/components/canvas/draggable-canvas-node";
import { ActiveConnectionPath, ConnectionPath } from "@/components/canvas/canvas-connections";
import { CanvasNodeContextMenu } from "@/components/canvas/canvas-context-menu";
import { CanvasCreateContextMenu, type CanvasCreationKind } from "@/components/canvas/canvas-create-context-menu";
import { RenameNodeDialog } from "@/components/canvas/rename-node-dialog";
import { GenerationNodeCard } from "@/components/canvas/generation-node-card";
import { InfiniteCanvas } from "@/components/canvas/infinite-canvas";
import { MediaCollectionNode, type MediaItemsUpdater } from "@/components/canvas/media-collection-node";
import { ModelCallNode } from "@/components/canvas/model-call-node";
import { NodePort } from "@/components/canvas/node-port";
import { PromptNodeCard } from "@/components/canvas/prompt-node-card";
import { CanvasNavigationControls } from "@/components/canvas/canvas-navigation-controls";
import { normalizeViewport } from "@/features/canvas/viewport";
import { GRAPH_SCHEMA_VERSION, type GraphMediaType, type GraphModelMetadata, type GraphParameterValue } from "@/features/graph/contracts";
import { compileGraphJob, CompileJobError } from "@/features/graph/compile-job";
import { graphPortsForModel } from "@/features/graph/model-capabilities";
import { parameterControls } from "@/components/model-picker";
import { connectGraphPorts, getNodePorts, graphConnectionInactiveMessage, graphConnectionRejectionMessage, graphConnectionTransientKey, resolveActiveConnections, type GraphPortRef } from "@/features/graph/connect";
import { nodeRegistry } from "@/features/nodes/registry";
import { deleteGraphNodes, isEditableEventTarget, selectNode } from "@/features/graph/selection";
import { copyCanvasSelection, pasteCanvasSelection } from "@/features/graph/canvas-clipboard";
import { appendJobResults } from "@/features/generation/result-node";
import { generationErrorMessage } from "@/features/generation/error-message";
import { useGenerationJob, type PendingRef } from "@/features/generation/use-generation-job";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";
import { CanvasNodeType } from "@/types/canvas";
import type { CanvasNodeData, ContextMenuState, Position, ViewportTransform } from "@/types/canvas";

export default function CanvasProjectPage() {
    const { id = "" } = useParams();
    const containerRef = useRef<HTMLDivElement>(null);
    const contextTriggerRef = useRef<HTMLElement | SVGElement | null>(null);
    const renameTriggerRef = useRef<HTMLElement | null>(null);
    const [models, setModels] = useState<ModelSpec[]>([]);
    const [modelMessages, setModelMessages] = useState<Record<string, string>>({});
    const [selectedNodeIds, setSelectedNodeIds] = useState<Set<string>>(() => new Set());
    const [selectedConnectionKey, setSelectedConnectionKey] = useState<string | null>(null);
    const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
    const [pendingPort, setPendingPortState] = useState<GraphPortRef | null>(null);
    const [connectionMessage, setConnectionMessage] = useState<string | null>(null);
    const [canvasCommandMessage, setCanvasCommandMessage] = useState<string | null>(null);
    const [renamingNodeId, setRenamingNodeId] = useState<string | null>(null);
    const [connectionPointerWorld, setConnectionPointerWorld] = useState<Position>({ x: 0, y: 0 });
    const [measuredNodeSizes, setMeasuredNodeSizes] = useState<Map<string, { width: number; height: number }>>(() => new Map());
    const pendingPortRef = useRef<GraphPortRef | null>(null);
    const pointerConnectionRef = useRef<number | null>(null);
    const suppressPortClickRef = useRef<string | null>(null);
    const project = useCanvasStore((state) => state.openProject(id));
    const registryRevision = useSyncExternalStore(nodeRegistry.subscribe, nodeRegistry.getSnapshot, nodeRegistry.getSnapshot);
    const projectsLoaded = useCanvasStore((state) => state.projectsLoaded);
    const syncNotice = useCanvasStore((state) => state.syncNotice);
    const loadError = useCanvasStore((state) => state.loadError);
    const readOnly = Boolean(loadError?.readOnly);
    const imageCreateOperation = useMemo<ModelOperation | null>(() => {
        if (models.some((model) => model.operations.includes("image.generate"))) return "image.generate";
        if (models.some((model) => model.operations.includes("image.edit"))) return "image.edit";
        return null;
    }, [models]);
    const videoCreateOperation = useMemo<ModelOperation | null>(() => {
        if (models.some((model) => model.operations.includes("video.generate"))) return "video.generate";
        if (models.some((model) => model.operations.includes("video.image_to_video"))) return "video.image_to_video";
        return null;
    }, [models]);
    const updateProject = useCanvasStore((state) => state.updateProject);
    const viewport = normalizeViewport(project?.viewport);
    const measuredNodeMap = useMemo(
        () =>
            new Map(
                (project?.nodes ?? []).map((node) => {
                    const measured = measuredNodeSizes.get(node.id);
                    return [node.id, measured ? { ...node, width: measured.width, height: measured.height } : node] as const;
                }),
            ),
        [measuredNodeSizes, project?.nodes],
    );
    const resolvedConnections = useMemo(() => resolveActiveConnections(project?.connections ?? [], project?.nodes ?? [], nodeRegistry), [project?.connections, project?.nodes, registryRevision]);
    const inactiveConnectionCount = resolvedConnections.filter((state) => !state.active).length;
    const recordMeasuredNodeSize = useCallback((nodeId: string, size: { width: number; height: number }) => {
        setMeasuredNodeSizes((current) => {
            const previous = current.get(nodeId);
            if (previous?.width === size.width && previous.height === size.height) return current;
            const next = new Map(current);
            next.set(nodeId, size);
            return next;
        });
    }, []);
    const setPendingPort = useCallback((port: GraphPortRef | null) => {
        pendingPortRef.current = port;
        setPendingPortState(port);
    }, []);
    const clearPendingConnection = useCallback(() => {
        pointerConnectionRef.current = null;
        suppressPortClickRef.current = null;
        setPendingPort(null);
        setConnectionMessage(null);
    }, [setPendingPort]);
    const clientToWorld = useCallback(
        (clientX: number, clientY: number) => {
            const rect = containerRef.current?.getBoundingClientRect();
            const left = rect?.left ?? 0;
            const top = rect?.top ?? 0;
            return {
                x: (clientX - left - viewport.x) / viewport.k,
                y: (clientY - top - viewport.y) / viewport.k,
            };
        },
        [viewport.k, viewport.x, viewport.y],
    );
    const changeViewport = useCallback(
        (next: ViewportTransform) => {
            updateProject(id, { viewport: normalizeViewport(next) });
        },
        [id, updateProject],
    );
    const moveNode = useCallback(
        (nodeId: string, position: Position) => {
            if (readOnly) return;
            const current = useCanvasStore.getState().openProject(id);
            if (!current) return;
            const nodes = current.nodes.map((node) => (node.id === nodeId ? { ...node, position } : node));
            updateProject(id, { nodes });
        },
        [id, readOnly, updateProject],
    );

    useEffect(() => {
        setSelectedNodeIds(new Set());
        setSelectedConnectionKey(null);
        setContextMenu(null);
        contextTriggerRef.current = null;
        setRenamingNodeId(null);
        renameTriggerRef.current = null;
        setMeasuredNodeSizes(new Map());
        clearPendingConnection();
    }, [clearPendingConnection, id]);

    useEffect(() => {
        if (!readOnly) return;
        setSelectedNodeIds(new Set());
        setSelectedConnectionKey(null);
        setContextMenu(null);
        contextTriggerRef.current = null;
        setRenamingNodeId(null);
        renameTriggerRef.current = null;
        clearPendingConnection();
    }, [clearPendingConnection, readOnly]);

    useEffect(() => {
        const existing = new Set(project?.nodes.map((node) => node.id) ?? []);
        setMeasuredNodeSizes((current) => {
            if ([...current.keys()].every((nodeId) => existing.has(nodeId))) return current;
            return new Map([...current].filter(([nodeId]) => existing.has(nodeId)));
        });
        setSelectedNodeIds((current) => {
            if ([...current].every((nodeId) => existing.has(nodeId))) return current;
            return new Set([...current].filter((nodeId) => existing.has(nodeId)));
        });
    }, [project?.nodes]);

    useEffect(() => {
        if (!selectedConnectionKey || resolvedConnections.some((state) => state.connectionKey === selectedConnectionKey)) return;
        setSelectedConnectionKey(null);
    }, [resolvedConnections, selectedConnectionKey]);

    useEffect(() => {
        if (!pendingPort || pendingPort.direction !== "source") return;
        const sourceNode = project?.nodes.find((node) => node.id === pendingPort.nodeId);
        const sourceStillDeclared = sourceNode && getNodePorts(sourceNode).sources.some((port) => port.portId === pendingPort.portId);
        if (sourceStillDeclared) return;
        pointerConnectionRef.current = null;
        suppressPortClickRef.current = null;
        setPendingPort(null);
        setConnectionMessage("连接起点已失效。");
    }, [pendingPort, project?.nodes, registryRevision, setPendingPort]);

    useEffect(() => {
        const handlePointerMove = (event: PointerEvent) => {
            if (pointerConnectionRef.current !== event.pointerId) return;
            setConnectionPointerWorld(clientToWorld(event.clientX, event.clientY));
        };
        const finishOutsidePort = (event: PointerEvent) => {
            if (pointerConnectionRef.current !== event.pointerId) return;
            clearPendingConnection();
        };
        const cancel = () => clearPendingConnection();
        window.addEventListener("pointermove", handlePointerMove);
        window.addEventListener("pointerup", finishOutsidePort);
        window.addEventListener("pointercancel", finishOutsidePort);
        window.addEventListener("blur", cancel);
        return () => {
            window.removeEventListener("pointermove", handlePointerMove);
            window.removeEventListener("pointerup", finishOutsidePort);
            window.removeEventListener("pointercancel", finishOutsidePort);
            window.removeEventListener("blur", cancel);
        };
    }, [clearPendingConnection, clientToWorld]);

    useEffect(() => {
        void fetchModels()
            .then(setModels)
            .catch(() => setModels([]));
    }, []);

    const onSucceeded = useCallback(
        (job: JobState, ref?: PendingRef) => {
            const targetProjectId = ref?.projectId;
            if (!targetProjectId) return;
            const current = useCanvasStore.getState().openProject(targetProjectId);
            if (!current) return;
            const source = current.nodes.find((node) => node.id === ref?.sourceNodeId);
            const completedSource = source ? { ...source, metadata: { ...source.metadata, status: "success" as const, jobStatus: "succeeded" as const, idempotencyKey: undefined } } : undefined;
            const nodes = current.nodes.map((node) => (node.id === completedSource?.id ? completedSource : node));
            updateProject(targetProjectId, appendJobResults(nodes, current.connections, job, completedSource, nanoid));
        },
        [updateProject],
    );

    const onFailed = useCallback(
        ({
            request,
            projectId,
            sourceNodeId,
            message,
            requestId,
            phase,
            retryToken,
        }: {
            request: { operation: "image.generate" | "image.edit" | "video.generate" | "video.image_to_video"; model_id: string; prompt: string; params: Record<string, unknown>; asset_ids: string[]; idempotency_key: string };
            projectId?: string;
            sourceNodeId?: string;
            message: string;
            requestId?: string;
            phase?: string;
            retryToken?: string;
        }) => {
            if (!projectId) return;
            const current = useCanvasStore.getState().openProject(projectId);
            if (!current) return;
            const source = current.nodes.find((node) => node.id === sourceNodeId);
            const existing = current.nodes.map((node) => (node.id === source?.id ? { ...node, metadata: { ...node.metadata, status: "error" as const, jobStatus: "failed" as const, idempotencyKey: retryToken } } : node));
            const failed: CanvasNodeData = {
                id: nanoid(),
                type: CanvasNodeType.Image,
                title: "生成失败",
                position: source ? { x: source.position.x + 48, y: source.position.y + 48 } : { x: 80, y: 80 },
                width: 340,
                height: 190,
                metadata: { status: "error", errorDetails: message, prompt: request.prompt, model: request.model_id, params: request.params, assetIds: request.asset_ids, requestId, phase, idempotencyKey: retryToken },
            };
            updateProject(projectId, { nodes: [...existing, failed] });
        },
        [updateProject],
    );

    const onGenerationStateChanged = useCallback(
        (job: JobState, ref?: PendingRef) => {
            if (!ref?.projectId || !ref.sourceNodeId || !["queued", "running"].includes(job.status)) return;
            const current = useCanvasStore.getState().openProject(ref.projectId);
            if (!current) return;
            updateProject(ref.projectId, { nodes: current.nodes.map((node) => node.id === ref.sourceNodeId ? { ...node, metadata: { ...node.metadata, status: "loading" as const, jobId: job.id, jobStatus: job.status } } : node) });
        },
        [updateProject],
    );
    const onCancelled = useCallback(
        ({ projectId, sourceNodeId }: { jobId: string; projectId?: string; sourceNodeId?: string }) => {
            if (!projectId || !sourceNodeId) return;
            const current = useCanvasStore.getState().openProject(projectId);
            if (!current) return;
            updateProject(projectId, { nodes: current.nodes.map((node) => node.id === sourceNodeId ? { ...node, metadata: { ...node.metadata, status: "idle" as const, jobStatus: "failed" as const, idempotencyKey: undefined } } : node) });
            setModelMessages((messages) => ({ ...messages, [sourceNodeId]: "任务已取消，输入和参数已保留。" }));
        },
        [updateProject],
    );

    const generation = useGenerationJob({ onSucceeded, onFailed, onStateChanged: onGenerationStateChanged, onCancelled });
    const deleteNodes = useCallback(
        (nodeIds: ReadonlySet<string>) => {
            if (readOnly || nodeIds.size === 0) return;
            const current = useCanvasStore.getState().openProject(id);
            if (!current) return;
            updateProject(id, deleteGraphNodes(current.nodes, current.connections, nodeIds));
            setSelectedNodeIds((selected) => new Set([...selected].filter((nodeId) => !nodeIds.has(nodeId))));
            setContextMenu(null);
        },
        [id, readOnly, updateProject],
    );
    const deleteConnection = useCallback(
        (connectionKey: string) => {
            if (readOnly) return;
            const current = useCanvasStore.getState().openProject(id);
            if (!current) return;
            const connectionIndex = current.connections.findIndex((connection, index) => graphConnectionTransientKey(connection, index) === connectionKey);
            if (connectionIndex < 0) return;
            updateProject(id, { connections: current.connections.filter((_connection, index) => index !== connectionIndex) });
            setSelectedConnectionKey((selected) => (selected === connectionKey ? null : selected));
            setContextMenu(null);
        },
        [id, readOnly, updateProject],
    );

    const copySelection = useCallback(() => {
        if (readOnly || selectedNodeIds.size === 0) return false;
        const current = useCanvasStore.getState().openProject(id);
        if (!current) return false;
        const result = copyCanvasSelection(current, selectedNodeIds);
        if (!result.ok) return false;
        setCanvasCommandMessage(`已复制 ${result.nodeCount} 个节点。`);
        setContextMenu(null);
        return true;
    }, [id, readOnly, selectedNodeIds]);

    const cutSelection = useCallback(() => {
        if (!copySelection()) return false;
        const count = selectedNodeIds.size;
        deleteNodes(selectedNodeIds);
        setCanvasCommandMessage(`已剪切 ${count} 个节点。`);
        return true;
    }, [copySelection, deleteNodes, selectedNodeIds]);

    const pasteSelection = useCallback(() => {
        if (readOnly) return false;
        const current = useCanvasStore.getState().openProject(id);
        if (!current) return false;
        const result = pasteCanvasSelection(current, nanoid);
        if (!result.ok) {
            const messages = {
                empty: "画布剪贴板为空。",
                "node-limit": "粘贴后会超过画布节点上限。",
                "connection-limit": "粘贴后会超过画布连接上限。",
            } as const;
            setCanvasCommandMessage(messages[result.reason]);
            return true;
        }
        updateProject(id, { nodes: [...current.nodes, ...result.nodes], connections: [...current.connections, ...result.connections] });
        setSelectedConnectionKey(null);
        setSelectedNodeIds(new Set(result.pastedNodeIds));
        setContextMenu(null);
        setCanvasCommandMessage(`已粘贴 ${result.nodes.length} 个节点。`);
        return true;
    }, [id, readOnly, updateProject]);

    const beginRename = useCallback((nodeId: string, trigger?: HTMLElement | null) => {
        if (readOnly) return false;
        const current = useCanvasStore.getState().openProject(id);
        if (!current?.nodes.some((node) => node.id === nodeId)) return false;
        renameTriggerRef.current = trigger ?? [...document.querySelectorAll<HTMLElement>("[data-node-id]")].find((element) => element.dataset.nodeId === nodeId) ?? null;
        setContextMenu(null);
        setRenamingNodeId(nodeId);
        return true;
    }, [id, readOnly]);

    const closeRename = useCallback(() => {
        setRenamingNodeId(null);
        const trigger = renameTriggerRef.current;
        renameTriggerRef.current = null;
        window.setTimeout(() => trigger?.focus(), 0);
    }, []);

    const saveNodeTitle = useCallback((nodeId: string, title: string) => {
        if (readOnly) return;
        const current = useCanvasStore.getState().openProject(id);
        if (!current) return;
        updateProject(id, { nodes: current.nodes.map((node) => node.id === nodeId ? { ...node, title } : node) });
        setCanvasCommandMessage(`节点已重命名为“${title}”。`);
        closeRename();
    }, [closeRename, id, readOnly, updateProject]);

    useEffect(() => {
        const handleCanvasShortcut = (event: KeyboardEvent) => {
            if (readOnly || isEditableEventTarget(event.target)) return;
            const key = event.key.toLocaleLowerCase();
            const primaryModifier = event.ctrlKey || event.metaKey;
            if (primaryModifier && key === "a") {
                const current = useCanvasStore.getState().openProject(id);
                if (!current?.nodes.length) return;
                event.preventDefault();
                setSelectedConnectionKey(null);
                setSelectedNodeIds(new Set(current.nodes.map((node) => node.id)));
                setContextMenu(null);
                return;
            }
            if (primaryModifier && key === "c" && copySelection()) { event.preventDefault(); return; }
            if (primaryModifier && key === "x" && cutSelection()) { event.preventDefault(); return; }
            if (primaryModifier && key === "v" && pasteSelection()) { event.preventDefault(); return; }
            if (event.key === "F2" && selectedNodeIds.size === 1) {
                const nodeId = [...selectedNodeIds][0];
                if (beginRename(nodeId)) event.preventDefault();
                return;
            }
            if (event.key !== "Delete" && event.key !== "Backspace") return;
            if (selectedNodeIds.size === 0 && !selectedConnectionKey) return;
            event.preventDefault();
            if (selectedNodeIds.size > 0) deleteNodes(selectedNodeIds);
            else if (selectedConnectionKey) deleteConnection(selectedConnectionKey);
        };
        window.addEventListener("keydown", handleCanvasShortcut);
        return () => window.removeEventListener("keydown", handleCanvasShortcut);
    }, [beginRename, copySelection, cutSelection, deleteConnection, deleteNodes, id, pasteSelection, readOnly, selectedConnectionKey, selectedNodeIds]);

    const finishPortConnection = useCallback(
        (first: GraphPortRef, second: GraphPortRef) => {
            if (readOnly) return false;
            const current = useCanvasStore.getState().openProject(id);
            if (!current) return false;
            const result = connectGraphPorts(first, second, current.nodes, current.connections, nanoid());
            if (!result.ok) {
                setConnectionMessage(graphConnectionRejectionMessage(result.reason));
                return false;
            }
            updateProject(id, { connections: [...current.connections, result.connection] });
            setSelectedNodeIds(new Set());
            setSelectedConnectionKey(graphConnectionTransientKey(result.connection, current.connections.length));
            setPendingPort(null);
            setConnectionMessage("连接已创建。");
            return true;
        },
        [id, readOnly, setPendingPort, updateProject],
    );

    const handlePortPointerDown = useCallback(
        (port: GraphPortRef, event: React.PointerEvent<HTMLButtonElement>) => {
            if (readOnly || port.direction !== "source" || event.button !== 0) return;
            event.preventDefault();
            event.stopPropagation();
            pointerConnectionRef.current = event.pointerId;
            setConnectionMessage(null);
            setConnectionPointerWorld(clientToWorld(event.clientX, event.clientY));
            setPendingPort(port);
        },
        [clientToWorld, readOnly, setPendingPort],
    );

    const handlePortPointerUp = useCallback(
        (port: GraphPortRef, event: React.PointerEvent<HTMLButtonElement>) => {
            if (readOnly || pointerConnectionRef.current !== event.pointerId) return;
            event.preventDefault();
            event.stopPropagation();
            pointerConnectionRef.current = null;
            const first = pendingPortRef.current;
            if (!first || (first.nodeId === port.nodeId && first.portId === port.portId && first.direction === port.direction)) return;
            const suppressedPortKey = `${port.nodeId}\u0000${port.portId}\u0000${port.direction}`;
            suppressPortClickRef.current = suppressedPortKey;
            window.setTimeout(() => {
                if (suppressPortClickRef.current === suppressedPortKey) suppressPortClickRef.current = null;
            }, 0);
            finishPortConnection(first, port);
        },
        [finishPortConnection, readOnly],
    );

    const handlePortClick = useCallback(
        (port: GraphPortRef, event: React.MouseEvent<HTMLButtonElement>) => {
            if (readOnly) return;
            event.preventDefault();
            event.stopPropagation();
            const portKey = `${port.nodeId}\u0000${port.portId}\u0000${port.direction}`;
            if (suppressPortClickRef.current === portKey) {
                suppressPortClickRef.current = null;
                return;
            }
            const first = pendingPortRef.current;
            if (!first) {
                if (port.direction === "source") {
                    setConnectionMessage(null);
                    setPendingPort(port);
                }
                return;
            }
            if (first.nodeId === port.nodeId && first.portId === port.portId && first.direction === port.direction) return;
            finishPortConnection(first, port);
        },
        [finishPortConnection, readOnly, setPendingPort],
    );

    const updateModelNode = useCallback(
        (nodeId: string, graph: GraphModelMetadata) => {
            if (readOnly) return;
            const current = useCanvasStore.getState().openProject(id);
            if (!current) return;
            updateProject(id, { nodes: current.nodes.map((node) => (node.id === nodeId ? { ...node, metadata: { ...node.metadata, model: graph.modelId, params: graph.parameters, graph } } : node)) });
            setModelMessages((messages) => ({ ...messages, [nodeId]: "" }));
        },
        [id, readOnly, updateProject],
    );

    const runModelNode = useCallback(
        (nodeId: string) => {
            if (readOnly) return;
            const current = useCanvasStore.getState().openProject(id);
            const sourceNode = current?.nodes.find((node) => node.id === nodeId);
            if (sourceNode?.metadata?.status === "loading" || sourceNode?.metadata?.jobStatus === "queued" || sourceNode?.metadata?.jobStatus === "running") return;
            const graph = sourceNode?.metadata?.graph;
            const model = graph?.role === "model" ? models.find((candidate) => candidate.model_id === graph.modelId) : undefined;
            if (!current || !model) return;
            try {
                const frozen = compileGraphJob(current.nodes, current.connections, nodeId, model);
                updateProject(id, {
                    nodes: current.nodes.map((node) =>
                        node.id === nodeId ? { ...node, metadata: { ...node.metadata, status: "loading" as const, prompt: frozen.prompt, params: { ...frozen.params }, assetIds: Object.values(frozen.inputs).flat() } } : node,
                    ),
                });
                setModelMessages((messages) => ({ ...messages, [nodeId]: "任务已提交。" }));
                void generation
                    .submit({ ...frozen, asset_ids: [...frozen.asset_ids], inputs: Object.fromEntries(Object.entries(frozen.inputs).map(([portId, ids]) => [portId, [...ids]])), params: { ...frozen.params }, projectId: id, sourceNodeId: nodeId })
                    .catch(() => undefined);
            } catch (error) {
                setModelMessages((messages) => ({ ...messages, [nodeId]: error instanceof CompileJobError ? error.message : "无法编译这个模型任务。" }));
            }
        },
        [generation, id, models, readOnly, updateProject],
    );

    const addModelNode = useCallback(
        (operation: ModelOperation, position?: Position) => {
            if (readOnly) return;
            const current = useCanvasStore.getState().openProject(id);
            const model = models.find((candidate) => candidate.operations.includes(operation));
            if (!current || !model) return;
            const parameters = Object.fromEntries(parameterControls(model.parameter_schema).flatMap((control) => (control.default === undefined ? [] : [[control.name, control.default]]))) as Record<string, GraphParameterValue>;
            const node: CanvasNodeData = {
                id: nanoid(),
                type: CanvasNodeType.Config,
                title: operation.startsWith("video.") ? "视频生成" : "图片生成",
                position: position ?? { x: 112 + current.nodes.length * 24, y: 136 + current.nodes.length * 24 },
                width: 340,
                height: 360,
                metadata: {
                    status: "idle",
                    model: model.model_id,
                    params: parameters,
                    generationMode: operation.startsWith("video.") ? "video" : "image",
                    graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "model", modelId: model.model_id, operation, inputPorts: graphPortsForModel(model), outputPortId: "result", parameters },
                },
            };
            updateProject(id, { nodes: [...current.nodes, node] });
            setSelectedNodeIds(new Set([node.id]));
        },
        [id, models, readOnly, updateProject],
    );
    const addPromptNode = useCallback((position?: Position) => {
        if (readOnly) return;
        const current = useCanvasStore.getState().openProject(id);
        if (!current) return;
        const node: CanvasNodeData = {
            id: nanoid(),
            type: CanvasNodeType.Text,
            title: "提示词",
            position: position ?? { x: 80 + current.nodes.length * 24, y: 80 + current.nodes.length * 24 },
            width: 300,
            height: 250,
            metadata: {
                content: "",
                status: "idle",
                graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt", text: "", outputPortId: "prompt" },
            },
        };
        updateProject(id, { nodes: [...current.nodes, node] });
        setSelectedNodeIds(new Set([node.id]));
    }, [id, readOnly, updateProject]);

    const updatePromptNode = useCallback(
        (nodeId: string, text: string) => {
            if (readOnly) return;
            const current = useCanvasStore.getState().openProject(id);
            if (!current) return;
            const nodes = current.nodes.map((node) => {
                if (node.id !== nodeId) return node;
                const graph = node.metadata?.graph;
                return {
                    ...node,
                    metadata: {
                        ...node.metadata,
                        content: text,
                        graph: graph?.role === "prompt" ? { ...graph, text } : { schemaVersion: GRAPH_SCHEMA_VERSION, role: "prompt" as const, text, outputPortId: "prompt" },
                    },
                };
            });
            updateProject(id, { nodes });
        },
        [id, readOnly, updateProject],
    );

    const addMediaCollectionNode = useCallback(
        (mediaType: GraphMediaType, position?: Position) => {
            if (readOnly) return;
            const current = useCanvasStore.getState().openProject(id);
            if (!current) return;
            const nodeType = mediaType === "image" ? CanvasNodeType.Image : mediaType === "video" ? CanvasNodeType.Video : CanvasNodeType.Audio;
            const title = mediaType === "image" ? "参考图片" : mediaType === "video" ? "参考视频" : "参考音频";
            const node: CanvasNodeData = {
                id: nanoid(),
                type: nodeType,
                title,
                position: position ?? { x: 96 + current.nodes.length * 24, y: 112 + current.nodes.length * 24 },
                width: mediaType === "video" ? 400 : 360,
                height: mediaType === "audio" ? 220 : 300,
                metadata: {
                    status: "idle",
                    graph: { schemaVersion: GRAPH_SCHEMA_VERSION, role: "media-collection", mediaType, outputPortId: "media", items: [] },
                },
            };
            updateProject(id, { nodes: [...current.nodes, node] });
            setSelectedNodeIds(new Set([node.id]));
        },
        [id, readOnly, updateProject],
    );

    const updateMediaCollection = useCallback(
        (nodeId: string, update: MediaItemsUpdater) => {
            if (readOnly) return false;
            const current = useCanvasStore.getState().openProject(id);
            if (!current) return false;
            let found = false;
            const nodes = current.nodes.map((node) => {
                if (node.id !== nodeId || node.metadata?.graph?.role !== "media-collection") return node;
                found = true;
                const items = update(node.metadata.graph.items).map((item) => ({ ...item }));
                return { ...node, metadata: { ...node.metadata, graph: { ...node.metadata.graph, items } } };
            });
            if (found) updateProject(id, { nodes });
            return found;
        },
        [id, readOnly, updateProject],
    );

    const openCanvasContextMenu = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
        if (readOnly) return;
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest("[data-node-id],[data-connection-key],[data-canvas-no-zoom]") && target !== event.currentTarget) return;
        event.preventDefault();
        event.stopPropagation();
        contextTriggerRef.current = event.currentTarget;
        setSelectedNodeIds(new Set());
        setSelectedConnectionKey(null);
        clearPendingConnection();
        setContextMenu({ type: "canvas", x: event.clientX, y: event.clientY, worldPosition: clientToWorld(event.clientX, event.clientY) });
    }, [clearPendingConnection, clientToWorld, readOnly]);

    const createNodeFromContextMenu = useCallback((kind: CanvasCreationKind) => {
        if (contextMenu?.type !== "canvas") return;
        const position = contextMenu.worldPosition;
        let created = true;
        if (kind === "prompt") addPromptNode(position);
        else if (kind === "image" || kind === "video" || kind === "audio") addMediaCollectionNode(kind, position);
        else if (kind === "image-model" && imageCreateOperation) addModelNode(imageCreateOperation, position);
        else if (kind === "video-model" && videoCreateOperation) addModelNode(videoCreateOperation, position);
        else created = false;
        setContextMenu(null);
        setCanvasCommandMessage(created ? "节点已创建在右键位置。" : "没有已授权的对应模型。");
    }, [addMediaCollectionNode, addModelNode, addPromptNode, contextMenu, imageCreateOperation, videoCreateOperation]);

    const openNodeContextMenu = useCallback(
        (nodeId: string, position: { x: number; y: number }, trigger: HTMLDivElement) => {
            if (readOnly) return;
            contextTriggerRef.current = trigger;
            setSelectedNodeIds((current) => current.has(nodeId) ? current : new Set([nodeId]));
            setSelectedConnectionKey(null);
            setContextMenu({ type: "node", nodeId, x: position.x, y: position.y });
        },
        [readOnly],
    );

    const openConnectionContextMenu = useCallback(
        (connectionId: string, connectionKey: string, position: { x: number; y: number }, trigger: SVGPathElement) => {
            if (readOnly) return;
            contextTriggerRef.current = trigger;
            setSelectedNodeIds(new Set());
            setSelectedConnectionKey(connectionKey);
            setContextMenu({ type: "connection", connectionId, connectionKey, x: position.x, y: position.y });
        },
        [readOnly],
    );

    const closeContextMenu = useCallback((restoreFocus = false) => {
        setContextMenu(null);
        if (restoreFocus) contextTriggerRef.current?.focus();
    }, []);

    if (!project) {
        if (loadError)
            return (
                <main role="alert" className="flex h-full items-center justify-center bg-[#050806] px-6 text-center text-[#ffbd73]">
                    {loadError.message}
                </main>
            );
        if (!projectsLoaded) return <main className="flex h-full items-center justify-center bg-[#050806] text-[#829889]">正在加载画布…</main>;
        return <Navigate to="/canvas" replace />;
    }

    return (
        <div className="flex h-full min-h-0 flex-col bg-[#050806] text-[#dceee1]">
            {loadError ? (
                <p role="alert" className="shrink-0 border-b border-[#70502b] bg-[#241a0c] px-4 py-2 text-sm text-[#ffbd73]">
                    {loadError.message}
                </p>
            ) : null}
            {syncNotice ? (
                <p data-testid="project-sync-notice" role="status" aria-live="polite" className="shrink-0 border-b border-[#70502b] bg-[#241a0c] px-4 py-2 text-sm text-[#ffbd73]">
                    {syncNotice}
                </p>
            ) : null}
            <main className="flex min-h-0 flex-1 flex-col overflow-hidden lg:grid lg:grid-cols-[152px_minmax(0,1fr)]">
                <aside data-testid="studio-palette" className="shrink-0 border-b border-[#1d3d28] bg-[#08100b] p-2 lg:border-b-0 lg:border-r lg:p-3">
                    <div className="flex items-center justify-between gap-2 lg:block">
                        <div>
                            <Link to="/canvas" aria-label="返回项目列表" className="mb-1 inline-flex items-center gap-1 px-2 text-[11px] text-[#8fa596] hover:text-[#dceee1] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#58ed87]">
                                <ArrowLeft className="size-3.5" />返回项目列表
                            </Link>
                            <p className="px-2 text-xs tracking-[0.16em] text-[#58ed87] lg:pt-2">NODE PALETTE</p>
                            <h1 className="px-2 py-1 text-sm font-semibold lg:pb-4 lg:pt-2">{project.title}</h1>
                        </div>
                        <div className="flex flex-wrap gap-2 lg:block lg:space-y-2">
                            <button
                                disabled={readOnly}
                                type="button"
                                onClick={() => addPromptNode()}
                                className="flex items-center gap-2 rounded-lg border border-[#254b33] bg-[#0d1b12] px-3 py-2 text-left text-xs hover:border-[#4fbd70] disabled:cursor-not-allowed disabled:opacity-50 lg:w-full lg:py-2.5"
                            >
                                <MessageSquareText className="size-4 text-[#58ed87]" />
                                提示词节点
                            </button>
                            <button
                                disabled={readOnly}
                                type="button"
                                onClick={() => addMediaCollectionNode("image")}
                                className="flex items-center gap-2 rounded-lg border border-[#254b33] bg-[#0d1b12] px-3 py-2 text-left text-xs hover:border-[#4fbd70] disabled:cursor-not-allowed disabled:opacity-50 lg:w-full lg:py-2.5"
                            >
                                <ImagePlus className="size-4 text-[#58ed87]" />
                                参考图节点
                            </button>
                            <button
                                disabled={readOnly}
                                type="button"
                                onClick={() => addMediaCollectionNode("video")}
                                className="flex items-center gap-2 rounded-lg border border-[#254b33] bg-[#0d1b12] px-3 py-2 text-left text-xs hover:border-[#4fbd70] disabled:cursor-not-allowed disabled:opacity-50 lg:w-full lg:py-2.5"
                            >
                                <Film className="size-4 text-[#58ed87]" />
                                参考视频节点
                            </button>
                            <button
                                disabled={readOnly}
                                type="button"
                                onClick={() => addMediaCollectionNode("audio")}
                                className="flex items-center gap-2 rounded-lg border border-[#254b33] bg-[#0d1b12] px-3 py-2 text-left text-xs hover:border-[#4fbd70] disabled:cursor-not-allowed disabled:opacity-50 lg:w-full lg:py-2.5"
                            >
                                <Music2 className="size-4 text-[#58ed87]" />
                                参考音频节点
                            </button>
                            <button
                                disabled={readOnly || imageCreateOperation === null}
                                type="button"
                                onClick={() => imageCreateOperation && addModelNode(imageCreateOperation)}
                                className="flex items-center gap-2 rounded-lg border border-[#254b33] bg-[#0d1b12] px-3 py-2 text-left text-xs hover:border-[#4fbd70] disabled:cursor-not-allowed disabled:opacity-50 lg:w-full lg:py-2.5"
                            >
                                图片生成
                            </button>
                            <button
                                disabled={readOnly || videoCreateOperation === null}
                                type="button"
                                onClick={() => videoCreateOperation && addModelNode(videoCreateOperation)}
                                className="flex items-center gap-2 rounded-lg border border-[#254b33] bg-[#0d1b12] px-3 py-2 text-left text-xs hover:border-[#4fbd70] disabled:cursor-not-allowed disabled:opacity-50 lg:w-full lg:py-2.5"
                            >
                                视频生成
                            </button>
                        </div>
                    </div>
                    <p className="mt-5 hidden px-2 text-[11px] leading-5 text-[#688371] lg:block">集合内顺序决定 @图片N、@视频N、@音频N 的引用编号。</p>
                </aside>
                <section data-testid="studio-canvas" className="embed-surface relative min-h-0 min-w-0 flex-1">
                    <InfiniteCanvas
                        containerRef={containerRef}
                        viewport={viewport}
                        backgroundMode={project.backgroundMode}
                        onViewportChange={changeViewport}
                        onCanvasDeselect={() => {
                            setSelectedNodeIds(new Set());
                            setSelectedConnectionKey(null);
                            setContextMenu(null);
                            clearPendingConnection();
                        }}
                        onContextMenu={readOnly ? undefined : openCanvasContextMenu}
                    >
                        <svg className="pointer-events-none absolute left-0 top-0 z-0 overflow-visible" width="1" height="1" aria-label="画布连接">
                            {resolvedConnections.map(({ connection, connectionKey, active: connectionActive, reason }) => {
                                const from = measuredNodeMap.get(connection.fromNodeId);
                                const to = measuredNodeMap.get(connection.toNodeId);
                                if (!from || !to) return null;
                                return (
                                    <ConnectionPath
                                        key={connectionKey}
                                        connection={connection}
                                        connectionKey={connectionKey}
                                        from={from}
                                        to={to}
                                        active={selectedConnectionKey === connectionKey}
                                        enabled={connectionActive}
                                        inactiveReason={reason ? graphConnectionInactiveMessage(reason) : undefined}
                                        fromPortLabel={getNodePorts(from).sources.find((port) => port.portId === connection.fromPortId)?.label}
                                        toPortLabel={getNodePorts(to).targets.find((port) => port.portId === connection.toPortId)?.label}
                                        onSelect={() => {
                                            if (readOnly) return;
                                            setSelectedNodeIds(new Set());
                                            setSelectedConnectionKey(connectionKey);
                                            setContextMenu(null);
                                        }}
                                        interactive={!readOnly}
                                        onOpenContextMenu={readOnly ? undefined : (position, trigger) => openConnectionContextMenu(connection.id, connectionKey, position, trigger)}
                                    />
                                );
                            })}
                            {pendingPort?.direction === "source" ? (
                                <ActiveConnectionPath node={measuredNodeMap.get(pendingPort.nodeId)} handle={{ nodeId: pendingPort.nodeId, handleType: "source", portId: pendingPort.portId }} mouseWorld={connectionPointerWorld} />
                            ) : null}
                        </svg>
                        {project.nodes.map((node) => {
                            const nodeGraph = node.metadata?.graph;
                            const promptNode = nodeGraph?.role === "prompt";
                            const mediaCollectionNode = nodeGraph?.role === "media-collection";
                            const modelNode = nodeGraph?.role === "model";
                            const modelOperation = modelNode ? (nodeGraph.operation as ModelOperation) : undefined;
                            const ports = getNodePorts(node);
                            const measuredNode = measuredNodeMap.get(node.id) ?? node;
                            return (
                                <DraggableCanvasNode
                                    key={node.id}
                                    node={node}
                                    scale={viewport.k}
                                    selected={selectedNodeIds.has(node.id)}
                                    disabled={readOnly}
                                    contentSized={mediaCollectionNode}
                                    onSelect={
                                        readOnly
                                            ? undefined
                                            : (nodeId, additive) => {
                                                  setSelectedConnectionKey(null);
                                                  setSelectedNodeIds((current) => selectNode(current, nodeId, additive));
                                              }
                                    }
                                    onContextMenu={readOnly ? undefined : openNodeContextMenu}
                                    onPositionChange={moveNode}
                                    onMeasuredSize={recordMeasuredNodeSize}
                                >
                                    {promptNode ? (
                                        <PromptNodeCard node={node} disabled={readOnly} onTextChange={(text) => updatePromptNode(node.id, text)} />
                                    ) : mediaCollectionNode ? (
                                        <MediaCollectionNode node={node} readOnly={readOnly} onItemsChange={(update) => updateMediaCollection(node.id, update)} />
                                    ) : modelNode ? (
                                        <ModelCallNode
                                            node={node}
                                            models={models.filter((model) => modelOperation && model.operations.includes(modelOperation))}
                                            disabled={readOnly}
                                            message={modelMessages[node.id]}
                                            onChange={(graph) => updateModelNode(node.id, graph)}
                                            onRun={() => runModelNode(node.id)}
                                            onRetry={(token) => void generation.retry(token).catch(() => undefined)}
                                            onCancel={(jobId) => void generation.cancelQueued(jobId).catch((error) => setModelMessages((messages) => ({ ...messages, [node.id]: generationErrorMessage(error) })))}
                                        />
                                    ) : (
                                        <GenerationNodeCard node={node} onRetry={readOnly ? undefined : (token) => void generation.retry(token).catch(() => undefined)} onDelete={readOnly ? undefined : () => deleteNodes(new Set([node.id]))} />
                                    )}
                                    {[...ports.targets, ...ports.sources].map((port) => (
                                        <NodePort
                                            key={`${port.direction}:${port.portId}`}
                                            node={measuredNode}
                                            port={port}
                                            active={pendingPort?.nodeId === port.nodeId && pendingPort.portId === port.portId && pendingPort.direction === port.direction}
                                            disabled={readOnly}
                                            onClick={handlePortClick}
                                            onPointerDown={handlePortPointerDown}
                                            onPointerUp={handlePortPointerUp}
                                        />
                                    ))}
                                </DraggableCanvasNode>
                            );
                        })}
                    </InfiniteCanvas>
                    <CanvasNavigationControls viewport={viewport} onViewportChange={changeViewport} />
                    {connectionMessage ? (
                        <p
                            data-testid="connection-status"
                            role="status"
                            aria-live="polite"
                            className="pointer-events-none absolute bottom-14 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-[#356b48] bg-[#08100b]/95 px-3 py-2 text-xs text-[#bcebc9] shadow-xl"
                        >
                            {connectionMessage}
                        </p>
                    ) : null}
                    {canvasCommandMessage ? (
                        <p data-testid="canvas-command-status" role="status" aria-live="polite" className="pointer-events-none absolute bottom-24 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-[#356b48] bg-[#08100b]/95 px-3 py-2 text-xs text-[#bcebc9] shadow-xl">
                            {canvasCommandMessage}
                        </p>
                    ) : null}
                    {inactiveConnectionCount > 0 ? (
                        <p
                            data-testid="inactive-connection-status"
                            role="status"
                            aria-live="polite"
                            className="pointer-events-none absolute bottom-3 left-1/2 z-40 -translate-x-1/2 rounded border border-[#526354] bg-[#08100b]/95 px-2 py-1 text-[11px] text-[#b8cdbd]"
                        >
                            {inactiveConnectionCount} 条连接暂不可用，已保留在画布中。
                        </p>
                    ) : null}
                    {contextMenu?.type === "canvas" ? (
                        <CanvasCreateContextMenu
                            menu={contextMenu}
                            imageModelDisabled={imageCreateOperation === null}
                            videoModelDisabled={videoCreateOperation === null}
                            onClose={closeContextMenu}
                            onCreate={createNodeFromContextMenu}
                        />
                    ) : contextMenu?.type === "node" ? (
                        <CanvasNodeContextMenu
                            menu={contextMenu}
                            onClose={closeContextMenu}
                            onCopy={copySelection}
                            onCut={cutSelection}
                            onRename={() => beginRename(contextMenu.nodeId, contextTriggerRef.current instanceof HTMLElement ? contextTriggerRef.current : null)}
                            onDelete={() => deleteNodes(selectedNodeIds.has(contextMenu.nodeId) ? selectedNodeIds : new Set([contextMenu.nodeId]))}
                        />
                    ) : contextMenu?.type === "connection" ? (
                        <CanvasNodeContextMenu menu={contextMenu} onClose={closeContextMenu} onDelete={() => deleteConnection(contextMenu.connectionKey)} />
                    ) : null}
                    {renamingNodeId ? (() => {
                        const node = project.nodes.find((candidate) => candidate.id === renamingNodeId);
                        return node ? <RenameNodeDialog node={node} onClose={closeRename} onSave={(title) => saveNodeTitle(node.id, title)} /> : null;
                    })() : null}
                </section>
            </main>
        </div>
    );
}
