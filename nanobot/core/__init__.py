"""Core primitives.

``History`` is the sole durable context logic layer for the product:
data (fragments) + context processing (compress, trim, build_for_*).
Groupchat runtime orchestrates collaboration; display only renders.
"""

from nanobot.core.history import Fragment, History

__all__ = ["History", "Fragment"]
