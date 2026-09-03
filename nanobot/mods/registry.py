"""Mod discovery — builtin package, workspace files, external entry points.

Mirrors the channel plugin registry (``nanobot/channels/registry.py``):

1. **Builtin** — submodules/packages under ``nanobot/mods/builtin/`` scanned
   with pkgutil (no import until needed).
2. **Workspace** — ``~/.nanobot/mods/<name>/mod.py`` loaded straight from
   disk. This is the layer the *agents themselves* are expected to use:
   drop a directory, get a mod, never touch core code again.
3. **External** — setuptools entry_points group ``nanobot.mods``.

Precedence on name collision: builtin > workspace > external (same
"builtins shadow plugins" rule as channels — a workspace mod must never
silently replace shipped behaviour; pick a different name).
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.mods.base import Mod

_BUILTIN_PKG = "nanobot.mods.builtin"
_SKIP_NAMES = {"base", "registry", "manager", "__init__"}


def _first_mod_class(module: Any) -> type[Mod] | None:
    """Return the first Mod subclass defined in *module*."""
    candidates = [
        obj for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, Mod) and obj is not Mod
    ]
    return candidates[0] if candidates else None


def discover_builtin() -> dict[str, type[Mod]]:
    """Scan nanobot.mods.builtin for Mod subclasses (name → class)."""
    found: dict[str, type[Mod]] = {}
    try:
        import nanobot.mods.builtin as _pkg  # noqa: PLC0415
    except ImportError:
        return found
    for mod_info in pkgutil.iter_modules(_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{_BUILTIN_PKG}.{mod_info.name}")
        except Exception as e:  # noqa: BLE001 — one bad builtin must not kill the rest
            logger.warning("mods: builtin '{}' failed to import: {}", mod_info.name, e)
            continue
        cls = _first_mod_class(module)
        if cls is not None:
            found[cls.name] = cls
    return found


def discover_workspace(mods_dir: Path | None = None) -> dict[str, type[Mod]]:
    """Load ~/.nanobot/mods/<name>/mod.py — the agent/user authoring layer."""
    base = mods_dir or Path.home() / ".nanobot" / "mods"
    found: dict[str, type[Mod]] = {}
    if not base.is_dir():
        return found
    for mod_dir in sorted(base.iterdir()):
        if not mod_dir.is_dir() or mod_dir.name.startswith(("_", ".")):
            continue
        mod_file = mod_dir / "mod.py"
        if not mod_file.is_file():
            continue
        module_name = f"nanobot_mods_{mod_dir.name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, mod_file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001
            logger.warning("mods: workspace '{}' failed to load: {}", mod_dir.name, e)
            continue
        cls = _first_mod_class(module)
        if cls is not None:
            found[cls.name] = cls
        else:
            logger.warning("mods: workspace '{}' has no Mod subclass", mod_dir.name)
    return found


def discover_external() -> dict[str, type[Mod]]:
    """Entry-points group ``nanobot.mods`` from installed distributions."""
    found: dict[str, type[Mod]] = {}
    try:
        eps = importlib.metadata.entry_points(group="nanobot.mods")  # type: ignore[attr-defined]
    except Exception:
        return found
    for ep in eps:
        try:
            cls = ep.load()
        except Exception as e:  # noqa: BLE001
            logger.warning("mods: external '{}' failed to load: {}", ep.name, e)
            continue
        if isinstance(cls, type) and issubclass(cls, Mod):
            found[cls.name] = cls
    return found


def discover_all() -> dict[str, type[Mod]]:
    """Merged view with builtin > workspace > external precedence."""
    merged = discover_external()
    merged.update(discover_workspace())
    builtin = discover_builtin()
    for name in set(merged) & set(builtin):
        logger.warning("mods: builtin '{}' shadows a workspace/external mod", name)
    merged.update(builtin)
    return merged
