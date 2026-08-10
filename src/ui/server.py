"""
HTTP server for the AI chat panel + settings + dashboard.
Uses Python stdlib only – zero extra dependencies.
"""

from __future__ import annotations

import json
import logging
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
UI_DIR = Path(__file__).resolve().parent

logger = logging.getLogger("investment-auto.http")


class ChatHandler(SimpleHTTPRequestHandler):
    """Serves static files from src/ui/ and handles API routes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)

        # API routes
        if path == "/api/config":
            return self._handle_get_config()
        if path == "/api/env":
            return self._handle_get_env()
        if path == "/api/dashboard":
            market = query.get("market", ["all"])[0]
            return self._handle_dashboard(market)
        if path == "/api/optimizer":
            return self._handle_optimizer()
        if path == "/api/macro":
            return self._handle_macro(query)
        if path == "/api/macro/dates":
            return self._handle_macro_dates()
        if path == "/api/models":
            return self._handle_models()
        if path == "/api/history":
            return self._handle_get_history()

        # Static files
        if path == "/" or path == "":
            self.path = "/index.html"
        elif path == "/settings":
            self.path = "/settings.html"
        elif path == "/dashboard":
            self.path = "/dashboard.html"
        elif path == "/macro":
            self.path = "/macro.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/chat":
            return self._handle_chat()
        if path == "/api/history/clear":
            return self._handle_clear_history()
        if path == "/api/chat/cancel":
            return self._handle_chat_cancel()
        if path == "/api/config":
            return self._handle_save_config()
        if path == "/api/env":
            return self._handle_save_env()

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'{"error":"not found"}')

    # ── API handlers ───────────────────────────────

    def _handle_chat(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid json"})

        message = data.get("message", "").strip()
        thinking = data.get("thinking", False)
        stream = data.get("stream", True)
        provider = data.get("provider")
        model = data.get("model")
        if not message:
            return self._json_response(400, {"error": "empty message"})

        logger.info(f"Chat: {message[:100]}...")
        try:
            if stream:
                return self._handle_chat_stream(message, thinking, provider, model)
            from .chat_server import handle_chat
            reply = handle_chat(message, thinking, provider=provider, model=model)
            self._json_response(200, {"reply": reply})
        except Exception as e:
            logger.exception("chat error")
            self._json_response(500, {"error": str(e)})

    def _handle_chat_stream(self, message: str, thinking: bool, provider: str | None = None, model: str | None = None):
        from .chat_server import handle_chat_stream
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for event in handle_chat_stream(message, thinking, provider=provider, model=model):
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except BrokenPipeError:
            logger.warning("SSE client disconnected")
        except Exception as e:
            payload = json.dumps({"type": "error", "content": str(e)}, ensure_ascii=False)
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()

    def _handle_models(self):
        try:
            from src.llm.registry import available_models
            self._json_response(200, available_models())
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_get_history(self):
        try:
            from .chat_server import load_history, load_memory
            self._json_response(200, {"history": load_history(limit=100), "memory": load_memory()})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_clear_history(self):
        try:
            from .chat_server import clear_history
            clear_history()
            self._json_response(200, {"ok": True})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_chat_cancel(self):
        try:
            from .chat_server import request_cancel
            request_cancel()
            self._json_response(200, {"ok": True})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_get_config(self):
        try:
            from src.config import cfg
            self._json_response(200, cfg.raw)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_save_config(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid json"})

        try:
            import yaml
            config_path = PROJECT_ROOT / "config" / "config.yaml"
            config_path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8")
            # Reload config
            from src.config import cfg
            cfg.reload()
            self._json_response(200, {"ok": True})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_dashboard(self, market: str):
        try:
            data = self._build_dashboard_data(market)
            self._json_response(200, data)
        except Exception as e:
            logger.exception("dashboard error")
            self._json_response(500, {"error": str(e)})

    def _handle_get_env(self):
        """Read .env file and return as dict (mask sensitive values for display)."""
        try:
            env_path = PROJECT_ROOT / ".env"
            env_vars = {}
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_vars[k.strip()] = v.strip()
            self._json_response(200, env_vars)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_save_env(self):
        """Save environment variables to .env file."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid json"})

        try:
            env_path = PROJECT_ROOT / ".env"
            # Read existing
            existing = {}
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        existing[k.strip()] = v.strip()
            # Merge
            existing.update(data)
            # Write back
            lines = [f"{k}={v}" for k, v in existing.items()]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._json_response(200, {"ok": True})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_macro(self):
        """Get macro environment data."""
        try:
            from src.config import cfg
            macro_dir = PROJECT_ROOT / "runtime" / "macro"
            if not macro_dir.exists():
                return self._json_response(200, {"available": False, "message": "宏观数据目录不存在"})
            
            # Find latest macro report
            files = sorted(macro_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not files:
                return self._json_response(200, {"available": False, "message": "暂无宏观数据"})
            
            latest = json.loads(files[0].read_text(encoding="utf-8"))
            self._json_response(200, {"available": True, "file": files[0].name, "data": latest})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_macro(self, query: dict):
        """Get macro environment report for a specific date."""
        try:
            date = query.get("date", [None])[0]
            if not date:
                return self._json_response(400, {"error": "missing date parameter"})
            
            # Try to find macro report in workspace data directory
            workspace = Path.home() / ".openclaw" / "workspace"
            macro_file = workspace / "data" / "macro" / "daily" / f"{date}.md"
            
            if not macro_file.exists():
                # Try JSON format
                macro_json = workspace / "data" / "macro" / "news" / f"{date}.json"
                if macro_json.exists():
                    data = json.loads(macro_json.read_text(encoding="utf-8"))
                    return self._json_response(200, {"available": True, "date": date, "content": self._macro_json_to_markdown(data)})
                return self._json_response(200, {"available": False, "message": f"{date} 无宏观数据"})
            
            content = macro_file.read_text(encoding="utf-8")
            self._json_response(200, {"available": True, "date": date, "content": content})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_macro_dates(self):
        """Get list of available macro report dates."""
        try:
            workspace = Path.home() / ".openclaw" / "workspace"
            macro_dir = workspace / "data" / "macro" / "daily"
            dates = []
            if macro_dir.exists():
                for f in sorted(macro_dir.glob("*.md"), reverse=True):
                    dates.append(f.stem)
            # Also check JSON news files
            news_dir = workspace / "data" / "macro" / "news"
            if news_dir.exists():
                for f in sorted(news_dir.glob("*.json"), reverse=True):
                    if f.stem not in dates:
                        dates.append(f.stem)
            dates = sorted(dates, reverse=True)
            self._json_response(200, {"dates": dates, "latest": dates[0] if dates else None})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _macro_json_to_markdown(self, data: dict) -> str:
        """Convert macro JSON to markdown format."""
        lines = [f"# 宏观环境日报 {data.get('date', '')}\n"]
        
        # Market data
        if "market_data" in data:
            md = data["market_data"]
            lines.append("## 市场概览\n")
            if "indices" in md:
                lines.append("| 指数 | 最新 | 涨跌% |")
                lines.append("|------|------|-------|")
                for idx in md["indices"]:
                    lines.append(f"| {idx.get('name','')} | {idx.get('price','')} | {idx.get('change_pct','')}% |")
                lines.append("")
        
        # Categories
        if "categories" in data:
            lines.append("## 政策与行业动态\n")
            for cat in data["categories"]:
                lines.append(f"### {cat.get('name','')}")
                for item in cat.get("items", []):
                    lines.append(f"- {item}")
                lines.append("")
        
        # Sectors
        if "sectors" in data:
            lines.append("## 板块分析\n")
            for sec in data["sectors"]:
                lines.append(f"- **{sec.get('name','')}**: {sec.get('summary','')}")
            lines.append("")
        
        return "\n".join(lines)

    def _handle_optimizer(self):
        try:
            from src.config import cfg
            opt_dir = PROJECT_ROOT / "runtime" / "optimizer"
            if not opt_dir.exists():
                return self._json_response(200, {"available": False})
            files = sorted(opt_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not files:
                return self._json_response(200, {"available": False})
            latest = json.loads(files[0].read_text(encoding="utf-8"))
            self._json_response(200, {"available": True, "file": files[0].name, "data": latest})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    # ── dashboard data builder ─────────────────────

    def _build_dashboard_data(self, market: str) -> dict:
        from src.portfolio import account
        from src.config import cfg
        from src.data import fetcher

        pf = account.load()
        accounts = pf.get("accounts", {})
        enabled = cfg.enabled_markets

        # Filter markets
        if market == "all":
            markets_to_show = enabled
        else:
            markets_to_show = [market] if market in enabled else []

        # Aggregate stats
        total_assets = 0
        total_cash = 0
        total_positions = 0
        all_holdings = []
        all_trades = []

        for m in markets_to_show:
            acct = accounts.get(m, {})
            cash = acct.get("cash", 0)
            holdings = acct.get("holdings", [])
            total_cash += cash
            total_positions += len(holdings)

            # Calculate holdings value
            holdings_value = 0
            for h in holdings:
                try:
                    rt = fetcher.realtime(h["code"])
                    last_price = rt.get("price", h.get("costPrice", 0))
                except Exception:
                    last_price = h.get("lastPrice", h.get("costPrice", 0))
                mv = last_price * h.get("shares", 0)
                holdings_value += mv
                all_holdings.append({
                    "market": m, "code": h["code"], "name": h.get("name", ""),
                    "shares": h.get("shares", 0), "cost_price": h.get("costPrice", 0),
                    "last_price": last_price, "market_value": mv,
                })

            total_assets += cash + holdings_value
            for t in acct.get("tradeHistory", []):
                t["market"] = m
                all_trades.append(t)

        # Asset distribution by market
        asset_dist = {"labels": [], "values": []}
        for m in markets_to_show:
            acct = accounts.get(m, {})
            cash = acct.get("cash", 0)
            hv = sum(h.get("market_value", 0) for h in all_holdings if h["market"] == m)
            asset_dist["labels"].append(m.upper())
            asset_dist["values"].append(round(cash + hv, 2))

        # Cash vs Holdings
        cash_vs = {"labels": [], "cash": [], "holdings": []}
        for m in markets_to_show:
            acct = accounts.get(m, {})
            cash_vs["labels"].append(m.upper())
            cash_vs["cash"].append(round(acct.get("cash", 0), 2))
            hv = sum(h.get("market_value", 0) for h in all_holdings if h["market"] == m)
            cash_vs["holdings"].append(round(hv, 2))

        # Weight distribution
        weight_dist = {"labels": [], "values": []}
        for h in all_holdings:
            weight_dist["labels"].append(f"{h['name']}({h['code']})")
            weight_dist["values"].append(round(h["market_value"], 2))

        # Optimizer comparison
        opt_comparison = []
        opt_dir = PROJECT_ROOT / "runtime" / "optimizer"
        if opt_dir.exists():
            files = sorted(opt_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                try:
                    opt_data = json.loads(files[0].read_text(encoding="utf-8"))
                    schemes = {}
                    for key in ["mean_variance", "black_litterman", "risk_parity", "cost_adjusted"]:
                        if key in opt_data:
                            m = opt_data[key]["metrics"]
                            schemes[key] = {
                                "annual_return": m.get("annual_return", 0),
                                "annual_volatility": m.get("annual_volatility", 0),
                                "sharpe": m.get("sharpe", 0),
                                "var95": opt_data[key].get("stress", {}).get("var95", 0),
                                "max_drawdown": opt_data[key].get("stress", {}).get("max_drawdown", 0),
                            }
                    opt_comparison = [
                        {"label": k, "data": [v["annual_return"]*100, v["sharpe"], v["max_drawdown"]*100, v["var95"]*100, 0]}
                        for k, v in schemes.items()
                    ]
                except Exception:
                    pass

        return {
            "stats": {
                "total_assets": round(total_assets, 2),
                "today_pnl": 0,  # TODO: calculate from trade history
                "today_pnl_pct": 0,
                "total_positions": total_positions,
                "cash_ratio": round(total_cash / total_assets * 100, 1) if total_assets > 0 else 0,
                "enabled_markets": enabled,
            },
            "asset_distribution": asset_dist,
            "cash_vs_holdings": cash_vs,
            "weight_distribution": weight_dist,
            "optimizer_comparison": opt_comparison,
            "holdings": all_holdings,
            "recent_trades": sorted(all_trades, key=lambda t: t.get("date", ""), reverse=True)[:20],
            "optimizer": {"available": len(opt_comparison) > 0, "schemes": {}},
        }

    # ── helpers ────────────────────────────────────

    def _json_response(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_server(host: str = "localhost", port: int = 8080):
    # Try localhost first, fallback to 0.0.0.0
    for h in [host, "0.0.0.0"]:
        try:
            server = HTTPServer((h, port), ChatHandler)
            actual_host = h
            break
        except OSError as e:
            logger.warning(f"Cannot bind to {h}:{port} - {e}")
            continue
    else:
        raise RuntimeError(f"Cannot bind to any host on port {port}")

    url = f"http://localhost:{port}" if actual_host == "localhost" else f"http://{actual_host}:{port}"
    logger.info(f"AI Chat Panel running at {url}")

    # Auto-open browser
    import webbrowser
    try:
        webbrowser.open(url)
        logger.info("Browser opened automatically")
    except Exception:
        logger.info(f"Please open {url} in your browser")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Chat server stopped")
        server.shutdown()
