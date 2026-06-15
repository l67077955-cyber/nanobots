"""nanobot.command package.

Re-exports the builtin command registration so that code (including the
AgentLoop in compiled form) can do:

    from nanobot.command import register_builtin_commands
"""

from .builtin import register_builtin_commands  # noqa: F401
from .router import CommandContext, CommandRouter  # noqa: F401
