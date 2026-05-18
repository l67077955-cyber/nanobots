"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Budget caps — keep skills section from bloating the prompt.
# Single SKILL.md files larger than this are skipped and only noted in the footer.
MAX_SINGLE_SKILL_CHARS: int = 20_000
# Total character budget for the non-always-on summary block.
MAX_SKILLS_SUMMARY_CHARS: int = 8_000
# Total budget for always-on skills (cumulative, enforced inside load_skills_for_context).
MAX_ALWAYS_SKILLS_CHARS: int = 5600
# Per-skill inline truncation limit for always-on skills.
MAX_ALWAYS_SKILL_INLINE: int = 700

_BOOL_TRUE = frozenset({"true", "yes", "1", "on"})


def _parse_bool(value) -> bool:
    """Parse a value that may be a string like 'true'/'false' into a real bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower().strip() in _BOOL_TRUE
    return bool(value)


def build_skills_section(workspace: Path) -> tuple[str, str]:
    """Build the skills section for prompt injection (shared logic).

    Returns (static_content, dynamic_content):
      - static_content: always-on skills with type=static inlined (stable, before history)
      - dynamic_content: always-on skills with type=dynamic inlined + summary + undocumented scripts (volatile, after history)

    Skills declare ``type: dynamic`` in frontmatter to opt into volatile template
    variables (e.g. ``{{datetime}}``).  Default is ``type: static``.
    """
    loader = SkillsLoader(workspace)

    # ── Split always-on skills by type ──
    static_always: list[str] = []
    dynamic_always: list[str] = []
    for name in loader.get_always_skills():
        if loader.get_skill_type(name) == "dynamic":
            dynamic_always.append(name)
        else:
            static_always.append(name)

    # ── Static: always-on skills with type=static ──
    static_parts: list[str] = []
    if static_always:
        content = loader.load_skills_for_context(
            static_always,
            max_chars_per_skill=MAX_ALWAYS_SKILL_INLINE,
            max_total_chars=MAX_ALWAYS_SKILLS_CHARS,
        )
        if content:
            static_parts.append(content)
    static_content = "\n\n".join(static_parts)

    # ── Dynamic: always-on type=dynamic inlined + summary + undocumented scripts ──
    dynamic_parts: list[str] = []
    if dynamic_always:
        content = loader.load_skills_for_context(
            dynamic_always,
            max_chars_per_skill=MAX_ALWAYS_SKILL_INLINE,
            max_total_chars=MAX_ALWAYS_SKILLS_CHARS,
        )
        if content:
            dynamic_parts.append(content)

    all_always = set(static_always) | set(dynamic_always)
    summary = loader.build_skills_summary(exclude=all_always if all_always else None)
    if summary:
        dynamic_parts.append("Other skills (read SKILL.md to use):\n" + summary)

    undocumented = loader._discover_undocumented_scripts()
    if undocumented:
        dynamic_parts.append("Undocumented scripts (auto-discovered from scripts/ dirs):\n" + "\n".join(undocumented))

    dynamic_content = "\n\n".join(dynamic_parts)

    return static_content, dynamic_content


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []

        # Workspace skills (highest priority)
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                # Skip disabled skill directories entirely — they exist for
                # reference/archive only and should never burn context tokens.
                if skill_dir.name.startswith("_disabled."):
                    continue
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # Built-in skills
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.name.startswith("_disabled."):
                    continue
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        # Check workspace first
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # Check built-in
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(
        self,
        skill_names: list[str],
        max_chars_per_skill: int = 0,
        max_total_chars: int = 0,
    ) -> str:
        """Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.
            max_chars_per_skill: If > 0, truncate each skill's content to this
                many chars and append a read_file hint for the remainder.
            max_total_chars: If > 0, stop adding skills once the cumulative
                output would exceed this limit (footer note appended).

        Returns:
            Formatted skills content.
        """
        parts = []
        total_chars = 0
        skipped: list[str] = []

        for name in skill_names:
            content = self.load_skill(name)
            if not content:
                continue
            content = self._strip_frontmatter(content)
            base_dir = self._resolve_skill_dir(name)
            if base_dir:
                content = content.replace("{baseDir}", base_dir)
            # Per-skill truncation.
            if max_chars_per_skill and len(content) > max_chars_per_skill:
                skill_path = f"skills/{name}/SKILL.md"
                content = (
                    content[:max_chars_per_skill]
                    + f"\n\n[…truncated. `read_file {skill_path}` for full doc.]"
                )
            block = f"### Skill: {name}\n\n{content}"
            # Cumulative budget check.
            if max_total_chars and total_chars + len(block) > max_total_chars:
                skipped.append(name)
                continue
            parts.append(block)
            total_chars += len(block)

        # Append remaining skipped names so the agent knows they exist.
        if skipped:
            note = (
                "\n\n(还有 always-on 技能因总预算限制未完整展示: "
                + ", ".join(skipped)
                + "。用 `read_file skills/<名称>/SKILL.md` 加载。)"
            )
            if parts:
                parts[-1] += note
            else:
                parts.append(note)

        return "\n\n---\n\n".join(parts) if parts else ""

    def _resolve_skill_dir(self, name: str) -> str | None:
        """Get the absolute directory path for a skill."""
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return str(self.workspace_skills / name)
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return str(self.builtin_skills / name)
        return None

    def build_skills_summary(
        self, *, exclude: set[str] | None = None,
    ) -> str:
        """Build a compact summary of available skills.

        Args:
            exclude: Skill names to omit (e.g. already-loaded always-on skills).

        Returns:
            Compact one-line-per-skill summary, capped at MAX_SKILLS_SUMMARY_CHARS.
            Large skills (>MAX_SINGLE_SKILL_CHARS) get a description line with
            a read_file pointer; remaining budget is filled with normal skills.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        exclude = exclude or set()
        lines: list[str] = []
        total_chars = 0
        skipped_budget: list[str] = []

        # --- Pass 1: large skills first (they were previously invisible) ---
        for s in all_skills:
            name = s["name"]
            if name in exclude:
                continue
            skill_path = Path(s["path"])
            try:
                skill_size = skill_path.stat().st_size
            except OSError:
                skill_size = 0
            if skill_size > MAX_SINGLE_SKILL_CHARS:
                desc = self._get_skill_description(name)
                line = f"- {name}: {desc} [large: read_file skills/{name}/SKILL.md]"
                if total_chars + len(line) + 1 <= MAX_SKILLS_SUMMARY_CHARS:
                    lines.append(line)
                    total_chars += len(line) + 1

        # --- Pass 2: normal skills fill remaining budget ---
        for s in all_skills:
            name = s["name"]
            if name in exclude:
                continue
            skill_path = Path(s["path"])
            try:
                skill_size = skill_path.stat().st_size
            except OSError:
                skill_size = 0
            if skill_size > MAX_SINGLE_SKILL_CHARS:
                continue  # already handled in pass 1

            desc = self._get_skill_description(name)
            skill_meta = self._get_skill_meta(name)
            available = self._check_requirements(skill_meta)
            status = ""
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                status = f" [unavailable: {missing}]" if missing else " [unavailable]"
            line = f"- {name}: {desc}{status}"

            if total_chars + len(line) + 1 > MAX_SKILLS_SUMMARY_CHARS:
                skipped_budget.append(name)
                continue

            lines.append(line)
            total_chars += len(line) + 1  # +1 for newline

        # Footer: let the agent know there are more skills it can load on demand.
        if skipped_budget:
            lines.append(
                f"\n(还有 {len(skipped_budget)} 个技能因数量限制未展示: "
                + ", ".join(skipped_budget[:8])
                + (f" 等" if len(skipped_budget) > 8 else "")
                + "。需要时请用 `read_file skills/<名称>/SKILL.md` 按需加载。)"
            )

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter (supports nanobot and openclaw keys)."""
        try:
            data = json.loads(raw)
            return data.get("nanobot", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _parse_yaml_value(value: str):
        """Convert YAML scalar strings to proper Python types (bool, str)."""
        v = value.strip('"\'')
        if v.lower() in ("true", "yes", "on"):
            return True
        if v.lower() in ("false", "no", "off"):
            return False
        return False
        return v

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get nanobot metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(meta.get("metadata", ""))

    def get_skill_type(self, name: str) -> str:
        """Get the prompt type for a skill: 'static' or 'dynamic'.

        Skills with ``type: dynamic`` in frontmatter are injected after history
        where volatile template variables (e.g. ``{{datetime}}``) are available.
        Default is 'static'.
        """
        meta = self.get_skill_metadata(name) or {}
        skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
        t = skill_meta.get("type") or meta.get("type")
        if isinstance(t, str) and t.strip().lower() == "dynamic":
            return "dynamic"
        return "static"

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            if _parse_bool(skill_meta.get("always")) or _parse_bool(meta.get("always")):
                result.append(s["name"])
        return result

    def _discover_undocumented_scripts(self) -> list[str]:
        """Find scripts in skill dirs not mentioned in their SKILL.md."""
        entries = []
        for s in self.list_skills(filter_unavailable=False):
            name = s["name"]
            skill_dir = Path(s["path"]).parent
            scripts_dir = skill_dir / "scripts"
            if not scripts_dir.exists():
                continue
            skill_content = self.load_skill(name) or ""
            for script_file in sorted(scripts_dir.iterdir()):
                if script_file.suffix not in (".py", ".sh") or script_file.name.startswith("_"):
                    continue
                if script_file.name not in skill_content:
                    docstring = ""
                    try:
                        text = script_file.read_text(encoding="utf-8", errors="ignore")
                        m = re.search(r'"""(.*?)(?:\n|$)', text) or re.search(r"'''(.*?)(?:\n|$)", text)
                        if m:
                            docstring = m.group(1).strip().rstrip(".")
                    except Exception:
                        pass
                    desc = f" — {docstring}" if docstring else ""
                    entries.append(f"  - `{script_file.name}`{desc}")
        return entries

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # YAML parsing with multiline string support (| and >)
                metadata = {}
                lines = match.group(1).split("\n")
                i = 0
                while i < len(lines):
                    line = lines[i]
                    if ":" in line:
                        key, value = line.split(":", 1)
                        key = key.strip()
                        value = value.strip()
                        # Handle multiline strings
                        if value in ("|", ">"):
                            # Collect subsequent indented lines
                            sub_lines = []
                            i += 1
                            while i < len(lines) and (lines[i].startswith("  ") or lines[i] == ""):
                                sub_lines.append(lines[i].rstrip())
                                i += 1
                            i -= 1  # compensate for loop increment
                            # Join and dedent
                            joined = "\n".join(sub_lines).strip()
                            metadata[key] = joined
                        else:
                            metadata[key] = value.strip('"\'')
                    i += 1
                return metadata

        return None
