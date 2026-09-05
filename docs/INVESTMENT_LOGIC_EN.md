# Investment Auto: Investment Logic Explained

This document explains the core investment intelligence of Investment Auto 2.1.3: **screening logic**, the **multi-role analysis pipeline**, **decision and risk control**, and a **business-value assessment** for each.

> This document covers the investment logic itself; reliability mechanisms (checkpoint recovery, idempotent receipts, credential storage) are covered in the [2.0 architecture](ARCHITECTURE_2.0.md).

## 1. Screening: Why One Stock Is More Worth Buying Than Another

The design principle is nine words: **exclude first, score second, explain last**. Screening is a deterministic two-stage pipeline—the LLM does not improvise.

### 1.1 Hard Filters (Exclude First)

- **Symbol normalization**: strip `sh/sz/bj/hk/us` prefixes to unify security identity, so the same stock is never double-counted or missed because of notation differences;
- **Five classes of hard thresholds**: minimum price, minimum turnover, minimum market cap, PE cap, PB cap, and maximum daily move;
- **Name-list exclusions**: exclude ST/*ST, delisting, warrants (WARRANT/RIGHT/UNIT/PREFERRED) and similar by name pattern; the ETF market additionally excludes money-market, short-term financing, and bond instruments;
- **Auditable rejections**: every rejected stock records which rule rejected it.

This layer blocks liquidity traps, delisting risk, extreme moves, and shell speculation first—**"don't buy the wrong one" comes before "buy the better one".**

### 1.2 Six-Factor Weighted Scoring (Then Score)

| Factor (weight) | How it is computed | Question it answers |
|---|---|---|
| Momentum (0.28) | 5/20-day price change, clamp-mapped | Is it strengthening right now |
| Trend (0.22) | MA5/10/20/60 alignment + MACD bars + RSI14 | Mid-term trend direction and health |
| Liquidity (0.20) | Turnover percentile within the sample | Can I get in and out |
| Valuation (0.12) | PE/PB historical band scoring | Is it expensive right now (auxiliary) |
| Volume (0.10) | Relative volume change | Is capital paying attention |
| Low volatility (0.08) | Recent volatility level | Can I hold it |

Composite score = Σ(factor score × weight). The top N stocks by score (default cap 30) are taken, then an Agent re-screen produces the normalized candidate list. Every selected stock carries Chinese-language evidence, and weights and thresholds are configurable.

**Worked example (illustrative numbers)**: stock A is up 8.3% over 20 days (momentum 0.90), has bullish moving-average alignment (trend 0.85), sits at the 92nd turnover percentile (liquidity 0.92), and trades at the 45th PE percentile (valuation 0.60), for a weighted total of ≈ 0.81; stock B scores ≈ 0.36. A is selected, with evidence such as "up 8.3% over 20 days, 92nd percentile of the sample; bullish moving-average alignment; turnover at the 92nd percentile; PE at the 45th percentile of the last three years—neutral valuation."

### 1.3 Business-Value Assessment

- **Explainable → trustworthy**: every pick carries a written reason the user can review;
- **Discipline → fewer traps**: exclusion rules keep ST stocks, liquidity traps, and delisting risk out;
- **Zero cost → margin advantage**: screening is deterministic code that consumes no tokens; the LLM is reserved for the re-screen and deep analysis;
- **Reproducible → auditable**: the same market data always yields the same candidate list;
- **Configurable → productizable**: factor weights, thresholds, and exclusion rules are configurable, supporting productized strategy styles.

## 2. Multi-Role Analysis: How a "Buy or Not" Verdict Is Reached

A single model "wants to buy everything", so IA moves an institutional research committee into code—13 roles across five stages.

### 2.1 Five Stages and 13 Roles

| Stage | Roles | Responsibility |
|---|---|---|
| ① Base research (parallel per symbol) | Technical / fundamental / news / sentiment analysts | Research each dimension with cited evidence |
| ② Research debate | Bull / bear researchers | Argue both sides |
| ② Research debate | Research manager | Adjudicate disagreements and evidence sufficiency |
| ② Research debate | Trader (per symbol) | Convert conclusions into BUY/SELL/HOLD + target weight + confidence |
| ③ Portfolio draft | — | Combine per-symbol results into a draft portfolio |
| ④ Risk debate | Aggressive / conservative / neutral analysts | Debate risk from three stances |
| ④ Risk debate | Risk manager | Risk adjudication and constraints |
| ⑤ Final decision | Portfolio manager | Final decision list (BUY/SELL/HOLD + weight + confidence) |

### 2.2 Code Implementation

- **Native workflow orchestration**: the pipeline is a DSH-native workflow script (pipeline / parallel / agent); the four base-research tracks run in parallel per symbol, while debates and adjudications proceed stage by stage;
- **Frozen schemas + composition-time validation**: all 13 role output schemas are frozen as shared constants and validated with JSON Schema when the plugin loads—structural errors surface before launch;
- **Mandatory evidence citations**: every conclusion must cite evidence (minimum one citation); under-cited candidates are dropped;
- **Quality floor**: if base-research success falls below 80%, the whole cycle fail-stops; under-researched holdings are forced to HOLD and non-holdings are excluded;
- **Role memory**: every role keeps structured memory (default 6 entries per role) and accumulates per-stock knowledge across cycles;
- **Cost control**: downstream roles receive compressed evidence summaries (default 16,000 characters); full evidence is persisted and traceable.

### 2.3 Business-Value Assessment

- **Bias counterweight**: a mandatory bear enters the debate, offsetting the single-model buy bias;
- **Hallucination defense**: "conclusions must carry evidence" leaves fabrication nowhere to hide;
- **Restraint when needed**: below 0.65 confidence the system does not act, and under-researched holdings are forced to HOLD—"when in doubt, don't" becomes code;
- **Full transparency**: the Analysis Workflow page shows real stages, agent progress, and evidence counts;
- **Methodology spillover**: every use is a live demonstration of an institutional research process.

## 3. Decision and Risk Control: Why It Can Only Trade This Way

### 3.1 Three-Tier Strategy Mandate

Conservative / neutral / aggressive tiers, each defining hard boundaries for total position, cash reserve, per-stock cap, per-order cap, minimum confidence, turnover cap, daily trade count, drawdown reduction, and maximum drawdown (defaults: 32% per stock, 38% per order, 35% per-cycle turnover, 0.65 confidence, at most 40 decisions / 10 orders per cycle). Boundary values always come from code—editing files cannot change them.

### 3.2 Execution Logic

- **Position capping**: target weight = min(market per-stock cap, strategy per-stock cap)—A-share market rules cap a single stock at 10%, so even a strategy allowing 32% executes at 10%;
- **Portfolio-level constraints**: per-order value, per-cycle turnover, cash reserve, and target total exposure all apply; available cash is further bounded by "target exposure − current holdings value";
- **Drawdown circuit breaker**: hitting the maximum drawdown line (A-shares -18%) force-liquidates all holdings and rejects every new buy;
- **Protective decisions outrank the AI**: hard stop-loss (A-shares -7%), trailing stop (-4% from the high-water mark), and two-tier take-profit (+15% sells 30%, +25% sells 40%) are executed by code automatically, always before AI suggestions;
- **Symbol and price double-checking**: trading is limited to the allowed pool (holdings ∪ latest screening ∪ configured default symbols); execution prices are fetched by the engine from live quotes, never from AI payloads;
- **Four-market rules built in**: T+1/T+0, lot sizes, price limits, commission, stamp tax, and slippage are all applied to paper execution per each market's real rules.

### 3.3 Business-Value Assessment

- **Discipline built in**: stop-loss and take-profit are code, not willpower; the drawdown circuit breaker does not negotiate;
- **Risk-profile fit**: three tiers match different risk preferences, and boundaries cannot be casually broken by the AI or the user;
- **Real cost feel**: commission, stamp tax, slippage, and T+1 all enter simulated execution, so trained strategies account for friction by default.

## 4. Summary

The value of this product is not "AI makes you more money" but "a set of explainable, disciplined, auditable investment decision processes turned into software anyone can run"—screening has reasons, conclusions have evidence, and trading has boundaries.

> This project supports research and paper trading only. It does not connect to a live broker and does not constitute investment advice.
