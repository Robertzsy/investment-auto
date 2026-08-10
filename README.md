# investment-auto

**多市场（A股/港股/美股/ETF）自动化模拟交易系统**

- 支持 A股、港股、美股、场内ETF 独立账户模拟交易
- 内置多 Agent 分析（技术面、基本面、情绪、宏观）与辩论决策
- 组合优化层：马科维茨、Black-Litterman、风险平价、压力测试
- 多模型接入：OpenAI (GPT)、DeepSeek、GLM (智谱)、Kimi (月之暗面)
- Python + Docker 一键部署，所有配置通过 YAML 文件自定义

## 快速开始

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
# AI 拥有完全操作权限：读写配置、运行优化器、启停调度

# 6. 启动自动化调度器
python -m src.main run

# 7. 单次分析
python -m src.main once --market cn
```

## 配置要点

- `config/config.yaml`：总控制文件，决定市场开关、盘中建仓次数/时间、模型选择
- 环境变量 `.env`：存储 API Key，切勿提交至 Git
- `config/market/`：各市场风控参数、手续费、交易规则

## Docker 部署

```bash
docker compose up -d --build
# AI 对话面板 → http://localhost:8080
```

## 多模型接入

系统默认接入以下主流模型，可在 `config.yaml` 中直接指定：

- OpenAI: `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna`
- DeepSeek: `deepseek-v4-pro` / `deepseek-v4-flash`
- GLM (智谱): `glm-5.2`
- Kimi (月之暗面): `kimi-k3`

## 许可证

MIT
