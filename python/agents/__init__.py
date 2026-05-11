# OMNI autonomous agent swarm
from .signal_agent    import SignalAgent
from .regime_agent    import RegimeAgent
from .execution_agent import ExecutionAgent
from .risk_agent      import RiskAgent
from .learning_agent  import LearningAgent
from .monitor_agent   import MonitorAgent
from .journal_agent   import JournalAgent
from .analyst_agent   import AnalystAgent

ALL_AGENTS = [
    SignalAgent,
    RegimeAgent,
    ExecutionAgent,
    RiskAgent,
    LearningAgent,
    MonitorAgent,
    JournalAgent,
    AnalystAgent,
]

__all__ = [
    "SignalAgent", "RegimeAgent", "ExecutionAgent", "RiskAgent",
    "LearningAgent", "MonitorAgent", "JournalAgent", "AnalystAgent", "ALL_AGENTS",
]
