import { upscaleDataUrl } from "@/lib/canvas/canvas-image-data";
import type { AgentAttachment, AgentChatItem } from "@/stores/use-agent-store";
import { captureScopedStore, isStorageLeaseActive, StorageScopeChangedError, type ScopedStoreLease } from "@/storage/scope";

export type StoredAgentUserMessage = Pick<AgentChatItem, "id" | "text" | "attachments"> & { role: "user"; historyText: string; threadId?: string; turnId?: string };

function lease() {
    const captured = captureScopedStore("agent_chat_messages");
    if (!captured) throw new Error("A Portal session is required before accessing chat storage");
    return captured;
}
function assertActive(captured: ScopedStoreLease) {
    if (!isStorageLeaseActive(captured)) throw new StorageScopeChangedError();
}
const mutations = new Map<string, Promise<void>>();
const indexKey = (threadId: string) => `thread:${threadId}`;
const messageKey = (threadId: string, messageId: string) => `message:${threadId}:${messageId}`;
const pendingKey = (messageId: string) => `pending:${messageId}`;
const threadMutationKey = (threadId: string) => `thread:${threadId}`;
const pendingMutationKey = (messageId: string) => `pending:${messageId}`;

export async function saveAgentUserMessage(threadId: string, message: StoredAgentUserMessage) {
    if (!message.attachments?.length) return;
    if (!threadId) return savePendingAgentUserMessage(message);
    await saveThreadAgentUserMessage(threadId, message);
}

/** Persist attachments before a turn is accepted. The record is moved to a thread after the server assigns one. */
export async function savePendingAgentUserMessage(message: StoredAgentUserMessage) {
    if (!message.id || !message.attachments?.length) return;
    const captured = lease();
    await mutateScopes([pendingMutationKey(message.id)], async () => {
        const attachments = await Promise.all(message.attachments!.map(createThumbnail));
        assertActive(captured);
        await captured.store.setItem(pendingKey(message.id), { ...message, threadId: undefined, turnId: undefined, attachments });
        assertActive(captured);
    });
}

export async function deletePendingAgentUserMessage(messageId: string) {
    if (!messageId) return;
    const captured = lease();
    await mutateScopes([pendingMutationKey(messageId)], async () => { assertActive(captured); await captured.store.removeItem(pendingKey(messageId)); assertActive(captured); });
}

export async function readAgentUserMessages(threadId: string) {
    const captured = lease();
    await mutations.get(threadMutationKey(threadId))?.catch(() => undefined);
    assertActive(captured);
    const ids: string[] = (await captured.store.getItem<string[]>(indexKey(threadId))) || [];
    assertActive(captured);
    const result = await Promise.all(ids.map((id: string) => captured.store.getItem<StoredAgentUserMessage>(messageKey(threadId, id))));
    assertActive(captured);
    return result.filter((item: StoredAgentUserMessage | null): item is StoredAgentUserMessage => Boolean(item));
}

/** Bind a pending message to the server thread, preserving an already-known turn id. */
export async function bindPendingAgentUserMessage(threadId: string, messageId: string, turnId = "") {
    if (!threadId || !messageId) return;
    const captured = lease();
    await mutateScopes([pendingMutationKey(messageId), threadMutationKey(threadId)], async () => {
        const pending = await captured.store.getItem<StoredAgentUserMessage>(pendingKey(messageId)); assertActive(captured);
        const key = messageKey(threadId, messageId);
        const existing = await captured.store.getItem<StoredAgentUserMessage>(key); assertActive(captured);
        if (!pending && !existing) return;
        const message = mergeStoredMessage(existing, pending, threadId, turnId);
        await putThreadMessage(captured, threadId, key, message);
        if (pending) { await captured.store.removeItem(pendingKey(messageId)); assertActive(captured); }
    });
}

export async function bindAgentUserMessageTurn(threadId: string, messageId: string, turnId: string) {
    await bindPendingAgentUserMessage(threadId, messageId, turnId);
}

