"""nanobot.mods — the mod (plugin) system for orchestration behaviour.

See ``docs/MOD_PLUGIN_GUIDE.md`` for authoring. Layout:
- ``base``: Mod contract + ModContext
- ``registry``: builtin / workspace / external discovery with precedence
- ``manager``: config-driven lifecycle (``~/.nanobot/mods.json``)
- ``builtin``: shipped mods
"""
