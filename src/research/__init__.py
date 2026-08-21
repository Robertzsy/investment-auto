"""Offline research loop: backtests, strategy experiments and bug fixes.

The research plane is deliberately isolated from the trading plane:

* every round starts a fresh agent with no conversation history;
* the shared workspace under runtime/research is the only long-term memory;
* only a bounded structured report crosses rounds;
* the trading path never gets a shell - the restricted research sandbox below
  is the only place commands may run;
* production config, accounts and order flow can never be changed from here
  without going through the versioned ChangeManager.
"""

from src.research.workspace import ResearchWorkspace
from src.research.loop import ResearchOutcome, run_research_loop

__all__ = ["ResearchWorkspace", "ResearchOutcome", "run_research_loop"]
