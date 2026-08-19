# 飞书 Agent Portal 内嵌样式恢复设计

**日期：** 2026-07-27  
**状态：** 已确认  
**范围：** 只恢复现有 Agent 前端在 Portal iframe 中的原有样式，不调整布局、交互或业务流程

## 1. 问题与证据

生产 Portal 中的 Agent 页面已经加载 HTML、JavaScript 和任务数据，但
`styles.css` 没有在用户浏览器中生效，页面因此退化为浏览器默认样式。

同一版本通过 Agent 直连地址访问时样式正常；使用隔离的 HTTP Portal 代理访问时
样式也正常。这说明页面实现本身没有缺失，问题位于生产 iframe、HTTPS 代理和
浏览器样式资源缓存/交付的边界。修复目标是不再让首屏样式依赖第二个 CSS 请求。

## 2. 方案

保留 `web/static/styles.css` 作为唯一的样式源文件。Agent 的 `/` 路由每次响应时：

1. 读取现有 `index.html`；
2. 读取现有 `styles.css`；
3. 将 HTML 中唯一的外链样式标签替换为
   `<style data-agent-inline-styles>...</style>`；
4. 返回带 `no-cache, no-store, must-revalidate` 的 HTML。

`/static/styles.css` 继续保留，方便直接检查、自动化测试和后续静态资源使用。
JavaScript 文件和 API 路径不变，因此任务扫描、审批、生成、结果表、个人计划提示词
以及 Portal 用户隔离均不受影响。

## 3. 边界与失败策略

- 不复制 CSS 到 `index.html`，避免出现两份样式源。
- 不修改 Portal iframe 地址、Agent 页面结构或视觉设计。
- HTML 中的目标 `<link>` 必须恰好存在一次；模板结构意外变化时返回明确错误，
  不静默交付无样式页面。
- CSS 仍在每次页面请求时从磁盘读取，保持当前“前端文件修改后刷新即生效”的行为。
- 不增加第三方依赖，不增加客户端构建步骤。

## 4. 验证

自动化测试需要证明：

- `/` 返回内嵌样式标记和现有 CSS 内容；
- `/` 不再包含原来的外链样式标签；
- `/static/styles.css` 仍返回 `text/css`；
- 原有静态前端、API、Portal 代理和用户身份测试保持通过。

生产验证需要证明：

- Agent 直连页面保留原样式；
- Portal iframe 页面计算样式不再是浏览器默认值；
- 动画类、真人类、最近任务、审批区和工作流区仍可见；
- 无运行中生成任务后才重启 Agent。
