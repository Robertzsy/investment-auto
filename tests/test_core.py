"""Smoke tests for investment-auto core modules."""
from __future__ import annotations

import random
import sys

import pytest

# ensure project root is importable
sys.path.insert(0, ".")


def test_config_loads():
    from src.config import cfg
    assert len(cfg.enabled_markets) >= 1
    assert cfg.llm_primary_provider in ("deepseek", "openai", "glm", "kimi", "")


def test_optimizer_max_sharpe():
    from src.optimizer.engine import max_sharpe, _cov_matrix
    random.seed(1)
    n = 3
    aligned = [[random.gauss(0.0005, 0.015) for _ in range(80)] for _ in range(n)]
    mus = [sum(x) / len(x) for x in aligned]
    cov = _cov_matrix(aligned)
    w, m = max_sharpe(mus, cov, 0.02, 0.5, 1000)
    assert len(w) == n
    assert abs(sum(w) - 1.0) < 0.01
    assert "sharpe" in m


def test_optimizer_risk_parity():
    from src.optimizer.engine import risk_parity, _cov_matrix
    random.seed(2)
    n = 4
    aligned = [[random.gauss(0.0003, 0.02) for _ in range(60)] for _ in range(n)]
    mus = [sum(x) / len(x) for x in aligned]
    cov = _cov_matrix(aligned)
    w, rc = risk_parity(cov, 0.4, 800)
    assert len(w) == n
    assert abs(sum(w) - 1.0) < 0.01
    # risk contributions should be roughly equal
    assert max(rc) - min(rc) < 0.2


def test_stress_test():
    from src.optimizer.engine import stress_test, _cov_matrix, max_sharpe
    random.seed(3)
    n = 5
    aligned = [[random.gauss(0.0002, 0.018) for _ in range(70)] for _ in range(n)]
    mus = [sum(x) / len(x) for x in aligned]
    cov = _cov_matrix(aligned)
    w, _ = max_sharpe(mus, cov, 0.02, 0.3, 800)
    s = stress_test(w, aligned)
    assert "var95" in s
    assert "max_drawdown" in s
    assert s["var95"] <= 0  # should be negative


def test_account_init():
    from src.portfolio import account
    # use in-memory style test
    acct = account.load()
    assert acct["version"] == 2
    assert acct["multiMarket"] is True


def test_market_config():
    from src.config import cfg
    cfg_cn = cfg.market_config("cn")
    assert cfg_cn["market"] == "cn"
    assert "risk" in cfg_cn
    assert cfg_cn["trading"]["settlement"] == "T+1"
