"""Standalone investment Agent application boundary."""

from .mandate import get_mandate, set_mandate
from .service import InvestmentAgentService

__all__ = ["InvestmentAgentService", "get_mandate", "set_mandate"]

