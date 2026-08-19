const GLOBAL_MEDIA_UPLOAD_CONCURRENCY = 3;

type QueueEntry<T> = {
    run: () => Promise<T>;
    signal: AbortSignal;
    resolve: (value: T) => void;
    reject: (reason: unknown) => void;
    abortQueued: () => void;
};

class MediaUploadScheduler {
    private active = 0;
    private readonly queue: QueueEntry<unknown>[] = [];

    schedule<T>(run: () => Promise<T>, signal: AbortSignal): Promise<T> {
        if (signal.aborted) return Promise.reject(new DOMException("The upload was cancelled.", "AbortError"));
        return new Promise<T>((resolve, reject) => {
            const entry: QueueEntry<unknown> = { run, signal, resolve: resolve as (value: unknown) => void, reject, abortQueued: () => undefined };
            entry.abortQueued = () => {
                const index = this.queue.indexOf(entry);
                if (index < 0) return;
                this.queue.splice(index, 1);
                reject(new DOMException("The upload was cancelled.", "AbortError"));
            };
            signal.addEventListener("abort", entry.abortQueued, { once: true });
            this.queue.push(entry);
            this.drain();
        });
    }

    private drain() {
        while (this.active < GLOBAL_MEDIA_UPLOAD_CONCURRENCY && this.queue.length) {
            const entry = this.queue.shift();
            if (!entry) return;
            entry.signal.removeEventListener("abort", entry.abortQueued);
            if (entry.signal.aborted) {
                entry.reject(new DOMException("The upload was cancelled.", "AbortError"));
                continue;
            }
            this.active += 1;
            void Promise.resolve().then(entry.run).then(entry.resolve, entry.reject).finally(() => {
                this.active -= 1;
                this.drain();
            });
        }
    }
}

export const sharedMediaUploadScheduler = new MediaUploadScheduler();
