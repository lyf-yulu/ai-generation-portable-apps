import { apiFetch } from "./client";
import type { PromptOptimization, PromptSkill } from "./contracts";

let catalogPromise: Promise<PromptSkill[]> | null = null;

export async function fetchPromptSkills(): Promise<PromptSkill[]> {
    catalogPromise ??= apiFetch<{ skills: PromptSkill[] }>("/api/v1/prompt-skills").then((response) => response.skills).catch((error) => {
        catalogPromise = null;
        throw error;
    });
    return catalogPromise;
}

export async function optimizePrompt(skillId: string, prompt: string): Promise<PromptOptimization> {
    if (!/^[a-z][a-z0-9-]{0,63}$/.test(skillId)) throw new Error("Invalid prompt skill");
    return apiFetch<PromptOptimization>(`/api/v1/prompt-skills/${encodeURIComponent(skillId)}/optimize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
    });
}

export function resetPromptSkillCacheForTests() {
    catalogPromise = null;
}