export async function moveAgentUserMessage(fromThreadId: string, toThreadId: string, messageId: string) {
    if (!toThreadId || !messageId || fromThreadId === toThreadId) return bindPendingAgentUserMessage(toThreadId, messageId);
    const scopes = [pendingMutationKey(messageId), threadMutationKey(toThreadId), ...(fromThreadId ? [threadMutationKey(fromThreadId)] : [])];
    const captured = lease();
    await mutateScopes(scopes, async () => {
        const pending = await captured.store.getItem<StoredAgentUserMessage>(pendingKey(messageId)); assertActive(captured);
        const fromKey = fromThreadId ? messageKey(fromThreadId, messageId) : "";
        const from = fromKey ? await captured.store.getItem<StoredAgentUserMessage>(fromKey) : null; assertActive(captured);
        const toKey = messageKey(toThreadId, messageId);
        const existing = await captured.store.getItem<StoredAgentUserMessage>(toKey); assertActive(captured);
        const source = pending || from;
        if (!source && !existing) return;
        await putThreadMessage(captured, toThreadId, toKey, mergeStoredMessage(existing, source, toThreadId));
        if (pending) { await captured.store.removeItem(pendingKey(messageId)); assertActive(captured); }
        if (from && fromThreadId) await removeThreadMessage(captured, fromThreadId, fromKey, messageId);
    });
}

export async function deleteAgentThreadMessages(threadIds: string[]) {
    const captured = lease();
    await mutateScopes(threadIds.map(threadMutationKey), async () => {
        await Promise.all(threadIds.map(async (threadId) => {
            const ids: string[] = (await captured.store.getItem<string[]>(indexKey(threadId))) || []; assertActive(captured);
            await Promise.all(ids.map((id: string) => captured.store.removeItem(messageKey(threadId, id)))); assertActive(captured);
            await captured.store.removeItem(indexKey(threadId)); assertActive(captured);
        }));
    });
}

async function saveThreadAgentUserMessage(threadId: string, message: StoredAgentUserMessage) {
    const captured = lease();
    await mutateScopes([threadMutationKey(threadId)], async () => {
        const attachments = await Promise.all(message.attachments!.map(createThumbnail));
        assertActive(captured);
        await putThreadMessage(captured, threadId, messageKey(threadId, message.id), { ...message, threadId, attachments });
    });
}

async function putThreadMessage(captured: ScopedStoreLease, threadId: string, key: string, message: StoredAgentUserMessage) {
    await captured.store.setItem(key, { ...message, threadId }); assertActive(captured);
    const ids = (await captured.store.getItem<string[]>(indexKey(threadId))) || []; assertActive(captured);
    if (!ids.includes(message.id)) { await captured.store.setItem(indexKey(threadId), [...ids, message.id]); assertActive(captured); }
}

function mergeStoredMessage(existing: StoredAgentUserMessage | null, source: StoredAgentUserMessage | null | undefined, threadId: string, turnId = "") {
    const message = { ...(source || {}), ...(existing || {}) } as StoredAgentUserMessage;
    if (!message.attachments?.length && source?.attachments?.length) message.attachments = source.attachments;
    if (!message.text && source?.text) message.text = source.text;
    if (!message.historyText && source?.historyText) message.historyText = source.historyText;
    return { ...message, threadId, ...(turnId ? { turnId } : message.turnId ? { turnId: message.turnId } : {}) };
}

async function removeThreadMessage(captured: ScopedStoreLease, threadId: string, key: string, messageId: string) {
    await captured.store.removeItem(key); assertActive(captured);
    const ids: string[] = (await captured.store.getItem<string[]>(indexKey(threadId))) || []; assertActive(captured);
    const remaining = ids.filter((id: string) => id !== messageId);
    if (remaining.length) await captured.store.setItem(indexKey(threadId), remaining);
    else await captured.store.removeItem(indexKey(threadId));
    assertActive(captured);
}

async function mutateScopes(scopes: string[], mutation: () => Promise<void>) {
    const ids = [...new Set(scopes.filter(Boolean))].sort();
    const operation = Promise.all(ids.map((id) => mutations.get(id)?.catch(() => undefined))).then(mutation);
    ids.forEach((id) => mutations.set(id, operation));
    try {
        await operation;
    } finally {
        ids.forEach((id) => {
            if (mutations.get(id) === operation) mutations.delete(id);
        });
    }
}

async function createThumbnail(attachment: AgentAttachment): Promise<AgentAttachment> {
    const dataUrl = Math.max(attachment.width, attachment.height) > 512 ? await upscaleDataUrl(attachment.dataUrl, { targetLongEdge: 512, algorithm: "high" }) : attachment.dataUrl;
    return { ...attachment, size: dataUrl.length, url: dataUrl, dataUrl };
}
