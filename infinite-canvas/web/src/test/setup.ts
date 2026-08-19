import "@testing-library/jest-dom/vitest";

// antd Modal/Form 等组件依赖 matchMedia;jsdom 不提供,统一 mock 一次。
Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
    }),
});
