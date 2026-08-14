import { playwright } from "@vitest/browser-playwright";
import { configDefaults, defineConfig, mergeConfig } from "vitest/config";

import baseConfig from "./vite.config";


const browserConfig = mergeConfig(baseConfig, defineConfig({
    optimizeDeps: {
        include: ["@tanstack/react-query", "antd", "antd/locale/zh_CN", "fflate", "file-saver"],
    },
    test: {
        include: ["src/test/**/*.browser.test.tsx"],
        browser: {
            enabled: true,
            provider: playwright({ launchOptions: { channel: "chrome" } }),
            headless: true,
            instances: [{ browser: "chromium" }],
        },
    },
}));

browserConfig.test = { ...browserConfig.test, exclude: [...configDefaults.exclude] };

export default browserConfig;
