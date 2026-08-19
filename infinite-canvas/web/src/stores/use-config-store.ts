import { useMemo } from "react";
import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ModelCapability = "image" | "video" | "text" | "audio";
export type ReasoningEffort = "auto" | "low" | "medium" | "high" | "xhigh";
export type AiConfig = {
    model: string; imageModel: string; videoModel: string; textModel: string; audioModel: string;
    audioVoice: string; audioFormat: string; audioSpeed: string; audioInstructions: string;
    videoSeconds: string; vquality: string; videoGenerateAudio: string; videoWatermark: string;
    reasoningEffort: ReasoningEffort; models: string[]; quality: string; size: string; background: string; count: string; canvasImageCount: string;
};
export const defaultConfig: AiConfig = { model: "", imageModel: "", videoModel: "", textModel: "", audioModel: "", audioVoice: "alloy", audioFormat: "mp3", audioSpeed: "1", audioInstructions: "", videoSeconds: "6", vquality: "720", videoGenerateAudio: "true", videoWatermark: "false", reasoningEffort: "auto", models: [], quality: "auto", size: "1:1", background: "", count: "1", canvasImageCount: "3" };
type ConfigStore = { config: AiConfig; updateConfig: <K extends keyof AiConfig>(key: K, value: AiConfig[K]) => void; isAiConfigReady: (_config: AiConfig, model: string) => boolean };
export const useConfigStore = create<ConfigStore>()(persist((set) => ({ config: defaultConfig, updateConfig: (key, value) => set((state) => ({ config: { ...state.config, [key]: value } })), isAiConfigReady: (_config, model) => Boolean(model.trim()) }), { name: "ai-creation-canvas:preferences", partialize: (state) => ({ config: { ...state.config, models: [] } }) }));
export function clearGenerationPreferences() { useConfigStore.setState({ config: { ...defaultConfig } }); }
export function useEffectiveConfig() { const config = useConfigStore((state) => state.config); return useMemo(() => config, [config]); }
export function modelOptionName(value: string) { return value; }
export function modelOptionLabel(value: string) { return value || "由服务端选择"; }
export function normalizeModelOptionValue(value: string | undefined) { return value || ""; }
export function selectableModelsByCapability(config: AiConfig, _capability?: ModelCapability) { return config.models; }
export function resolveModelForCapability(config: AiConfig, current: string | undefined, capability: ModelCapability) { return current || (capability === "image" ? config.imageModel : capability === "video" ? config.videoModel : capability === "audio" ? config.audioModel : config.textModel); }
/** Keeps callers on the local preference model while all network routing stays server-side. */
export function resolveModelRequestConfig(config: AiConfig, model: string) { return { ...config, model }; }
