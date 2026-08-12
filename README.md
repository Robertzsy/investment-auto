# investment-auto

**多市场（A股/港股/美股/ETF）自动化模拟交易系统**

- 支持 A股、港股、美股、场内ETF 独立账户模拟交易
- 内置多 Agent 分析（技术面、基本面、情绪、宏观）与辩论决策
- AI 自主模拟交易闭环：目标仓位决策、硬风控审批、撮合、资金与持仓更新
- 全市场自主选股：流动性/价格/市值/估值硬筛选，多因子评分后交给 Agent 复筛
- 组合优化层：马科维茨、Black-Litterman、风险平价、压力测试
- 多模型接入：OpenAI (GPT)、DeepSeek、GLM (智谱)、Kimi (月之暗面)
- Python + Docker 一键部署，MongoDB 加速选股查询并在不可用时自动回退 JSON

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
# 对话 Agent 使用有边界的类型化工具；配置和交易控制走确定性接口
# 可用 CHAT_HOST、CHAT_PORT、CHAT_OPEN_BROWSER、CHAT_START_SCHEDULER 覆盖行为

# 6. 仅启动自动化调度器（服务器/拆分部署使用；不要与默认 chat 重复启动）
python -m src.main run

# 7. 手动运行组合优化器（不传 symbols 时使用持仓 + 默认标的池）
python -m src.main optimizer --market cn
python -m src.main optimizer --market cn --symbols 600519,000858,601318,600030

# 7a. 只刷新选股池（读取行情并评分，不会下单）
python -m src.main screen --market cn

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
- `runtime/screener/`：全市场候选缓存、因子分数和各市场最新入选结果
- MongoDB（可选）：`securities`、`market_snapshots`、`screening_factors`、`screening_runs` 保存可索引的选股数据；连接异常不阻断调度
- `runtime/macro/`：独立宏观日报数据与报告
- `runtime/trading/control.json`：人工暂停与紧急停止状态
- `runtime/trading/audit/`：每轮 Agent 意见、主席决策、风控拒绝与真实模拟成交审计
- `runtime/logs/investment-auto.log`：聊天与调度统一日志
- APScheduler 的星期编号以周一为 `0`。A股/港股/ETF 和美股北京时间晚间轮次使用 `0-4`（周一至周五）；美股北京时间凌晨轮次使用 `schedule.us_early_morning_days`，默认 `1-5`（周二至周六）。设置 `weekdays_only: false` 可允许每日触发。

## AI 自主模拟交易

自主模块采用“确定性选股、AI 决策、代码风控、模拟经纪执行”的分层结构：

1. A 股/港股/ETF 从新浪市场中心、美股从 Nasdaq Screener 获取按流动性排序的全市场候选；接口异常时回退到优化器默认标的。
2. 代码先排除 ST/退市、特殊证券、低价、低流动性、过小市值、极端涨跌和超出估值门槛的标的，再按动量、趋势、流动性、估值、量能和低波动综合评分。
3. 市场技术、市场情绪、新闻事件和基本面 Agent 先并行形成证据化报告；多头/空头研究员辩论后由研究经理裁决，再由投资建议 Agent 输出候选级方案。
4. 激进、中立、保守三类风险 Agent 评议方案，风险经理裁决后由投资组合经理只输出允许标的的目标仓位、置信度与依据，不能直接修改账户。
5. 硬风控重新计算订单，执行允许池、最低置信度、单标的仓位、单笔金额、单轮换手、现金储备、当日交易次数和最大回撤限制。
6. 模拟经纪处理滑点、佣金、印花税、整手、T+1 可卖数量、现金、持仓成本和成交历史；代码止损止盈优先于 AI 主观决策。

在设置页的“自主选股”区域可修改候选数量、刷新频率、分市场价格/成交额/市值门槛、估值上限和因子权重。`autonomous.universe` 一旦填写就作为硬候选池；全部留空时才启用全市场发现。最近结果会显示在 Dashboard 的“AI 自主选股池”，也会写入每轮交易审计。

设置页“AI 自主交易”中可以启停 13 个分阶段角色、调整多空/风险辩论轮数、强制证据引用和角色独立记忆。系统为行情、技术指标、宏观、筛选、账户、优化器和上游 Agent 报告生成稳定证据 ID；每条事实判断及每个组合决策必须引用有效 ID，伪造或漏引会重试后阻断。记忆按“市场 + 角色”隔离保存在 `runtime/trading/agent_memory/`，每个角色只能读取自己的有限历史，并且记忆不能冒充本轮证据。

同一区域可以选择选股存储后端。默认 `auto`：设置 `MONGODB_URI` 后优先使用 MongoDB，并自动创建复合唯一索引和市场/时间/分数查询索引；未配置、连接超时或写入失败时继续使用 `runtime/screener/` 中的 JSON，不会阻塞交易。直接使用 Docker Compose 时已内置 MongoDB，无需单独配置 URI；本地 Python 运行可填 `mongodb://127.0.0.1:27017`。

如果本机 27017 已被其他项目占用，可以单独启动在其他仅回环端口，例如 `docker run -d --name investment-auto-mongodb --restart unless-stopped -p 127.0.0.1:27018:27017 -v investment-auto-mongodb-data:/data/db mongo:7 --bind_ip_all --quiet`，再设置 `MONGODB_URI=mongodb://127.0.0.1:27018`。

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
- AI 不能绕过硬风控，也不能交易本轮选股池（或显式 `autonomous.universe` 硬候选池）和已有持仓以外的代码。
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

## AI 对话执行边界

对话面板使用 Pydantic AI V2 的原生 function calling，不再从普通回复文本中解析或执行
工具 JSON。开放式问题由主 Agent 按需委派给组合、风控、报告和运行状态专业 Agent；
股票搜索、行情快照和最新选股结果使用类型化只读工具；明确要求“立即筛选”时可刷新单个市场候选池，但该工具不能下单。

- 对话 Agent 没有任意 Shell、文件写入或订单执行工具。
- 启停自主交易、市场状态和组合优化等明确命令优先走确定性路由。
- 每轮限制模型请求数、工具调用数与总 Token；相同工具、参数和结果重复两次会安全停止。
- 交易建议仍必须经过标的池、仓位、现金、换手、每日次数、止损止盈、最大回撤及撮合规则。

## 许可证

MIT
