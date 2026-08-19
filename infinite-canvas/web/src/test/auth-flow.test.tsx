import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ApiRequestError } from "@/api/client";
import { AuthGate } from "@/components/auth/auth-gate";
import LoginPage from "@/pages/auth/login";
import { useSessionStore } from "@/stores/portal/use-session-store";


const session = { user_id: "user-a", username: "普通用户 A", role: "user" as const, must_change_password: false };

function PrivatePage() {
    const location = useLocation();
    return <div>private:{location.pathname}</div>;
}

function renderFlow() {
    return render(
        <MemoryRouter initialEntries={["/private"]}>
            <Routes>
                <Route path="/login" element={<LoginPage />} />
                <Route path="/private" element={<AuthGate><PrivatePage /></AuthGate>} />
            </Routes>
        </MemoryRouter>,
    );
}

const originalActions = {
    loadSession: useSessionStore.getState().loadSession,
    login: useSessionStore.getState().login,
};

beforeEach(() => {
    useSessionStore.setState(originalActions);
    useSessionStore.setState({ session: null, environment: null, loading: false, errorCode: null });
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

it("redirects an unauthenticated browser to the login page", async () => {
    const error = new ApiRequestError({ code: "AUTH_REQUIRED", message: "Sign in is required.", retryable: false, request_id: "req-auth", phase: "authentication" });
    const loadSession = vi.fn(async () => {
        useSessionStore.setState({ loading: false, errorCode: "AUTH_REQUIRED" });
        throw error;
    });
    useSessionStore.setState({ loadSession });

    renderFlow();

    expect(await screen.findByRole("heading", { name: "登录 AI 创作画布" })).toBeVisible();
    expect(screen.queryByText("private:/private")).not.toBeInTheDocument();
});

it("returns to the requested page after login", async () => {
    const error = new ApiRequestError({ code: "AUTH_REQUIRED", message: "Sign in is required.", retryable: false, request_id: "req-auth", phase: "authentication" });
    const loadSession = vi.fn(async () => {
        useSessionStore.setState({ loading: false, errorCode: "AUTH_REQUIRED" });
        throw error;
    });
    const login = vi.fn(async () => {
        useSessionStore.setState({ session, environment: "local", loading: false, errorCode: null });
        return session;
    });
    useSessionStore.setState({ loadSession, login });

    renderFlow();
    await screen.findByRole("heading", { name: "登录 AI 创作画布" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "canvas-user" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct-horse-battery" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(screen.getByText("private:/private")).toBeVisible());
    expect(login).toHaveBeenCalledWith("canvas-user", "correct-horse-battery", "local");
});

it("does not misreport a session service failure as a login failure", async () => {
    const error = new ApiRequestError({ code: "internal_error", message: "The service failed to process the request.", retryable: true, request_id: "req-down", phase: "response" });
    const loadSession = vi.fn(async () => {
        useSessionStore.setState({ loading: false, errorCode: "internal_error" });
        throw error;
    });
    useSessionStore.setState({ loadSession });

    renderFlow();

    expect(await screen.findByText("暂时无法验证登录状态")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "登录 AI 创作画布" })).not.toBeInTheDocument();
});
