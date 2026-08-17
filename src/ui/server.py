"""
HTTP server for the AI chat panel + settings + dashboard.
Uses Python stdlib only – zero extra dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
from src.paths import config_dir, data_root, runtime_dir
UI_DIR = Path(__file__).resolve().parent

logger = logging.getLogger("investment-auto.http")

ENV_VALUE_MASK = "********"
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MARKET_CONFIG_FILES = {
    "cn": "cn.yaml",
    "hk": "hk.yaml",
    "us": "us.yaml",
    "etf": "etf.yaml",
}
_EDITABLE_RISK_LIMITS = {
    "single_stock_max_pct": (1.0, 100.0),
    "min_cash_reserve_pct": (0.0, 100.0),
    "hard_stop_pct": (-100.0, 0.0),
    "trailing_stop_pct": (-100.0, 0.0),
    "take_profit_1_pct": (0.0, 1000.0),
    "take_profit_1_sell_ratio": (0.0, 1.0),
    "take_profit_2_pct": (0.0, 1000.0),
    "take_profit_2_sell_ratio": (0.0, 1.0),
    "max_drawdown_pct": (-100.0, 0.0),
}
_SENSITIVE_ENV_MARKERS = (
    "_KEY",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "AUTH_TOKEN",
    "CREDENTIAL",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
    "WEBHOOK",
    "MONGODB_URI",
)

# Role -> model tier mapping used by the first-run wizard. Analysts and
# researchers do cheap high-volume work (quick model); managers, judges and
# risk roles make decisions (deep model). Unknown roles default to deep.
_QUICK_ROLES = {
    "aggressive_analyst", "analyst", "bear_researcher", "bull_researcher",
    "conservative_analyst", "fundamentals_analyst", "neutral_analyst",
    "news_analyst", "researcher", "sentiment_analyst",
    "technical_analyst", "trader",
}
_DEEP_ROLES = {
    "investment_advisor", "judge", "portfolio_manager", "quant_analyst",
    "research_manager", "risk_chairman", "risk_manager",
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base; lists/scalars are replaced."""
    merged = dict(base or {})
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _write_config_merged(patch: dict) -> dict:
    """Apply a patch to config.yaml via deep merge (never clobbers other
    sections such as llm/schedule/autonomous/trading) and reload."""
    import yaml

    from src.config import cfg

    config_path = config_dir() / "config.yaml"
    existing: dict = {}
    if config_path.exists():
        try:
            existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            existing = {}
    merged = _deep_merge(existing, patch)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )
    temporary.replace(config_path)
    cfg.reload()
    return merged


def _is_sensitive_env_key(key: str) -> bool:
    normalized = key.upper()
    return any(marker in normalized for marker in _SENSITIVE_ENV_MARKERS)


def _optimizer_files(directory: Path, market: str = "all") -> list[Path]:
    if not directory.exists():
        return []
    pattern = "*.json" if market in {"", "all"} else f"*-{market.lower()}.json"
    return sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)


ACCESS_TOKEN = os.getenv("IA_ACCESS_TOKEN", "").strip()


