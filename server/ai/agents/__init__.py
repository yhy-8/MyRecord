"""Independent analysis agents and their shared invocation protocol."""

from . import researcher, research_planner, retrospective, reviewer
from .base import AgentPipelineError, AgentSpec, invoke_agent


AGENTS = {
    module.SPEC.name: module.SPEC
    for module in (retrospective, research_planner, researcher, reviewer)
}

__all__ = [
    "AGENTS",
    "AgentPipelineError",
    "AgentSpec",
    "invoke_agent",
    "researcher",
    "research_planner",
    "retrospective",
    "reviewer",
]
