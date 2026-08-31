"""Single definition point for Telegram callback-data route prefixes.

Buttons all over the UI build ``callback_data=f"{prefix}:{args}"`` and the
dispatcher in ``callbacks.py`` matches ``data.startswith(prefix + ":")``.
Before this module those two literals were hand-typed independently on each
side — the ``emc_mi`` dead-button bug (generator emitted 4 segments, parser
read ``parts[4]``) was the direct result.

Contract rules (enforced by ``tests/test_callback_contracts.py``):

1. Wire prefixes are IMMUTABLE. Renaming breaks buttons still sitting in
   users' chat histories. New code gets a named constant here; the constant
   name follows ``<domain>_<object>_<action>``, the wire value stays as-is.
2. Build payloads with the builders below — never inline f-strings next to
   a button.
3. Parse with :func:`parse_args`; builder/parser pairs are round-trip
   tested, which kills the arity-mismatch bug class (``parts[3]`` vs
   ``parts[4]``) at the route level.
4. Every prefix emitted or parsed anywhere in the telegram package must be
   declared in :data:`ROUTES` (the ratchet: new undeclared routes fail CI).

Migration is incremental: only the agent-model-picking family is fully on
builders so far; the rest are declared as bare strings and migrate family
by family.
"""
from __future__ import annotations

# ── agent / model picking (pilot family; legacy wire values em_*/emc_*) ─────

AG_MDL_PROV = "em_prov"        # edit flow: pick provider for agent's model
AG_MDL_PICK = "em_mi"          # edit flow: pick model by list index
AG_MDL_BY_NAME = "em_model"    # edit flow: pick model by exact id (legacy)
AG_MDL_MANUAL = "em_manual"    # edit flow: type model id by hand

AG_MDL_CREATE_PROV = "emc_prov"      # create flow: pick provider
AG_MDL_CREATE_PICK = "emc_mi"        # create flow: pick model by index
AG_MDL_CREATE_MANUAL = "emc_manual"  # create flow: type model id
AG_MDL_CREATE_SKIP = "emc_skip"      # create flow: skip model test


def ag_mdl_prov(agent: str, prov: str) -> str:
    return f"{AG_MDL_PROV}:{agent}:{prov}"


def ag_mdl_pick(agent: str, prov: str, idx: int) -> str:
    return f"{AG_MDL_PICK}:{agent}:{prov}:{idx}"


def ag_mdl_by_name(agent: str, prov: str, model: str) -> str:
    return f"{AG_MDL_BY_NAME}:{agent}:{prov}:{model}"


def ag_mdl_manual(agent: str) -> str:
    return f"{AG_MDL_MANUAL}:{agent}"


def ag_mdl_create_prov(agent: str, prov: str) -> str:
    return f"{AG_MDL_CREATE_PROV}:{agent}:{prov}"


def ag_mdl_create_pick(agent: str, prov: str, idx: int) -> str:
    return f"{AG_MDL_CREATE_PICK}:{agent}:{prov}:{idx}"


def ag_mdl_create_manual(agent: str) -> str:
    return f"{AG_MDL_CREATE_MANUAL}:{agent}"


def ag_mdl_create_skip(model: str) -> str:
    return f"{AG_MDL_CREATE_SKIP}:{model}"


def parse_args(data: str, prefix: str, maxsplit: int = -1) -> list[str]:
    """Split a prefixed callback payload into its ':'-separated args.

    ``maxsplit`` mirrors ``str.split`` semantics: the LAST argument may
    contain ':' (model ids, free text). Raises ValueError when the payload
    does not carry this prefix — a programming error, not user input.
    """
    head = prefix + ":"
    if not data.startswith(head):
        raise ValueError(f"payload {data!r} does not carry prefix {head!r}")
    return data[len(head):].split(":", maxsplit)


# ── declared route inventory (contract-test ratchet) ────────────────────────
# Every prefix emitted (callback_data=...) or parsed (startswith / ==) in the
# telegram package must appear here. Grouped by domain; legacy names kept.

ROUTES: tuple[str, ...] = (
    # menu / settings dispatcher ("m:" payloads dispatch on the action segment)
    "m", "cfg", "cfg:cancel", "config", "new_agent", "new_provider",
    "new_model", "add_model", "inpc_cancel", "inpc_confirm", "noop",
    # agent membership in groupchat
    "add", "rm", "edit", "da", "dac", "dg",
    # agent model picking (pilot family above)
    AG_MDL_PROV, AG_MDL_PICK, AG_MDL_BY_NAME, AG_MDL_MANUAL,
    AG_MDL_CREATE_PROV, AG_MDL_CREATE_PICK, AG_MDL_CREATE_MANUAL,
    AG_MDL_CREATE_SKIP,
    # agent tools / rank / effort
    "tf", "srr", "ef", "ef_re", "think_", "think_agent", "think_set",
    "think_back",
    # hyperparams (global hp:* / per-agent ahp:*)
    "hp", "hp_add", "hp_back", "hp_custom", "hp_del", "hp_new",
    "ahp", "ahp_add", "ahp_back", "ahp_custom", "ahp_del", "ahp_new",
    "ahp_sync",
    # groupchat settings
    "gc",
    # provider management
    "ep_back", "ep_list", "ep_pick", "ep_field", "ep_models", "ep_addm",
    "ep_retry", "ep_retry_set",
    "pm_newm", "pm_cancel", "pm_delm", "pm_delm_p", "pm_delp", "pm_delp_yes",
    # model catalog browsing
    "ml_pfx", "ml_srch",
    # prompts (prompt_builder components)
    "pradd", "pradd_custom", "prcan", "prmanage", "prrules", "prd", "prdel",
    "pre", "prinfo", "pru", "prv", "pviz", "pr",
    # history settings
    "hs_", "hs_back", "hs_edit", "hs_set", "hs_global", "hs_reload",
    "hs_stage1", "hs_stage2", "hs_stage3", "hs_stage4",
    # logs
    "lg", "sl", "log", "log_pg", "logd", "logp",
    "rlog", "rlog_dl", "rlog_pg", "rlogctx", "rlogp", "rlogs_pg",
    # prompt component ordering / speedtest
    "ord", "st_agent", "st_prov",
)