class ChatHandler(SimpleHTTPRequestHandler):
    """Serves static files from src/ui/ and handles API routes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def _authorized(self) -> bool:
        if not ACCESS_TOKEN:
            return True
        query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        if query_token == ACCESS_TOKEN:
            return True
        header = self.headers.get("X-IA-Token", "")
        authorization = self.headers.get("Authorization", "")
        if header == ACCESS_TOKEN or authorization in (ACCESS_TOKEN, "Bearer " + ACCESS_TOKEN):
            return True
        return False

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')

    def do_GET(self):
        if not self._authorized():
            return self._unauthorized()
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)

        # API routes
        if path == "/api/config":
            return self._handle_get_config()
        if path == "/api/market-configs":
            return self._handle_get_market_configs()
        if path == "/api/env":
            return self._handle_get_env()
        if path == "/api/dashboard":
            market = query.get("market", ["all"])[0]
            return self._handle_dashboard(market)
        if path == "/api/optimizer":
            market = query.get("market", ["all"])[0]
            return self._handle_optimizer(market)
        if path == "/api/macro":
            return self._handle_macro(query)
        if path == "/api/macro/dates":
            return self._handle_macro_dates()
        if path == "/api/models":
            return self._handle_models()
        if path == "/api/history":
            return self._handle_get_history()
        if path == "/api/autonomy":
            return self._handle_autonomy_status()
        if path == "/api/investment/mandate":
            return self._handle_investment_mandate()
        if path == "/api/secrets":
            return self._handle_get_secrets()
        if path == "/api/migrate":
            return self._handle_migrate_detect()

        # Static files
        if path == "/" or path == "":
            self.path = "/index.html"
        elif path == "/settings":
            self.path = "/settings.html"
        elif path == "/dashboard":
            self.path = "/dashboard.html"
        elif path == "/macro":
            self.path = "/macro.html"
        elif path == "/setup":
            self.path = "/setup.html"
        return super().do_GET()

    def do_POST(self):
        if not self._authorized():
            return self._unauthorized()
        path = urlparse(self.path).path

        if path == "/api/models/test":
            return self._handle_models_test()
        if path == "/api/chat":
            return self._handle_chat()
        if path == "/api/investment-cycle":
            return self._handle_investment_cycle()
        if path == "/api/investment/mandate":
            return self._handle_set_investment_mandate()
        if path == "/api/history/clear":
            return self._handle_clear_history()
        if path == "/api/chat/cancel":
            return self._handle_chat_cancel()
        if path == "/api/config":
            return self._handle_save_config()
        if path == "/api/market-configs":
            return self._handle_save_market_configs()
        if path == "/api/env":
            return self._handle_save_env()
        if path == "/api/secrets":
            return self._handle_save_secrets()
        if path == "/api/migrate":
            return self._handle_migrate_run()
        if path == "/api/setup/models":
            return self._handle_setup_models()
        if path == "/api/setup/mode":
            return self._handle_setup_mode()
        if path == "/api/setup/mandate":
            return self._handle_setup_mandate()
        if path == "/api/setup/init":
            return self._handle_setup_init()
        if path == "/api/setup/complete":
            return self._handle_setup_complete()
        if path.startswith("/api/autonomy/"):
            return self._handle_autonomy_control(path.rsplit("/", 1)[-1])

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
        if not isinstance(data, dict):
            return self._json_response(400, {"error": "chat payload must be a JSON object"})

        message = str(data.get("message", "")).strip()
        thinking = bool(data.get("thinking", False))
        stream = bool(data.get("stream", True))
        provider = data.get("provider")
        model = data.get("model")
        request_id = data.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
                return self._json_response(400, {"error": "invalid request_id"})
        if not message:
            return self._json_response(400, {"error": "empty message"})

        logger.info(f"Chat: {message[:100]}...")
        try:
            if stream:
                return self._handle_chat_stream(message, thinking, provider, model, request_id)
            from .chat_server import handle_chat
            reply = handle_chat(message, thinking, provider=provider, model=model, request_id=request_id)
            self._json_response(200, {"reply": reply})
        except Exception as e:
            logger.exception("chat error")
            self._json_response(500, {"error": str(e)})

    def _handle_chat_stream(self, message: str, thinking: bool, provider: str | None = None, model: str | None = None, request_id: str | None = None):
        from .chat_server import handle_chat_stream
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for event in handle_chat_stream(message, thinking, provider=provider, model=model, request_id=request_id):
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if event.get("type") in {"final", "cancelled", "error"}:
                    break
        except BrokenPipeError:
            logger.warning("SSE client disconnected")
        except Exception as e:
            try:
                payload = json.dumps({"type": "error", "content": str(e)}, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                logger.warning("SSE client disconnected while sending error")
        finally:
            self.close_connection = True

    def _handle_investment_cycle(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid json"})
        if not isinstance(data, dict):
            return self._json_response(400, {"error": "payload must be a JSON object"})
        market = str(data.get("market", "")).strip().lower()
        if market not in {"cn", "hk", "us", "etf"}:
            return self._json_response(400, {"error": "market must be cn, hk, us, or etf"})
        request_id = data.get("request_id")
        if request_id is not None and (
            not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id)
        ):
            return self._json_response(400, {"error": "invalid request_id"})

        from .chat_server import handle_investment_cycle_stream

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for event in handle_investment_cycle_stream(market, request_id=request_id):
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if event.get("type") in {"final", "cancelled", "error"}:
                    break
        except BrokenPipeError:
            logger.warning("Investment-cycle SSE client disconnected")
        except Exception as exc:
            logger.exception("investment cycle stream error")
            try:
                payload = json.dumps({"type": "error", "content": str(exc)}, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                pass
        finally:
            self.close_connection = True

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
            request_id = parse_qs(urlparse(self.path).query).get("request_id", [None])[0]
            length = int(self.headers.get("Content-Length", 0))
            if length:
                try:
                    data = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    return self._json_response(400, {"error": "invalid json"})
                if not isinstance(data, dict):
                    return self._json_response(400, {"error": "cancel payload must be a JSON object"})
                request_id = data.get("request_id", request_id)
            if request_id is not None:
                if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
                    return self._json_response(400, {"error": "invalid request_id"})
            from .chat_server import request_cancel
            cancelled = request_cancel(request_id)
            self._json_response(200, {"ok": True, "cancelled": cancelled})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_get_config(self):
        try:
            from src.config import cfg
            self._json_response(200, cfg.raw)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_get_market_configs(self):
        """Return editable risk controls and read-only trading mechanics by market."""
        try:
            import yaml

            payload = {}
            base = config_dir() / "market"
            for market, filename in _MARKET_CONFIG_FILES.items():
                data = yaml.safe_load((base / filename).read_text(encoding="utf-8")) or {}
                risk = data.get("risk", {})
                trading = data.get("trading", {})
                payload[market] = {
                    "name": data.get("name", market.upper()),
                    "risk": {
                        key: risk.get(key)
                        for key in _EDITABLE_RISK_LIMITS
                    },
                    "rules": {
                        "settlement": trading.get("settlement"),
                        "lot_size": trading.get("lot_size"),
                        "commission_rate": trading.get("commission_rate"),
                        "stamp_tax": trading.get("stamp_tax"),
                        "slippage": trading.get("slippage"),
                    },
                }
            self._json_response(200, payload)
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_save_market_configs(self):
        """Update only the risk controls intentionally exposed by Settings."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid json"})
        if not isinstance(data, dict):
            return self._json_response(400, {"error": "market configs must be a JSON object"})

        normalized = {}
        for market, market_data in data.items():
            if market not in _MARKET_CONFIG_FILES:
                return self._json_response(400, {"error": f"unsupported market: {market}"})
            if not isinstance(market_data, dict) or not isinstance(market_data.get("risk"), dict):
                return self._json_response(400, {"error": f"{market}.risk must be a JSON object"})
            risk = market_data["risk"]
            unknown = set(risk) - set(_EDITABLE_RISK_LIMITS)
            if unknown:
                return self._json_response(400, {"error": f"unsupported risk field: {sorted(unknown)[0]}"})
            normalized[market] = {}
            for key, value in risk.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return self._json_response(400, {"error": f"{market}.{key} must be numeric"})
                lower, upper = _EDITABLE_RISK_LIMITS[key]
                numeric = float(value)
                if not lower <= numeric <= upper:
                    return self._json_response(
                        400,
                        {"error": f"{market}.{key} must be between {lower:g} and {upper:g}"},
                    )
                normalized[market][key] = numeric

            risk = normalized[market]
            if (
                "take_profit_1_pct" in risk
                and "take_profit_2_pct" in risk
                and risk["take_profit_2_pct"] < risk["take_profit_1_pct"]
            ):
                return self._json_response(
                    400,
                    {"error": f"{market}.take_profit_2_pct must not be below take_profit_1_pct"},
                )

        try:
            import yaml

            base = config_dir() / "market"
            prepared = []
            for market, risk_updates in normalized.items():
                path = base / _MARKET_CONFIG_FILES[market]
                market_config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                market_config.setdefault("risk", {}).update(risk_updates)
                prepared.append((path, market_config))
            for path, market_config in prepared:
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(
                    yaml.safe_dump(market_config, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                temporary.replace(path)
            self._json_response(200, {"ok": True})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_autonomy_status(self):
        try:
            from src.investment.command_bus import InvestmentAgentClient

            self._json_response(200, InvestmentAgentClient().issue("status", requested_by="chat-ui", timeout=30))
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_investment_mandate(self):
        try:
            from src.investment.mandate import STRATEGIES, get_mandate

            self._json_response(200, {
                "mandate": get_mandate(),
                "strategies": {
                    key: {
                        "profile": value.profile,
                        "display_name": value.display_name,
                        "objective": value.objective,
                        "max_total_position_pct": value.max_total_position_pct,
                        "min_cash_reserve_pct": value.min_cash_reserve_pct,
                        "max_position_pct": value.max_position_pct,
                        "min_confidence": value.min_confidence,
                        "max_drawdown_pct": value.max_drawdown_pct,
                    }
                    for key, value in STRATEGIES.items()
                },
            })
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_set_investment_mandate(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid json"})
        if not isinstance(data, dict):
            return self._json_response(400, {"error": "payload must be a JSON object"})
        try:
            from src.investment.command_bus import InvestmentAgentClient

            result = InvestmentAgentClient().issue(
                "set_strategy",
                {"profile": data.get("profile")},
                requested_by="chat-ui",
                timeout=30,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(400, {"error": str(e)})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_autonomy_control(self, action: str):
        length = int(self.headers.get("Content-Length", 0))
        data = {}
        if length:
            try:
                data = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                return self._json_response(400, {"error": "invalid json"})
            if not isinstance(data, dict):
                return self._json_response(400, {"error": "payload must be a JSON object"})
        reason = str(data.get("reason", ""))[:500]
        try:
            if action == "mode":
                mode = str(data.get("mode", "")).lower()
                if mode not in {"manual", "automatic"}:
                    return self._json_response(400, {"error": "mode must be manual or automatic"})
                from src.investment.command_bus import InvestmentAgentClient

                result = InvestmentAgentClient().issue(
                    "set_mode", {"mode": mode}, requested_by="chat-ui", timeout=30,
                )
                return self._json_response(200, result)

            command = {"pause": "pause", "resume": "resume", "kill": "kill", "reset-kill": "reset_kill"}.get(action)
            if command is None:
                return self._json_response(404, {"error": "unknown autonomy action"})
            from src.investment.command_bus import InvestmentAgentClient

            result = InvestmentAgentClient().issue(
                command, {"reason": reason}, requested_by="chat-ui", timeout=30,
            )
            self._json_response(200, result)
        except RuntimeError as e:
            self._json_response(409, {"error": str(e)})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_save_config(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid json"})
        if not isinstance(data, dict):
            return self._json_response(400, {"error": "payload must be a JSON object"})

        try:
            # Deep merge so partial saves (e.g. only markets.enable) never
            # wipe llm/schedule/autonomous/trading sections.
            _write_config_merged(data)
            self._json_response(200, {"ok": True})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_models_test(self):
        """Actually call the provider with the saved key (wizard '保存并测试')."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            return self._json_response(400, {"ok": False, "error": "JSON 无效"})
        if not isinstance(data, dict):
            return self._json_response(400, {"ok": False, "error": "payload 必须是对象"})
        provider = str(data.get("provider", "") or "").strip().lower()
        model = str(data.get("model", "") or "").strip()
        try:
            from src.llm.registry import resolve_llm

            llm = resolve_llm(provider=provider or None, model=model or None)
            reply = llm.chat(
                [{"role": "user", "content": "ping，只回复 pong。"}],
                temperature=0.0,
                max_tokens=8,
            )
            if not reply or not reply.strip():
                return self._json_response(200, {"ok": False, "error": "服务商返回了空响应"})
            self._json_response(200, {"ok": True, "reply": reply[:50]})
        except Exception as exc:
            logger.warning("Model test failed for provider=%s: %s", provider, exc)
            self._json_response(200, {"ok": False, "error": str(exc)[:300]})

    def _handle_setup_models(self):
        """Wizard step 2: persist provider + quick/deep model selection."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            return self._json_response(400, {"ok": False, "error": "JSON 无效"})
        if not isinstance(data, dict):
            return self._json_response(400, {"ok": False, "error": "payload 必须是对象"})
        provider = str(data.get("provider", "") or "").strip().lower()
        quick = str(data.get("quick_model", "") or "").strip()
        deep = str(data.get("deep_model", "") or "").strip()
        try:
            from src.config import cfg

            models = cfg.raw.get("llm", {}).get("models", {})
            variants = models.get(provider, {}).get("variants", []) or []
            if provider not in models:
                return self._json_response(400, {"ok": False, "error": f"未知服务商: {provider}"})
            if quick not in variants or deep not in variants:
                return self._json_response(400, {"ok": False, "error": "模型不在该服务商的可选列表中"})

            role_override: dict = {}
            for role in set(_QUICK_ROLES) | set(_DEEP_ROLES):
                role_override[role] = quick if role in _QUICK_ROLES else deep

            patch = {
                "llm": {
                    "provider": provider,
                    "quick_model": quick,
                    "deep_model": deep,
                    "models": {provider: {"model": deep}},
                    "role_model_override": role_override,
                },
            }
            _write_config_merged(patch)
            self._json_response(200, {"ok": True})
        except Exception as exc:
            logger.warning("setup models save failed: %s", exc)
            self._json_response(500, {"ok": False, "error": str(exc)[:300]})

    def _handle_setup_mode(self):
        """Wizard step 4: persist autonomous mode directly (no agent yet)."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            return self._json_response(400, {"ok": False, "error": "JSON 无效"})
        if not isinstance(data, dict):
            return self._json_response(400, {"ok": False, "error": "payload 必须是对象"})
        mode = str(data.get("mode", "") or "").strip().lower()
        if mode not in {"manual", "automatic"}:
            return self._json_response(400, {"ok": False, "error": "mode 必须是 manual 或 automatic"})
        try:
            from src.investment.service import _save_operation_mode

            result = _save_operation_mode(mode)
            self._json_response(200, {"ok": True, **result})
        except Exception as exc:
            logger.warning("setup mode save failed: %s", exc)
            self._json_response(500, {"ok": False, "error": str(exc)[:300]})

    def _handle_setup_mandate(self):
        """Wizard step 3: persist strategy profile directly (no agent yet)."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            return self._json_response(400, {"ok": False, "error": "JSON 无效"})
        if not isinstance(data, dict):
            return self._json_response(400, {"ok": False, "error": "payload 必须是对象"})
        profile = str(data.get("profile", "") or "").strip().lower()
        try:
            from src.investment.mandate import set_mandate

            payload = set_mandate(profile, selected_by="setup-wizard")
            self._json_response(200, {"ok": True, "profile": payload.get("profile")})
        except ValueError as exc:
            self._json_response(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.warning("setup mandate save failed: %s", exc)
            self._json_response(500, {"ok": False, "error": str(exc)[:300]})

    def _handle_get_secrets(self):
        from src.secret_store import list_keys

        try:
            keys = list_keys()
        except Exception:
            keys = []
        # Never return secret values - only which keys are configured.
        self._json_response(200, {"keys": keys})

    def _handle_save_secrets(self):
        from src.secret_store import save_secret

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"ok": False, "error": "JSON 无效"})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"ok": False, "error": "payload 必须是对象"})
            return
        try:
            for key, value in data.items():
                name = str(key)
                secret = str(value or "")
                save_secret(name, secret)
                # Keep adapters created later in this chat process in sync.
                # The standalone investment process reads the same DPAPI store.
                if secret.strip():
                    os.environ[name] = secret
                else:
                    os.environ.pop(name, None)
        except Exception as exc:
            logger.warning("Secret save failed: %s", exc)
            self._json_response(500, {"ok": False, "error": str(exc)[:300]})
            return
        self._json_response(200, {"ok": True})

    def _handle_migrate_detect(self):
        from src.migration import detect_sources, plan_migration

        sources = detect_sources()
        self._json_response(200, {
            "sources": sources,
            "plan": plan_migration(sources[0]["path"]) if sources else None,
        })

    def _handle_migrate_run(self):
        from src.migration import run_migration

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"ok": False, "error": "JSON 无效"})
            return
        source = str((data or {}).get("source", ""))
        items = (data or {}).get("items") or None
        if not source:
            self._json_response(400, {"ok": False, "error": "source 不能为空"})
            return
        try:
            result = run_migration(source, items)
        except Exception as exc:
            self._json_response(500, {"ok": False, "error": str(exc)[:400]})
            return
        self._json_response(200, {"ok": True, **result})

    def _handle_setup_init(self):
        from src.portfolio import account

        try:
            # account.load() returns the default portfolio when the file is
            # missing, so "already initialized" must be judged by file
            # existence - otherwise the wizard never writes portfolio.json.
            already = account.exists()
            if not already:
                account.save(account.load())
            self._json_response(200, {"ok": True, "created": not already, "initialized": already})
        except Exception as exc:
            self._json_response(500, {"ok": False, "error": str(exc)[:300]})

    def _handle_setup_complete(self):
        from src.paths import runtime_dir

        marker = runtime_dir() / "setup.complete"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")
        self._json_response(200, {"ok": True})

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
            env_path = data_root() / ".env"
            env_vars = {}
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        key = k.strip()
                        env_vars[key] = ENV_VALUE_MASK if _is_sensitive_env_key(key) and v.strip() else v.strip()
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

        if not isinstance(data, dict):
            return self._json_response(400, {"error": "environment variables must be a JSON object"})

        updates = {}
        for key, value in data.items():
            if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
                return self._json_response(400, {"error": f"invalid environment variable name: {key!r}"})
            if not isinstance(value, str):
                return self._json_response(400, {"error": f"environment variable {key} must be a string"})
            if len(value) > 65536 or any(char in value for char in ("\x00", "\r", "\n")):
                return self._json_response(400, {"error": f"invalid value for environment variable {key}"})
            # Blank fields and the display-only mask mean "keep the current value".
            if value and value != ENV_VALUE_MASK:
                updates[key] = value

        try:
            env_path = data_root() / ".env"
            # Read existing
            existing = {}
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        existing[k.strip()] = v.strip()
            # Merge
            existing.update(updates)
            # Write back
            lines = [f"{k}={v}" for k, v in existing.items()]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            env_path.chmod(0o600)
            # dotenv does not refresh an already-running process. Keep adapters
            # created after this request in sync with the newly saved values.
            os.environ.update(updates)
            self._json_response(200, {"ok": True})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_macro(self, query: dict):
        """Get a locally generated macro report for a specific date."""
        try:
            date = query.get("date", [None])[0]
            if not date:
                return self._json_response(400, {"error": "missing date parameter"})

            from src.macro import DATA_ROOT, report_path

            macro_file = report_path(date, DATA_ROOT)
            if not macro_file.exists():
                macro_json = DATA_ROOT / "news" / f"{date}.json"
                if macro_json.exists():
                    data = json.loads(macro_json.read_text(encoding="utf-8"))
                    return self._json_response(200, {"available": True, "date": date, "content": self._macro_json_to_markdown(data)})
                return self._json_response(200, {"available": False, "message": f"{date} 无宏观数据"})

            content = macro_file.read_text(encoding="utf-8")
            self._json_response(200, {"available": True, "date": date, "content": content})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_macro_dates(self):
        """Get dates available in the standalone runtime macro store."""
        try:
            from src.macro import DATA_ROOT, latest_dates

            dates = latest_dates(DATA_ROOT)
            self._json_response(200, {"dates": dates, "latest": dates[0] if dates else None})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _macro_json_to_markdown(self, data: dict) -> str:
        """Convert macro JSON to markdown format."""
        lines = [f"# 市场环境研判 {data.get('date', '')}\n"]
        
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

    def _handle_optimizer(self, market: str = "all"):
        try:
            opt_dir = runtime_dir() / "optimizer"
            files = _optimizer_files(opt_dir, market)
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
                quantity = h.get("shares", h.get("quantity", 0)) or 0
                cost_price = h.get("costPrice", h.get("cost", h.get("price", 0))) or 0
                try:
                    rt = fetcher.realtime(h["code"])
                    last_price = rt.get("price", cost_price)
                    realtime_name = str(rt.get("name", "")).strip()
                except Exception:
                    last_price = h.get("lastPrice", cost_price)
                    realtime_name = ""
                stored_name = str(h.get("name", "")).strip()
                display_name = realtime_name if stored_name.upper() in {"", str(h["code"]).upper()} and realtime_name else stored_name
                mv = last_price * quantity
                holdings_value += mv
                all_holdings.append({
                    "market": m, "code": h["code"], "name": display_name,
                    "shares": quantity, "cost_price": cost_price,
                    "last_price": last_price, "market_value": mv,
                })

            total_assets += cash + holdings_value
            for trade in acct.get("tradeHistory", []):
                item = dict(trade)
                item["market"] = m
                item["date"] = item.get("date", item.get("time", ""))
                item["shares"] = item.get("shares", item.get("quantity", 0))
                all_trades.append(item)

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
        optimizer_schemes = {}
        opt_dir = runtime_dir() / "optimizer"
        if opt_dir.exists():
            files = _optimizer_files(opt_dir, market)
            if files:
                try:
                    opt_data = json.loads(files[0].read_text(encoding="utf-8"))
                    schemes = {}
                    for key in ["mean_variance", "black_litterman", "risk_parity", "cost_adjusted"]:
                        if key in opt_data:
                            m = opt_data[key]["metrics"]
                            net = opt_data[key].get("net_metrics", {})
                            schemes[key] = {
                                "annual_return": net.get("net_annual_return", m.get("annual_return", 0)),
                                "annual_volatility": net.get("annual_volatility", m.get("annual_volatility", 0)),
                                "sharpe": net.get("net_sharpe", m.get("sharpe", 0)),
                                "var95": opt_data[key].get("stress", {}).get("var95", 0),
                                "max_drawdown": opt_data[key].get("stress", {}).get("max_drawdown", 0),
                            }
                    optimizer_schemes = schemes
                    colors = ["#58a6ff", "#a371f7", "#3fb950", "#d29922"]
                    opt_comparison = [
                        {
                            "label": key,
                            "data": [value["annual_return"] * 100, value["sharpe"], value["max_drawdown"] * 100, value["var95"] * 100, 0],
                            "borderColor": colors[index % len(colors)],
                            "backgroundColor": colors[index % len(colors)] + "22",
                        }
                        for index, (key, value) in enumerate(schemes.items())
                    ]
                except Exception:
                    pass

        screening_results = []
        try:
            from src.screening import latest_screening

            for market_name in markets_to_show:
                latest = latest_screening(market_name)
                if latest:
                    screening_results.append({
                        "market": market_name,
                        "generated_at": latest.get("generated_at"),
                        "status": latest.get("status"),
                        "source": latest.get("source"),
                        "candidate_count": latest.get("candidate_count", 0),
                        "scored_count": latest.get("scored_count", 0),
                        "selected": latest.get("selected", []),
                        "discovery_error": latest.get("discovery_error"),
                    })
        except Exception:
            screening_results = []

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
            "optimizer": {"available": bool(optimizer_schemes), "schemes": optimizer_schemes},
            "screening": {"available": bool(screening_results), "markets": screening_results},
        }

    # ── helpers ────────────────────────────────────

    def _json_response(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def _format_url(host: str, port: int) -> str:
    """Render a URL from the actual bound host/port (IPv6 gets brackets).

    Never rewrites the host to "localhost": the desktop WebView2 may resolve
    localhost to IPv6 ::1 first and hang in SYN_SENT when the server only
    bound IPv4 loopback.
    """
    rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{rendered}:{port}"


def _write_ready_file(host: str, port: int, token: str) -> None:
    """Publish the actual bound port and token for the desktop shell."""
    try:
        payload = {
            "host": host,
            "port": int(port),
            "token": token,
            "url": _format_url(host, port),
            "pid": os.getpid(),
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        ready_path = runtime_dir() / "chat.ready.json"
        ready_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = ready_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(ready_path)
    except Exception:
        logger.debug("Could not write chat ready file", exc_info=True)


def start_server(host: str = "localhost", port: int = 8080, open_browser: bool = True, token: str = ""):
    global ACCESS_TOKEN
    ACCESS_TOKEN = (token or os.getenv("IA_ACCESS_TOKEN", "")).strip()
    try:
        server = ThreadingHTTPServer((host, port), ChatHandler)
    except OSError as exc:
        raise RuntimeError(f"Cannot bind to {host}:{port}: {exc}") from exc

    actual_host, actual_port = server.server_address[:2]
    _write_ready_file(actual_host, actual_port, ACCESS_TOKEN)
    url = _format_url(actual_host, actual_port)
    logger.info(f"AI Chat Panel running at {url}")

    # Auto-open browser for interactive local use only.
    if open_browser:
        import webbrowser
        try:
            webbrowser.open(url)
            logger.info("Browser opened automatically")
        except Exception:
            logger.info(f"Please open {url} in your browser")
    else:
        logger.info(f"Please open {url} in your browser")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Chat server stopped")
        server.shutdown()
