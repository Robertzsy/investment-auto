# investment-auto

**多市场（A股/港股/美股/ETF）自动化模拟交易系统**

- 支持 A股、港股、美股、场内ETF 独立账户模拟交易
- 内置多 Agent 分析（技术面、基本面、情绪、宏观）与辩论决策
- AI 自主模拟交易闭环：目标仓位决策、硬风控审批、撮合、资金与持仓更新
- 组合优化层：马科维茨、Black-Litterman、风险平价、压力测试
- 多模型接入：OpenAI (GPT)、DeepSeek、GLM (智谱)、Kimi (月之暗面)
- Python + Docker 一键部署，所有配置通过 YAML 文件自定义

## 快速开始

**运行依赖：** Python 3.10+ 与 Node.js 18+。Windows 可先执行 `python --version`、`node --version` 确认；系统启动时也会主动检查 Node.js。宏观采集默认使用 Node 内置 HTTPS；检测到 HTTP(S) 代理时会使用系统 `curl` 作为代理后端（Windows 10/11 自带，Linux 精简系统需另行安装）。

```bash
# 1. 克隆仓库
git clone <repo-url> && cd investment-auto

# 2. 配置环境
cp .env.example .env          # 填入你的 API Key
vim config/config.yaml        # 选择市场、轮次、模型

# 3. 安装 Python 依赖
pip install -r requirements.txt

# 4. 初始化模拟账户
python -m src.main init

# 5. 启动 AI 对话面板（推荐）
python -m src.main chat
# 打开浏览器访问 http://localhost:8080
# Python 部署会在同一进程自动启动 scheduler，并补跑启动前错过的 A 股轮次与宏观日报
# AI 拥有完全操作权限：读写配置、运行优化器、查看调度与日志
# 可用 CHAT_HOST、CHAT_PORT、CHAT_OPEN_BROWSER、CHAT_START_SCHEDULER 覆盖行为

# 6. 仅启动自动化调度器（服务器/拆分部署使用；不要与默认 chat 重复启动）
python -m src.main run

# 7. 手动运行组合优化器（不传 symbols 时使用持仓 + 默认标的池）
python -m src.main optimizer --market cn
python -m src.main optimizer --market cn --symbols 600519,000858,601318,600030

# 8. 手动补跑今天已错过的轮次 / 生成宏观日报
python -m src.main catchup --market cn
python -m src.main macro

# 9. 单次分析
python -m src.main once --market cn

# 10. 手动运行一次自主决策（先用 dry-run 检查）
python -m src.main autonomous --market cn --dry-run
python -m src.main autonomous --market cn

# 11. 少量人工干预：状态、暂停、恢复和紧急停止
python -m src.main status
python -m src.main pause --reason "人工检查"
python -m src.main resume
python -m src.main kill --reason "异常行情"
python -m src.main reset-kill
```

## 配置要点

- `config/config.yaml`：总控制文件，决定市场开关、轮次、启动补跑、宏观日报和模型选择
- 环境变量 `.env`：存储 API Key，切勿提交至 Git
- `config/market/`：各市场风控参数、手续费、交易规则
- `runtime/optimizer/`：组合优化结果；仪表盘会读取最新结果
- `runtime/macro/`：独立宏观日报数据与报告
- `runtime/trading/control.json`：人工暂停与紧急停止状态
- `runtime/trading/audit/`：每轮 Agent 意见、主席决策、风控拒绝与真实模拟成交审计
- `runtime/logs/investment-auto.log`：聊天与调度统一日志
- APScheduler 的星期编号以周一为 `0`。A股/港股/ETF 和美股北京时间晚间轮次使用 `0-4`（周一至周五）；美股北京时间凌晨轮次使用 `schedule.us_early_morning_days`，默认 `1-5`（周二至周六）。设置 `weekdays_only: false` 可允许每日触发。

## AI 自主模拟交易

自主模块采用“AI 决策、代码风控、模拟经纪执行”的分层结构：

1. 技术、研究、量化和风险 Agent 基于同一时点的账户、行情、宏观和优化结果独立分析。
2. 交易主席只输出允许标的的目标仓位、置信度与依据，不能直接修改账户。
3. 硬风控重新计算订单，执行允许池、最低置信度、单标的仓位、单笔金额、单轮换手、现金储备、当日交易次数和最大回撤限制。
4. 模拟经纪处理滑点、佣金、印花税、整手、T+1 可卖数量、现金、持仓成本和成交历史。
5. 硬止损、移动止损和分档止盈由代码产生保护性订单，优先级高于 AI 的主观决策。

公开仓库默认 `autonomous.enabled: false`。首次启用建议：

```yaml
autonomous:
  enabled: true
  auto_execute: true
```

也可通过 `AUTONOMOUS_TRADING_ENABLED=true` 覆盖开关。建议先运行
`python -m src.main autonomous --market cn --dry-run`，检查
`runtime/trading/audit/` 后再允许自动成交。

安全边界：

- 自主执行强制要求 `trading.mode: paper`，代码拒绝其他模式。
- 启动补跑默认不交易，避免使用历史计划时间执行过期订单。
- `kill` 会同时暂停并锁定恢复；必须先 `reset-kill`，再执行 `resume`。
- AI 不能绕过硬风控，也不能交易 `autonomous.universe`/默认标的池和已有持仓以外的代码。
- 当前行情来自公开第三方接口，因此系统适合研究与模拟，不应用于真实资金。

## Docker 部署

```bash
docker compose up -d --build
# 同时启动 scheduler 和 chat 两个服务
docker compose ps
docker compose logs -f scheduler chat

# AI 对话面板 → http://127.0.0.1:8080
```

Compose 中聊天服务在容器内监听 `0.0.0.0:8080`，但端口只发布到宿主机回环地址 `127.0.0.1:8080`，不会直接暴露到局域网或公网。`config/` 与 `runtime/` 由两个服务共享挂载；`.env` 仅作为运行时环境文件使用，不会进入镜像构建上下文。

## 多模型接入

系统默认接入以下主流模型，可在 `config.yaml` 中直接指定：

- OpenAI: `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna`
- DeepSeek: `deepseek-v4-pro` / `deepseek-v4-flash`
- GLM (智谱): `glm-5.2`
- Kimi (月之暗面): `kimi-k3`

## 许可证

MIT
