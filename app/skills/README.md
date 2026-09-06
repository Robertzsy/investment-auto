# 投资 Skills

本目录存放 Investment Auto 2.0 的投资 Skills（DSH 原生 `SKILL.md` 格式），
由 `app/scripts/seed.ps1` 播种到 `$DSH_HOME/skills/`。

格式要求（来自 `dsh-skill-filesystem`）：

- 目录 bundle：`<kebab-case-name>/SKILL.md`
- frontmatter：必填 `name`（kebab-case）与 `description`；可选 `whenToUse`、
  `metadata`、`disable-model-invocation`、`user-invocable`

P2 将把 1.x 的 `src/manager/builtin_skills/` 内容改写为这里的 DSH Skills：

- security-analysis（证券分析）
- market-overview（市场概览）
- stock-screening（选股）
- portfolio-review（组合检查）
- portfolio-optimization（组合优化）
- complete-investment-cycle（完整投资周期）
- account-management（账户管理）

（内容在 P2 阶段落地，当前为空壳目录。）
