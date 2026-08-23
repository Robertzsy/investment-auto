# Investment Auto 产品外壳改造设计（DSH 内核嵌入）

## 目标

把 investment-web 从「DSH 默认外壳 + 投资 preset」改造成独立投资产品：
无可见 DSH 品牌、无 Workspace、无模式/preset 选择、产品级主导航
（Dashboard / 投资助手 / 分析流程 / 设置），对话思考体验零改动。

## 关键勘察结论（rc.6 实测）

1. **对话内核可无损嵌入**：`ui-layout` 注册 `root` 槽并声明子槽
   `sidebar / conversation / details / shell.overlay`；`ui-conversation`
   把对话注册为 `conversation` 槽，**所有者只需 `renderSlot("conversation",
   {})`**，无需任何业务 props（会话上下文由槽机制的 scope 提供）。→
   新外壳接管 `root`，原样渲染 conversation 槽即可，思考/流式/工具链
   组件零改动。
2. **设置面板是槽体系**：settings-general 注册 `sidebar.settings` 并声明
   `settings.trigger/header/action/close/section/onboarding`；各设置页
   （models 等）注册进 `settings.section`（list 槽，key=id，带 label/order）。
   → 新外壳可自建 settings 外壳（同名子槽声明），复用现有 models 页并
   追加投资设置页。
3. **会话服务 API 足够**：`ctx.sessions`（open/search/binding().rename/
   fork）+ `ctx.workspaces`（startSession/archiveSession）；主机侧无默认
   workspace（实测空目录下 workspace.list 为空）→ 产品外壳的 node 半在
   启动时创建固定 Investment Auto 工作区（path=DSH_HOME）。
4. **品牌分布**：JS bundle 中无用户可见 "DeepSeek/Harness" 字符串（仅
   module id 与 CSS token 名）；可见品牌 = `<title>`、manifest
   name/short_name、favicon（鱼形 logo）、ui-sidebar 的 BrandWordmark
   （该行随外壳替换消失）。→ 品牌去除 = 组合层替换 + dist 三处 patch。

## 架构

```
product shell（新客户端插件 @investment-auto/dsh-product-shell）
├─ root 槽（替代 ui-layout）：左侧产品导航 [Dashboard|投资助手|分析流程|设置]
│   ├─ 投资助手页：会话二级栏（新建/扁平会话/搜索/归档）
│   │              + renderSlot("conversation", {}) + 可收起投资上下文列
│   ├─ Dashboard 页：数据来自 host 代理 /api/investment/summary
│   ├─ 分析流程页：展示真实轮次、Agent/证据计数、阶段检查点与完整多角色链路
│   └─ 设置页：自建 settings 外壳（渲染 settings.section 各页）
├─ node 半：启动时创建固定工作区；注册 /api/investment/* 代理
│            （引擎 URL+令牌仅存在于 host，不进浏览器 JS）
└─ 设置页插件：策略与风控 / 轮次与调度 / 市场与选股 / 通知 / 应用设置
```

产品外壳只在展示层隐藏对话组件残留的 Workspace/访问模式选择器，并把空白页标题和
输入框提示改成投资语义；消息、思考过程、流式输出、工具链和会话状态仍由原
conversation 组件完整负责。

引擎侧新增（不绕过 DPAPI）：`GET /api/config`（脱敏全量）+
`POST /api/config/update`（产品设置精确字段、类型与范围校验 + 原子写 + reload）；
另有 `/api/analysis/*` 保存和查询多角色工作流的真实阶段与检查点。

## 组合层移除（investment-web profile patch）

disable：`ui-layout`、`ui-sidebar`、`ui-workspace`、`ui-settings-general`、
`ui-settings-plugin-inventory`、`ui-agent-preset`、`ui-permission`、
`ui-cordis`、`ui-model-selection`。
保留：`ui-conversation`、`ui-tool`、`ui-skill`、`ui-subagent`、`ui-jobs`、
`ui-goal`、`ui-plan`、`ui-user-questions`、`ui-trajectory`、
`ui-message-feedback`、`ui-commands`、`ui-input-trigger`、`ui-attachment`、
`ui-theme`、`ui-locale`、`ui-settings`（服务）、`ui-settings-models`、
`ui-deliverables`、`client-hmr`、`investment-ui`（工具卡片）。
新增：`@investment-auto/dsh-product-shell`（root/sidebar/settings 外壳 +
host 代理 + 设置页 + Dashboard）。

agent-presets 行保留（机制），default 恒为 investment；preset 选择 UI 已
随 ui-agent-preset 移除，新建会话自动 investment（已实测）。

## 品牌

`app/scripts/brand-dist.mjs`：dev（npm ci 后）与发行前执行，改写
dist/index.html（title）、manifest.webmanifest（name/short_name）、
favicon.svg（Investment Auto 标记）。web-runtime 行改 `surfaceContext:
false`（同时移除模型可见的 harness-source 提示段）。

## 验收

状态：全部完成（2026-08-22）。

1. ✅ boot 表不含 ui-layout/ui-sidebar/ui-workspace/ui-agent-preset 等条目（9+2
   行禁用，`--dump-config` 逐行确认 `disabled: true`；product-shell 挂载）；
2. ✅ 页面 title/favicon/manifest 为 Investment Auto（`app/scripts/brand-dist.mjs`，
   无头浏览器断言无 DeepSeek/Harness 可见文本；模型页仅出现模型供应商名 DeepSeek）；
3. ✅ wire 协议驱动：session.create → preset=investment；prompt → investment_status
   工具调用与中文回答正常（`app/scripts/verify-web-conversation.mjs`）；对话内核 =
   未改动的 conversation 组件（10 个内核 bundle SHA-256 记录于 CHANGELOG/本文件，
   外壳仅 renderSlot("conversation", {})）；
4. ✅ Dashboard/设置页数据与引擎一致；策略切换/调度配置经 `/api/investment/*` 代理
   写回引擎（host 持有引擎 URL+令牌，浏览器不可见）；Webhook DPAPI 存储不回显；
5. ✅ 无头浏览器验收（`app/scripts/smoke-web.mjs`，Chrome CDP）：左侧产品
   导航、扁平会话列表、投资上下文、Dashboard、分析流程、五个设置页 + 模型页渲染、
   页面零异常、client bundle 无 404；桌面壳与安装器全链路回归（release-check）。

## 实现备注（坑）

- profile patch 不能改行 `name`（name 不匹配静默跳过）——旧行一律 `disabled: true`
  + `insert` 新行。
- `root` 是 single 槽：必须禁用 ui-layout，否则后注册者「遮蔽」而不是并列。
- ui-layout 退役后 theme 投影（body token/dark 属性/theme-color meta）由其负责——
  产品外壳接管（ThemePresenter 复制实现）。
- 槽注册 `store:` 收工厂函数；`inject(actions)` 收到的是烘焙 actions（draft 参数
  已剥离），defineStore 动作签名是 `(draft, ...args) => ...`。
- `ctx.slots.inject(key, fn)` 是「等声明再注册」：声明方与注册方顺序无关，但
  `settings.section` 等槽必须由外壳（root 的子槽）声明，禁用 settings-general 后
  该声明随之消失。
- settings 页组件直接以 `<SettingsSurface {...props}/>` 渲染时拿不到槽注入的
  sectionsSource——改为 root 注册的 inject 返回 settings API，经 ShellFrame props
  透传；settings.section/onboarding 声明挂在 root 下。
- 测试全局态：config_api 测试夹具曾污染全局 `cfg` 单例（`_data`/`_path` 不还原），
  导致后续 scheduler/decision 测试读到临时配置——夹具改为完整还原。
