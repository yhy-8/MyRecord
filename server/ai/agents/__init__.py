"""Independent analysis agents and their shared invocation protocol."""

from . import retrospective, reviewer
from .base import AgentPipelineError, AgentSpec, invoke_agent


AGENTS = {
    module.SPEC.name: module.SPEC
    for module in (retrospective, reviewer)
}

__all__ = [
    "AGENTS",
    "AgentPipelineError",
    "AgentSpec",
    "invoke_agent",
    "retrospective",
    "reviewer",
]