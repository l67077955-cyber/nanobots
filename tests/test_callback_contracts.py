"""Contract tests for Telegram callback-data routes.

Enforces the single-definition-point contract from
``nanobot/channels/telegram/callbacks_registry.py``:

1. **No dead buttons** — every prefix emitted via ``callback_data=`` must be
   parsed somewhere (``startswith`` / ``==``). The ``emc_mi`` bug (button
   emitted, handler read the wrong segment) is the reason this exists.
2. **Registry ratchet** — every prefix, emitted or parsed, must be declared
   in ``ROUTES``. New undeclared routes fail here.
3. **No shadowing** — no parsed prefix may be a colon-boundary prefix of a
   longer one that is checked later in the same dispatcher.
4. **Round-trip** — every builder in the registry parses back into exactly
   the values it was built from (kills the ``parts[3]`` vs ``parts[4]``
   arity bug class per route).

Deliberately static-scans source text (not imports): the contract is about
what literals exist in the codebase, which reflection cannot see.
"""

import re
from pathlib import Path

import pytest

from nanobot.channels.telegram import callbacks_registry as reg

PKG_DIR = Path(__file__).resolve().parent.parent / "nanobot" / "channels" / "telegram"

# Emitted: callback_data=f"pfx:..." / callback_data="pfx..." — capture the
# literal head up to ':' or end-of-string (f-string interpolation starts later).
EMIT_RE = re.compile(r'callback_data=f?"([a-z_0-9]+)')
# Parsed: startswith (prefix may carry a trailing ':'), exact equality, or
# membership in a tuple of exact payloads ("pr:refresh" → prefix "pr").
PARSE_START_RE = re.compile(r'(?:data|action)\.startswith\(f?"([^"]+)"\)')
PARSE_EQ_RE = re.compile(r'(?:data|action) == "([^"]+)"')
PARSE_IN_RE = re.compile(r'(?:data|action) in \(([^)]*)\)')


def _scan() -> tuple[set[str], set[str], set[str]]:
    """Returns (emitted, parsed, startswith_parsed) prefix sets."""
    emitted: set[str] = set()
    parsed: set[str] = set()
    startswith_parsed: set[str] = set()
    for path in PKG_DIR.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for m in EMIT_RE.finditer(src):
            emitted.add(m.group(1))
        for m in PARSE_START_RE.finditer(src):
            parsed.add(m.group(1).rstrip(":"))
            startswith_parsed.add(m.group(1).rstrip(":"))
        for m in PARSE_EQ_RE.finditer(src):
            parsed.add(m.group(1))
        for m in PARSE_IN_RE.finditer(src):
            for lit in re.findall(r'"([^"]+)"', m.group(1)):
                parsed.add(lit.split(":")[0])
    return emitted, parsed, startswith_parsed

# Emitted prefixes whose payloads are built dynamically (f-string interpolation
# before the ':') or intentionally inert. Each entry: prefix -> why.
DYNAMIC_EMITTED: dict[str, str] = {
    # log.py builds cb_prefix = "rlogs_pg" if keyword else "rlog_pg" into an
    # f-string; both ARE parsed (callbacks.py rlog_pg/rlogs_pg handlers).
    "rlogs_pg": "emitted via dynamic cb_prefix in commands/log.py",
    "rlog_pg": "emitted via dynamic cb_prefix in commands/log.py",
    # Decorative separator button: inert by design, never dispatched.
    "noop": "separator button (commands/agents.py), intentionally not parsed",
}

# Parsed prefixes with no literal emitter. Each entry: prefix -> why.
PARSED_NO_EMITTER: dict[str, str] = {
    # "m:" payloads strip the prefix then dispatch on the action segment
    # (see _dispatch_menu_action); sub-actions are checked via `action ==`.
    "m": "dispatcher route; m:<action> sub-actions checked individually",
    # Legacy wire format still parsed so old buttons in chat history work.
    "em_model": "legacy payload; kept alive for buttons predating ag_mdl_pick",
}


def test_every_emitted_prefix_is_parsed() -> None:
    emitted, parsed, _ = _scan()
    unparsed = sorted(
        p for p in emitted
        if p not in parsed and p not in DYNAMIC_EMITTED and not p.startswith("m:")
    )
    assert not unparsed, (
        f"Dead buttons: emitted but never parsed (each is a silent no-op): {unparsed}"
    )


def test_every_prefix_is_declared_in_registry() -> None:
    emitted, parsed, _ = _scan()
    routes = set(reg.ROUTES)
    undeclared = sorted((emitted | parsed) - routes - set(DYNAMIC_EMITTED))
    assert not undeclared, (
        f"Undeclared routes — add them to callbacks_registry.ROUTES: {undeclared}"
    )


def test_no_prefix_shadowing() -> None:
    """A payload for the longer prefix must not be swallowed by a shorter
    one that is also dispatched with startswith. Exact-match (==/in) forms
    cannot swallow, so only startswith dispatch participates."""
    _, _, startswith_parsed = _scan()
    shadowing = sorted(
        (a, b)
        for a in startswith_parsed
        for b in startswith_parsed
        if a != b and (b + ":").startswith(a + ":")
    )
    assert not shadowing, f"Prefix shadowing pairs (shorter, longer): {shadowing}"


@pytest.mark.parametrize("prefix", [
    reg.AG_MDL_PROV, reg.AG_MDL_PICK, reg.AG_MDL_BY_NAME, reg.AG_MDL_MANUAL,
    reg.AG_MDL_CREATE_PROV, reg.AG_MDL_CREATE_PICK,
    reg.AG_MDL_CREATE_MANUAL, reg.AG_MDL_CREATE_SKIP,
])
def test_builder_payload_carries_its_prefix(prefix: str) -> None:
    builders = {
        reg.AG_MDL_PROV: reg.ag_mdl_prov("kirk", "openrouter"),
        reg.AG_MDL_PICK: reg.ag_mdl_pick("kirk", "openrouter", 3),
        reg.AG_MDL_BY_NAME: reg.ag_mdl_by_name("kirk", "xai", "grok-4.1:fast"),
        reg.AG_MDL_MANUAL: reg.ag_mdl_manual("kirk"),
        reg.AG_MDL_CREATE_PROV: reg.ag_mdl_create_prov("luna", "新国产"),
        reg.AG_MDL_CREATE_PICK: reg.ag_mdl_create_pick("luna", "新国产", 0),
        reg.AG_MDL_CREATE_MANUAL: reg.ag_mdl_create_manual("luna"),
        reg.AG_MDL_CREATE_SKIP: reg.ag_mdl_create_skip("glm-5:turbo"),
    }
    assert builders[prefix].startswith(prefix + ":")


def test_parse_args_round_trip() -> None:
    # Values containing ':' must survive in the trailing arg (maxsplit).
    payload = reg.ag_mdl_by_name("kirk", "xai", "grok-4.1:fast")
    assert reg.parse_args(payload, reg.AG_MDL_BY_NAME, maxsplit=2) == [
        "kirk", "xai", "grok-4.1:fast",
    ]
    payload = reg.ag_mdl_pick("kirk", "openrouter", 41)
    assert reg.parse_args(payload, reg.AG_MDL_PICK) == ["kirk", "openrouter", "41"]
    payload = reg.ag_mdl_create_skip("glm-5:turbo")
    assert reg.parse_args(payload, reg.AG_MDL_CREATE_SKIP, maxsplit=0) == ["glm-5:turbo"]
    with pytest.raises(ValueError):
        reg.parse_args("other:payload", reg.AG_MDL_PICK)
