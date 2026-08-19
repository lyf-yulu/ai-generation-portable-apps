import { uploadMediaFile, type UploadedFile } from "@/services/file-storage";
import type { AiConfig } from "@/stores/use-config-store";
export async function requestAudioGeneration(_config: AiConfig, _prompt: string, _options?: { signal?: AbortSignal }): Promise<Blob> { throw new Error("音频生成尚未接入受控任务服务"); }
export const storeGeneratedAudio = (blob: Blob, _format = "mp3"): Promise<UploadedFile> => uploadMediaFile(blob, "audio");
