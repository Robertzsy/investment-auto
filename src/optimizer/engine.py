from __future__ import annotations

import math
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EPS = 1e-12
TRADING_DAYS = 252

# ─── stat helpers ─────────────────────────────
def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0

def _std(xs: List[float]) -> float:
    if len(xs) < 2: return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x-m)*(x-m)/(len(xs)-1) for x in xs))

def _cov(x: List[float], y: List[float]) -> float:
    n = min(len(x), len(y))
    if n < 2: return 0.0
    mx, my = _mean(x[-n:]), _mean(y[-n:])
    return sum((x[-n:][i]-mx)*(y[-n:][i]-my) for i in range(n))/(n-1)

def _cov_matrix(aligned: List[List[float]]) -> List[List[float]]:
    n = len(aligned)
    return [[_cov(aligned[i], aligned[j]) for j in range(n)] for i in range(n)]

# ─── metrics ───────────────────────────────
def _port_ret(w: List[float], mus: List[float]) -> float:
    return sum(wi*mu for wi, mu in zip(w, mus))

def _port_var(w: List[float], cov: List[List[float]]) -> float:
    return max(0, sum(w[i]*w[j]*cov[i][j] for i in range(len(w)) for j in range(len(w))))

def _port_vol(w: List[float], cov: List[List[float]]) -> float:
    return math.sqrt(_port_var(w, cov))

def _metrics(w: List[float], mus: List[float], cov: List[List[float]], rf: float) -> Dict:
    rd = _port_ret(w, mus)
    vd = _port_vol(w, cov)
    ra = rd * TRADING_DAYS
    va = vd * math.sqrt(TRADING_DAYS)
    sh = (ra - rf) / va if va > EPS else 0.0
    return {"annual_return": ra, "annual_volatility": va, "sharpe": sh, "daily_return": rd, "daily_volatility": vd}

def _normalize(w: List[float]) -> List[float]:
    s = sum(max(0, wi) for wi in w)
    if s <= EPS: return [1/len(w)]*len(w) if w else []
    return [max(0, wi)/s for wi in w]

def _cap_norm(w: List[float], cap: float, its: int = 20) -> List[float]:
    w = _normalize(w)
    for _ in range(its):
        excess = sum(max(0, x-cap) for x in w)
        w = [min(x, cap) for x in w]
        room = sum(max(0, cap-x) for x in w)
        if excess <= EPS or room <= EPS: break
        w = [x + excess*r/room for x, r in zip(w, [max(0, cap-x) for x in w])]
    return _normalize(w)

def _rand_weights(n: int, cap: float) -> List[float]:
    return _cap_norm([random.expovariate(1.0) for _ in range(n)], cap)

# ── max sharpe sampler ──────────────────────
def max_sharpe(mus: List[float], cov: List[List[float]], rf: float, max_weight: float, samples: int = 6000) -> Tuple[List[float], Dict]:
    n = len(mus)
    if n == 0: return [], {}
    pool = [_cap_norm([1/n]*n, max_weight)]
    vols = [math.sqrt(max(cov[i][i], EPS)) for i in range(n)]
    pool.append(_cap_norm([1/v for v in vols], max_weight))
    pool.append(_cap_norm([max(m, 0)/max(cov[i][i], EPS) for i, m in enumerate(mus)], max_weight))
    pool += [_rand_weights(n, max_weight) for _ in range(samples)]
    best_w, best_m = pool[0], _metrics(pool[0], mus, cov, rf)
    for w in pool[1:]:
        m = _metrics(w, mus, cov, rf)
        if m["sharpe"] > best_m["sharpe"]:
            best_w, best_m = w, m
    return best_w, best_m

# ── risk parity ───────────────────────────────
def _risk_contrib(w: List[float], cov: List[List[float]]) -> List[float]:
    pv = _port_var(w, cov)
    if pv <= EPS: return [0]*len(w)
    mrc = [sum(cov[i][j]*w[j] for j in range(len(w))) for i in range(len(w))]
    return [w[i]*mrc[i]/pv for i in range(len(w))]

def risk_parity(cov: List[List[float]], max_weight: float, its: int = 2500) -> Tuple[List[float], List[float]]:
    n = len(cov)
    if n == 0: return [], []
    w = _cap_norm([1/n]*n, max_weight)
    tgt = 1/n
    for _ in range(its):
        rc = _risk_contrib(w, cov)
        w = _cap_norm([wi * math.exp(-0.05*(rci-tgt)) for wi, rci in zip(w, rc)], max_weight)
    return w, _risk_contrib(w, cov)

# ── stress test ────────────────────────────────
def _mdd(rs: List[float]) -> float:
    wlth = 1.0; peak = 1.0; dd = 0.0
    for r in rs:
        wlth *= (1+r); peak = max(peak, wlth); dd = min(dd, wlth/peak - 1)
    return dd

def stress_test(w: List[float], aligned: List[List[float]]) -> Dict:
    if not aligned: return {}
    pr = [sum(w[i]*aligned[i][t] for i in range(len(w))) for t in range(len(aligned[0]))]
    worst = min(pr) if pr else 0
    sorted_ret = sorted(pr)
    var95 = sorted_ret[max(0, int(len(pr)*0.05))]
    es95 = _mean([r for r in pr if r <= var95])
    # weekly/monthly worst
    wk = [math.prod(1+r for r in pr[i:i+5])-1 for i in range(max(0, len(pr)-4))]
    mo = [math.prod(1+r for r in pr[i:i+20])-1 for i in range(max(0, len(pr)-19))]
    return {
        "var95": var95, "expected_shortfall_95": es95,
        "max_drawdown": _mdd(pr),
        "worst_day": worst, "worst_week": min(wk) if wk else worst, "worst_month": min(mo) if mo else worst,
        "one_sigma": -_std(pr), "two_sigma": -2*_std(pr),
    }

# ── cost-aware ────────────────────────────────
def rebalance_cost(w0: List[float], w1: List[float], buy_rate: float, sell_rate: float) -> Dict:
    b = sum(max(0, t-c) for c, t in zip(w0, w1))
    s = sum(max(0, c-t) for c, t in zip(w0, w1))
    return {"turnover_buy": b, "turnover_sell": s, "turnover_total": b+s, "cost_ratio": b*buy_rate + s*sell_rate}

def max_sharpe_cost_aware(mus, cov, rf, max_w, w0, buy_rate, sell_rate, samples=6000):
    n = len(mus)
    pool = [_cap_norm([1/n]*n, max_w)] + [_rand_weights(n, max_w) for _ in range(samples)]
    best_w, best_m = pool[0], None
    best_sharpe_net = -1e9
    for w in pool:
        m = _metrics(w, mus, cov, rf)
        cost = rebalance_cost(w0, w, buy_rate, sell_rate)["cost_ratio"]
        net_ret = m["annual_return"] - cost
        net_sh = (net_ret - rf)/m["annual_volatility"] if m["annual_volatility"]>EPS else 0
        if net_sh > best_sharpe_net:
            best_w, best_m = w, m
            best_sharpe_net = net_sh
    return best_w, best_m, best_sharpe_net
