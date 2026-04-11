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
MAX_SKILLS_SUMMARY_CHARS: int = 15_000


def build_skills_section(workspace: Path) -> str:
    """Build the skills section for prompt injection (shared logic).

    Always-on skills are injected in full; other skills are listed
    compactly (one line each) for progressive loading via read_file.
    """
    loader = SkillsLoader(workspace)
    parts: list[str] = []

    always_skills = loader.get_always_skills()
    if always_skills:
        content = loader.load_skills_for_context(always_skills)
        if content:
            parts.append(content)

    summary = loader.build_skills_summary(exclude=set(always_skills) if always_skills else None)
    if summary:
        parts.append("Other skills (read SKILL.md to use):\n" + summary)

    return "\n\n".join(parts)


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

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                # Resolve {baseDir} to actual skill directory path
                base_dir = self._resolve_skill_dir(name)
                if base_dir:
                    content = content.replace("{baseDir}", base_dir)
                parts.append(f"### Skill: {name}\n\n{content}")

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
            Skills whose SKILL.md exceeds MAX_SINGLE_SKILL_CHARS are skipped and
            counted in the footer note so the agent knows they exist.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        exclude = exclude or set()
        lines: list[str] = []
        total_chars = 0
        skipped_large: list[str] = []
        skipped_budget: list[str] = []

        for s in all_skills:
            name = s["name"]
            if name in exclude:
                continue

            # Skip single skills that are too large to ever inline sanely.
            skill_path = Path(s["path"])
            try:
                skill_size = skill_path.stat().st_size
            except OSError:
                skill_size = 0
            if skill_size > MAX_SINGLE_SKILL_CHARS:
                skipped_large.append(name)
                continue

            desc = self._get_skill_description(name)
            skill_meta = self._get_skill_meta(name)
            available = self._check_requirements(skill_meta)
            status = ""
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                status = f" [unavailable: {missing}]" if missing else " [unavailable]"
            line = f"- {name}: {desc}{status}"

            # Enforce total character budget.
            if total_chars + len(line) + 1 > MAX_SKILLS_SUMMARY_CHARS:
                skipped_budget.append(name)
                continue

            lines.append(line)
            total_chars += len(line) + 1  # +1 for newline

        # Footer: let the agent know there are more skills it can load on demand.
        hidden = skipped_large + skipped_budget
        if hidden:
            lines.append(
                f"\n(还有 {len(hidden)} 个技能因体积/数量限制未展示: "
                + ", ".join(hidden[:8])
                + (f" 等" if len(hidden) > 8 else "")
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

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

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
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None
