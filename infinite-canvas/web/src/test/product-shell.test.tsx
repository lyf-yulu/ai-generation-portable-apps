import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ProductShell } from "@/components/layout/product-shell";
import { useSessionStore } from "@/stores/portal/use-session-store";


beforeEach(() => {
    useSessionStore.setState({
        session: { user_id: "user-a", username: "普通用户 A", role: "user", must_change_password: false },
        environment: "local",
        loading: false,
        errorCode: null,
        logout: vi.fn(async () => useSessionStore.getState().clearSession()),
    });
});

afterEach(() => cleanup());

it("keeps the fixed task tray outside the scrollable content", () => {
    render(<MemoryRouter><ProductShell><div>内容区域</div></ProductShell></MemoryRouter>);

    expect(screen.getByTestId("product-main")).toHaveClass("pb-[var(--task-tray-height)]");
    expect(screen.getByTestId("task-tray")).toHaveClass("fixed", "bottom-0");
    expect(screen.getByTestId("product-main")).not.toContainElement(screen.getByTestId("task-tray"));
});

it("shows only released ordinary-user destinations", () => {
    render(<MemoryRouter><ProductShell><div>内容区域</div></ProductShell></MemoryRouter>);

    expect(screen.getAllByRole("link", { name: "项目" })).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "项目" }).every((link) => link.getAttribute("href") === "/canvas")).toBe(true);
    expect(screen.getAllByRole("link", { name: "资产" }).every((link) => link.getAttribute("href") === "/assets")).toBe(true);
    expect(screen.getAllByRole("link", { name: "任务" }).every((link) => link.getAttribute("href") === "/tasks")).toBe(true);
    expect(screen.queryByRole("link", { name: "管理员" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Skill" })).not.toBeInTheDocument();
    // 退出登录由 Portal 负责，画布内不再提供入口。
    expect(screen.queryByRole("button", { name: "退出登录" })).not.toBeInTheDocument();
});

it("returns to the active canvas after visiting another product page and resets for another user", () => {
    render(<MemoryRouter initialEntries={["/canvas/project-a"]}><ProductShell><div>内容区域</div></ProductShell></MemoryRouter>);

    expect(screen.getAllByRole("link", { name: "项目" }).every((link) => link.getAttribute("href") === "/canvas/project-a")).toBe(true);
    fireEvent.click(screen.getAllByRole("link", { name: "资产" })[0]);
    expect(screen.getAllByRole("link", { name: "项目" }).every((link) => link.getAttribute("href") === "/canvas/project-a")).toBe(true);

    act(() => useSessionStore.setState({ session: { user_id: "user-b", username: "普通用户 B", role: "user", must_change_password: false } }));
    expect(screen.getAllByRole("link", { name: "项目" }).every((link) => link.getAttribute("href") === "/canvas")).toBe(true);
});

it("does not adopt the previous user's canvas when the session changes on that route", () => {
    render(<MemoryRouter initialEntries={["/canvas/project-a"]}><ProductShell><div>内容区域</div></ProductShell></MemoryRouter>);
    expect(screen.getAllByRole("link", { name: "项目" }).every((link) => link.getAttribute("href") === "/canvas/project-a")).toBe(true);

    act(() => useSessionStore.setState({ session: { user_id: "user-b", username: "普通用户 B", role: "user", must_change_password: false } }));

    expect(screen.getAllByRole("link", { name: "项目" }).every((link) => link.getAttribute("href") === "/canvas")).toBe(true);
});
