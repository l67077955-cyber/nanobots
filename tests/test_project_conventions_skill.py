"""Pins the project-conventions skill wiring: the coding red lines must be
discoverable from ANY workspace (builtin tier) and surfaced in the always-on
static block of every agent prompt.

Always-on skills render description + read pointer (not full body) — that
compact form is the contract, same as cron/debug.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from nanobot.skills.loader import SkillsLoader, build_skills_section

TRIGGER = "强制规范"


def _tmp_ws() -> Path:
    d = tempfile.mkdtemp()
    return Path(d)


class TestProjectConventionsSkill:
    def test_discovered_from_bare_workspace(self):
        loader = SkillsLoader(_tmp_ws())
        names = [s["name"] for s in loader.list_skills()]
        assert "project-conventions" in names
        assert [s for s in loader.list_skills() if s["name"] == "project-conventions"][0]["source"] == "builtin"

    def test_always_on(self):
        loader = SkillsLoader(_tmp_ws())
        assert "project-conventions" in loader.get_always_skills()

    def test_static_block_carries_trigger_description(self):
        static, _dynamic = build_skills_section(_tmp_ws())
        assert TRIGGER in static
        assert "project-conventions" in static
        # Pointer to the full doc must be present (compact always-on form)
        assert "project-conventions/SKILL.md" in static

    def test_body_has_red_lines_and_full_doc_pointer(self):
        skill_dir = Path(__file__).parent.parent / "nanobot" / "skills" / "project-conventions"
        body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        for must in ("先写回归测试", "AGENTS.md", "py_compile", "pytest", "RoundLifecycle"):
            assert must in body, must
