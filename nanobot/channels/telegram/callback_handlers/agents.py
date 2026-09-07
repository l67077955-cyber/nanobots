"""Agent-related callback handlers (add/rm/edit/delete, model selection).

Handles callbacks with prefixes:
- add:AgentName — add agent to conversation
- rm:AgentName — remove agent from conversation
- edit:AgentName — show edit menu
- da:AgentName — show delete confirmation
- dac:AgentName:yes/no — confirm/cancel delete
- ef:AgentName:field — edit field prompt
- ef_re:AgentName:field — edit field with reasoning
- ep_* — edit prompts and model selection
"""

from __future__ import annotations


class AgentCallbacksMixin:
    """Mixin providing agent-related callback handlers."""

    # Note: Implementation lives in callbacks.py.
    # This stub marks the domain boundary for future extraction.

    pass
