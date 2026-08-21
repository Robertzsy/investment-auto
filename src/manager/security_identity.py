from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_CN_PROVIDER = re.compile(r"^(sh|sz|bj)(\d{6})$", re.I)


def normalize_provider_text(value: Any) -> str:
    """Decode provider JSON that accidentally contains literal ``\\uXXXX`` text."""
    text = str(value or "").strip()
    for _ in range(2):
        decoded = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
        if decoded == text:
            break
        text = decoded
    return text


def _exchange_from(value: str, raw: Mapping[str, Any]) -> str:
    combined = " ".join(
        normalize_provider_text(raw.get(key, "")).casefold()
        for key in ("symbol", "code", "exchange", "market")
    )
    lowered = value.casefold()
    if lowered.startswith("sh") or "沪" in combined or "sse" in combined:
        return "SH"
    if lowered.startswith("sz") or "深" in combined or "szse" in combined:
        return "SZ"
    if lowered.startswith("bj") or "北" in combined or "bse" in combined:
        return "BJ"
    digits = re.sub(r"\D", "", value)
    if digits.startswith(("6", "5")):
        return "SH"
    if digits.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def _asset_type(raw: Mapping[str, Any], symbol: str, name: str) -> str:
    marker = " ".join(
        normalize_provider_text(raw.get(key, "")).casefold()
        for key in ("type", "asset_type", "security_type", "category")
    )
    marker = f"{marker} {name.casefold()}"
    if any(value in marker for value in ("zs", "index", "指数")):
        return "index"
    if any(value in marker for value in ("etf", "fund", "基金")) or symbol.startswith(("5", "1")):
        return "etf"
    return "equity"


@dataclass(frozen=True)
class SecurityIdentity:
    symbol: str
    canonical_symbol: str
    provider_symbol: str
    exchange: str
    asset_type: str
    market: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_search_row(
        cls,
        row: Mapping[str, Any],
        *,
        fallback_query: str = "",
        fallback_market: str = "",
    ) -> "SecurityIdentity":
        raw = row.get("raw", row)
        raw = raw if isinstance(raw, Mapping) else {}
        candidates = [
            raw.get("symbol"),
            row.get("provider_symbol"),
            row.get("symbol"),
            raw.get("code"),
            fallback_query,
        ]
        source = next((normalize_provider_text(item) for item in candidates if str(item or "").strip()), "")
        name = normalize_provider_text(
            row.get("name") or raw.get("name") or raw.get("description") or source
        )
        lowered = source.casefold()
        cn_match = _CN_PROVIDER.fullmatch(lowered)
        if cn_match or re.fullmatch(r"\d{6}", source):
            exchange = _exchange_from(source, raw)
            symbol = cn_match.group(2) if cn_match else source
            provider_symbol = exchange.casefold() + symbol
            market = "cn"
            canonical = f"{exchange}:{symbol}"
        elif lowered.startswith("hk") or re.fullmatch(r"\d{5}", source):
            symbol = re.sub(r"^hk", "", source, flags=re.I).zfill(5)
            provider_symbol = "hk" + symbol
            exchange = "HK"
            market = "hk"
            canonical = f"HK:{symbol}"
        else:
            symbol = re.sub(r"^us", "", source, flags=re.I).upper()
            provider_symbol = "us" + symbol
            exchange = "US"
            market = "us" if fallback_market not in {"cn", "hk"} else fallback_market
            canonical = f"US:{symbol}"
        asset_type = _asset_type(raw, symbol, name)
        return cls(
            symbol=symbol,
            canonical_symbol=canonical,
            provider_symbol=provider_symbol,
            exchange=exchange,
            asset_type=asset_type,
            market=market,
            name=name or symbol,
        )


def snapshot_identity(payload: Mapping[str, Any]) -> Optional[str]:
    realtime = payload.get("realtime", payload)
    if not isinstance(realtime, Mapping):
        return None
    value = normalize_provider_text(
        realtime.get("code") or realtime.get("symbol") or realtime.get("ticker") or ""
    )
    if not value:
        return None
    lowered = value.casefold()
    if _CN_PROVIDER.fullmatch(lowered) or lowered.startswith(("hk", "us")):
        return lowered
    return value.upper()


def identity_matches(identity: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    actual = snapshot_identity(payload)
    expected = normalize_provider_text(identity.get("provider_symbol", "")).casefold()
    return bool(actual and expected and actual.casefold() == expected)


def evidence_date(payload: Mapping[str, Any]) -> Optional[date]:
    realtime = payload.get("realtime", payload)
    if not isinstance(realtime, Mapping):
        return None
    value = normalize_provider_text(realtime.get("time") or realtime.get("date") or "")
    digits = re.sub(r"\D", "", value)
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def is_fresh(payload: Mapping[str, Any], *, today: Optional[date] = None, max_age_days: int = 4) -> bool:
    source_date = evidence_date(payload)
    if source_date is None:
        return False
    age = ((today or datetime.now().astimezone().date()) - source_date).days
    return 0 <= age <= max_age_days
