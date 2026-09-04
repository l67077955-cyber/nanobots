"""Agent Skills 标准兼容性：生态 SKILL.md（含折叠块、嵌套 metadata、
可选字段、CRLF）必须能直接落进 skills/ 目录并被正确解析。

钉住的行为：
- `description: >-` / `|-` 等折叠/字面块 → 解析出全文，而不是字面 ">-"。
- 标准可选字段（license / allowed-tools / 嵌套 metadata map）不破坏解析。
- 嵌套 metadata map 里的 nanobot 键（always）生效；旧式 JSON 字符串 metadata 仍生效。
- CRLF 行尾可解析；无 frontmatter 时保持既有 None 契约。
- 真实生态样本（superpowers）冒烟：description 提取非空、无残留 artifact。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from nanobot.skills.loader import SkillsLoader

SUPERPOWERS_SRC = Path(
    "/root/archive/clones/agent-skills-import/superpowers/skills"
)


def _ws_with_skill(name: str, frontmatter: str, body: str = "# Body\n") -> Path:
    ws = Path(tempfile.mkdtemp())
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return ws


def _meta(ws: Path, name: str) -> dict | None:
    return SkillsLoader(ws, builtin_skills_dir=None).get_skill_metadata(name)


class TestStandardBlockScalars:
    def test_folded_description(self):
        ws = _ws_with_skill(
            "folded-skill",
            "name: folded-skill\ndescription: >-\n  Use when writing code.\n  Covers bugs and features.\n",
        )
        meta = _meta(ws, "folded-skill")
        assert meta is not None
        assert "Use when writing code" in meta["description"]
        assert "Covers bugs" in meta["description"]
        assert meta["description"] != ">-"

    def test_folded_strip_variant(self):
        ws = _ws_with_skill(
            "strip-skill",
            "description: |-\n  Line one\n  Line two\n",
        )
        meta = _meta(ws, "strip-skill")
        assert "Line one" in meta["description"]
        assert "Line two" in meta["description"]

    def test_literal_block_keeps_newlines(self):
        ws = _ws_with_skill(
            "literal-skill",
            "description: |\n  Para one\n  Para two\n",
        )
        desc = _meta(ws, "literal-skill")["description"]
        assert "Para one" in desc and "Para two" in desc

    def test_no_key_pollution_from_block_body(self):
        # 折叠块内含冒号的行不得变成顶层键
        ws = _ws_with_skill(
            "colon-skill",
            "description: >-\n  Use for api: endpoints and cli: tools\n",
        )
        meta = _meta(ws, "colon-skill")
        assert "api: endpoints" in meta["description"]
        assert "cli" not in meta  # 冒号行没有被误解析成新键


class TestStandardOptionalFields:
    def test_optional_fields_present(self):
        ws = _ws_with_skill(
            "std-skill",
            "name: std-skill\n"
            "description: Standard skill\n"
            "license: MIT\n"
            "allowed-tools: Bash, Read\n",
        )
        meta = _meta(ws, "std-skill")
        assert meta["license"] == "MIT"
        assert "Bash" in str(meta["allowed-tools"])

    def test_nested_metadata_map(self):
        ws = _ws_with_skill(
            "nested-skill",
            "description: nested\nmetadata:\n  always: true\n",
        )
        meta = _meta(ws, "nested-skill")
        assert isinstance(meta.get("metadata"), dict)
        assert meta["metadata"].get("always") is True

    def test_nested_metadata_always_gates(self):
        ws = _ws_with_skill(
            "nested-always",
            "description: nested always\nmetadata:\n  always: true\n",
        )
        loader = SkillsLoader(ws, builtin_skills_dir=None)
        assert "nested-always" in loader.get_always_skills()

    def test_legacy_json_metadata_string_still_works(self):
        ws = _ws_with_skill(
            "legacy-skill",
            'description: legacy\nmetadata: \'{"nanobot": {"always": true}}\'\n',
        )
        loader = SkillsLoader(ws, builtin_skills_dir=None)
        assert "legacy-skill" in loader.get_always_skills()


class TestEdgeCases:
    def test_quoted_colon_description(self):
        ws = _ws_with_skill(
            "quoted-skill",
            'description: "触发: 编码、api: 调试"\n',
        )
        assert "api: 调试" in _meta(ws, "quoted-skill")["description"]

    def test_crlf_frontmatter(self):
        ws = Path(tempfile.mkdtemp())
        d = ws / "skills" / "crlf-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(
            b"---\r\ndescription: crlf skill\r\n---\r\n# Body\r\n"
        )
        meta = _meta(ws, "crlf-skill")
        assert meta is not None
        assert "crlf skill" in meta["description"]

    def test_no_frontmatter_returns_none(self):
        ws = Path(tempfile.mkdtemp())
        d = ws / "skills" / "bare-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# Just markdown\n", encoding="utf-8")
        assert _meta(ws, "bare-skill") is None

    def test_comment_and_list_lines_skipped(self):
        ws = _ws_with_skill(
            "comment-skill",
            "# a comment\ndescription: has comments\n- stray list item\n",
        )
        meta = _meta(ws, "comment-skill")
        assert meta["description"] == "has comments"


class TestRealEcosystemSample:
    def test_superpowers_skill_smoke(self):
        if not SUPERPOWERS_SRC.exists():
            return  # 归档不在时跳过（CI 环境无该样本）
        src = SUPERPOWERS_SRC / "using-git-worktrees"
        ws = Path(tempfile.mkdtemp())
        shutil.copytree(src, ws / "skills" / src.name)
        loader = SkillsLoader(ws, builtin_skills_dir=None)
        meta = loader.get_skill_metadata(src.name)
        assert meta is not None
        desc = meta["description"]
        assert desc and desc.strip()
        assert ">-" not in desc and "|" not in desc[:3]
        names = [s["name"] for s in loader.list_skills()]
        assert src.name in names


class TestFallbackParserPath:
    """PyYAML 不可用时，手写子集解析器必须满足同样的契约。"""

    def test_subset_parser_matches_contract(self, monkeypatch, tmp_path):
        import nanobot.skills.loader as loader_mod

        monkeypatch.setattr(loader_mod, "yaml", None)

        ws = tmp_path
        d = ws / "skills" / "fb-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\n"
            "description: >-\n"
            "  Folded text here\n"
            "  second line with colon: kept\n"
            "metadata:\n"
            "  always: true\n"
            "---\n# Body\n",
            encoding="utf-8",
        )
        loader = loader_mod.SkillsLoader(ws, builtin_skills_dir=None)
        meta = loader.get_skill_metadata("fb-skill")
        assert "Folded text here" in meta["description"]
        assert "colon: kept" in meta["description"]
        assert meta["metadata"] == {"always": True}
        assert "fb-skill" in loader.get_always_skills()
