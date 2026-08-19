import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ObjectLifecycleActions } from "@/components/admin/object-lifecycle-actions";
import { ApiRequestError } from "@/api/client";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("requires the display name for deletion and restores focus on Escape", async () => {
    const remove = vi.fn();
    render(<ObjectLifecycleActions objectLabel="Nano Banana" enabled revision={4} onEnable={vi.fn()} onDisable={vi.fn()} onArchive={vi.fn()} onRestore={vi.fn()} onDelete={remove} onPurge={vi.fn()} onChanged={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "删除" });
    trigger.focus();
    fireEvent.click(trigger);
    const input = screen.getByLabelText("输入 Nano Banana 确认删除");
    expect(input).toHaveFocus();
    expect(screen.getByRole("button", { name: "确认删除" })).toBeDisabled();
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(trigger).toHaveFocus());
    fireEvent.click(trigger);
    fireEvent.change(screen.getByLabelText("输入 Nano Banana 确认删除"), { target: { value: "Nano Banana" } });
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(4));
});

it("shows only bounded reference counts and a refresh action for stale revisions", async () => {
    const references = new ApiRequestError({ code: "RESOURCE_REFERENCED", message: "safe", retryable: false, request_id: "r", phase: "request" }, { job: 2, route: 1 });
    const refresh = vi.fn();
    const { rerender } = render(<ObjectLifecycleActions objectLabel="Banana" enabled revision={1} onEnable={vi.fn()} onDisable={vi.fn()} onArchive={vi.fn().mockRejectedValue(references)} onRestore={vi.fn()} onDelete={vi.fn()} onPurge={vi.fn()} onChanged={vi.fn()} onRefresh={refresh} />);
    fireEvent.click(screen.getByRole("button", { name: "归档" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("任务 2");
    expect(screen.getByRole("alert")).toHaveTextContent("线路 1");

    const conflict = new ApiRequestError({ code: "REVISION_CONFLICT", message: "raw ignored", retryable: false, request_id: "r", phase: "request" });
    rerender(<ObjectLifecycleActions objectLabel="Banana" enabled revision={1} onEnable={vi.fn()} onDisable={vi.fn().mockRejectedValue(conflict)} onArchive={vi.fn()} onRestore={vi.fn()} onDelete={vi.fn()} onPurge={vi.fn()} onChanged={vi.fn()} onRefresh={refresh} />);
    fireEvent.click(screen.getByRole("button", { name: "停用" }));
    expect(await screen.findByText("配置已变化，请重新加载。" )).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refresh).toHaveBeenCalledTimes(1);
});

it("drops a late action from the previously selected object and closes its confirmation", async () => {
    let rejectOld!: (reason: unknown) => void;
    const archive = vi.fn(() => new Promise<unknown>((_resolve, reject) => { rejectOld = reject; }));
    const changed = vi.fn();
    const props = { enabled: true, onEnable: vi.fn(), onDisable: vi.fn(), onRestore: vi.fn(), onDelete: vi.fn(), onPurge: vi.fn(), onChanged: changed };
    const { rerender } = render(<ObjectLifecycleActions {...props} objectIdentity="model-a" objectLabel="A" revision={1} onArchive={archive} />);
    fireEvent.click(screen.getByRole("button", { name: "归档" }));
    rerender(<ObjectLifecycleActions {...props} objectIdentity="model-b" objectLabel="B" revision={1} onArchive={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("B");
    rerender(<ObjectLifecycleActions {...props} objectIdentity="model-c" objectLabel="C" revision={1} onArchive={vi.fn()} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    rejectOld(new ApiRequestError({ code: "REVISION_CONFLICT", message: "", retryable: false, request_id: "r", phase: "request" }));
    await Promise.resolve();
    expect(screen.queryByText("配置已变化，请重新加载。" )).not.toBeInTheDocument();
    expect(changed).not.toHaveBeenCalled();
});
