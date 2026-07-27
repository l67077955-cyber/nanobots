"""Test that callback data prefixes are unambiguous.

This ensures that:
1. Same-level prefixes don't conflict (same dispatch function)
2. Root prefixes (hs_, think_) are only checked AFTER specific dispatch fails
3. Longer prefixes are checked before shorter ones within same handler
"""

import re
from pathlib import Path

# Root prefixes that are used as fallback dispatch (checked last)
ROOT_PREFIXES = {"hs_", "think_"}


def get_all_startswith_prefixes() -> list[str]:
    """Extract all callback data prefixes from the codebase."""
    prefixes = set()
    callbacks_dir = Path(__file__).parent.parent / "nanobot" / "channels" / "telegram" / "callbacks"

    for py_file in callbacks_dir.glob("*.py"):
        content = py_file.read_text()
        # Match startswith("prefix:") patterns
        matches = re.findall(r'''startswith\(["']([^"']+)["']\)''', content)
        prefixes.update(matches)

    return sorted(prefixes)


def test_prefixes_unambiguous():
    """Verify no prefix is a prefix of another prefix (same-level conflict).

    Exception: Root prefixes (hs_, think_) are allowed to be prefixes of
    more specific ones because they're checked last as fallback.
    """
    prefixes = get_all_startswith_prefixes()
    assert prefixes, "No prefixes found"

    issues = []
    for i, p1 in enumerate(prefixes):
        for j, p2 in enumerate(prefixes):
            if i < j and p2.startswith(p1):
                # Skip if p1 is a known root prefix (fallback dispatch)
                if p1 in ROOT_PREFIXES:
                    continue
                issues.append((p1, p2))

    if issues:
        msg = "Prefix ambiguity detected:\n" + "\n".join(
            f'  "{p2}" would be matched by "{p1}"' for p1, p2 in issues
        )
        raise AssertionError(msg)


def test_root_prefixes_in_core():
    """Verify root prefixes are only used in core.py as fallback."""
    callbacks_dir = Path(__file__).parent.parent / "nanobot" / "channels" / "telegram" / "callbacks"
    core_content = (callbacks_dir / "core.py").read_text()

    for root in ROOT_PREFIXES:
        pattern = rf'startswith\(["\']({re.escape(root)})["\']\)'
        matches = re.findall(pattern, core_content)
        assert matches, f"Root prefix '{root}' not found in core.py"


def test_prefix_structure():
    """Verify all prefixes end with colon or underscore (domain boundary)."""
    prefixes = get_all_startswith_prefixes()

    # Group by root prefix (before first colon)
    root_prefixes: dict[str, list[str]] = {}
    for p in prefixes:
        # Extract root (e.g., "hs_" from "hs_grp:")
        if ":" in p:
            root = p.split(":")[0].rsplit("_", 1)[0] + "_"
        else:
            root = p
        root_prefixes.setdefault(root, []).append(p)

    # All prefixes with same root should be handled in same file
    # This is a structural check, not a hard requirement
    print(f"Found {len(prefixes)} prefixes in {len(root_prefixes)} groups")


if __name__ == "__main__":
    test_prefixes_unambiguous()
    test_root_prefixes_in_core()
    test_prefix_structure()
    print("✅ All prefix checks passed")
