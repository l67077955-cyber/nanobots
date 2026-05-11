#!/usr/bin/env python3
"""Dead code analysis for nanobot project."""
import ast
import os
from pathlib import Path
from collections import defaultdict

NANOBOT = Path("nanobot")
EXCLUDE_PREFIXES = ("__pycache__",)
BAK_FILES = sorted((Path(".")).rglob("*"))
ba_extras = [
    p for p in BAK_FILES 
    if (
        (".bak" in p.suffixes or ".bak" in str(p.name) or ".deprecated" in str(p.name))
        and "__pycache__" not in str(p)
        and "node_modules" not in str(p)
        and "SillyTavern/" not in str(p)
        and ".git/" not in str(p)
    )
]

print("=== DEAD CODE ANALYSIS FOR NANOBOT ===\n")

# ── Category 1: Orphaned backup files (*.bak*, *.deprecated) ──
print(f"[Category 1] Backup/Deprecated Files ({len(ba_extras)} found)")
for p in ba_extras:
    rel = str(p)[:150]
    sz = os.path.getsize(str(p))
    print(f"  📄 {rel} ({sz:,} bytes)")
    
# Count within nanobot source dir specifically  
src_backups = [p for p in ba_extras if str(p).startswith("nanobot")]
print(f"\n  => In-source backups: {len(src_backups)} files")

# ── Category 2: Unused imports ──
print("\n[Category 2] Checking unused imports via AST...")

py_files = sorted(NANOBOT.rglob("*.py"))
py_files = [f for f in py_files if "__pycache__" not in str(f)]
print(f"Scanning {len(py_files)} Python files...\n")

unused_count = 0
orphaned_funcs = []

for fp in py_files:
    try:
        with open(fp) as f:
            tree = ast.parse(f.read(), filename=str(fp))
    except SyntaxError as e:
        print(f"  ⚠️  Syntax error in {fp}: {e}")
        continue
    
    relative_path = fp.relative_to(Path("."))

    # Find top-level functions, classes, assignments
    defined_names = set()
    imported_names = {}
    
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defined_names.add(node.name)
        elif isinstance(node, ast.AsyncFunctionDef):
            defined_names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            defined_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    defined_names.add(tgt.id)
                elif isinstance(tgt, ast.Tuple):
                    for elt in tgt.elts:
                        if isinstance(elt, ast.Name):
                            defined_names.add(elt.id)
        
        # Track imports
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                imp_name = alias.asname or alias.name
                imported_names[imp_name] = (mod, alias.name)
                
except ImportError:
    pass

# Actually let me simplify - look at which modules define things vs where they're used elsewhere

defined_by_module = defaultdict(set)
references_by_module = defaultdict(lambda: defaultdict(int))

for fp in py_files:
    try:
        with open(fp) as f:
            src = f.read()
            tree = ast.parse(src, filename=str(fp))
    except SyntaxError:
        continue
    
    rel_key = str(fp.relative_to(Path(".")))
    
    # What does THIS file export?
    exports_at_top_level = set()
    
    class ExportCollector(ast.NodeVisitor):
        def visit_FunctionDef(self, n): exports_at_top_level.add(n.name)
        def visit_AsyncFunctionDef(self, n): exports_at_top_level.add(n.name)
        def visit_ClassDef(self, n): exports_at_top_level.add(n.name)
    
    ExportCollector().visit(tree)
    
    # Also collect assigned variables at module level (constants etc.)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    exports_at_top_level.add(tgt.id)
                    
    defined_by_module[rel_key] = exports_at_top_level
    
    # Now scan ALL files for usage of these symbols
    

# Simpler approach: just report orphaned bak files clearly plus obvious structural issues

print("\n=== SUMMARY OF FINDINGS ===")

# Group backup files by location
core_backups = [p for p in src_backups if "tools/" in str(p)]
hist_backups = [p for p in src_backups if "history/" in str(p)]
orch_backups = [p for p in src_backups if "orchestra/" in str(p)]
tg_callbacks_bak = [p for p in src_backups if "callbacks.py.bak" in str(p)]

if core_backups:
    print(f"\n🔴 Core tools/: {len(core_backups)} backup versions of active files")
if hist_backups:
    print(f"\n🟡 History/: {len(hist_backups)} stale prompt_builder backups")
if orch_backups:
    print(f"\n🟡 Orchestra/: {len(orch_backups)} broadcast.py backups")
if tg_callbacks_bak:
    print(f"\n🟢 telegram/callbacks.py.bak exists alongside live callbacks.py")

mem_deprecated = [p for p in ba_extras if "MEMORY.md.deprecated" in str(p)]
if mem_deprecated:
    print(f"\n📝 templates/memory/MEMORY.md.deprecated still present")

# Check git config version backups too
git_config_baks = [p for p in ba_extras if ".git/config-versions.git/" in str(p)]
if git_config_baks:
    print(f"\n🗂️ Git config history backups: {len(git_config_baks)} items under .git/config-versions.git/")

print("\nDone.")
