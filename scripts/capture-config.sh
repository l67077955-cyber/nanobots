#!/bin/bash
set -euo pipefail

# Capture current ~/.nanobot/ configuration,
# link it to current source commit via shared tag.
#
# Usage: ./tools/capture-config.sh [custom-label]
#   label example: "before-changing-provider-order"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO="/root/nanobot-src"
CONFIG_REPO="${SOURCE_REPO}/.git/config-versions.git"

LABEL="${1:-}"
TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

cd "$SOURCE_REPO"

if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    echo "❌ Not inside a git repo."
    exit 1
fi

SRC_COMMIT_FULL=$(git rev-parse HEAD)
SRC_SHORT=${SRC_COMMIT_FULL:0:21}
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "(detached)")

# Check for uncommitted changes
DIRTY=""
if ! git diff --quiet HEAD 2>/dev/null; then
    DIRTY="-dirty"
fi

# Tags for config repo:
TAG_MAP_BRANCH="cfg/${BRANCH}/${SRC_SHORT}${DIRTY}"
TAG_LABELED=""
if [[ -n "$LABEL" ]]; then
    TAG_LABELED="cfg/label/${LABEL}-${SRC_SHORT}"
fi

echo ""
echo "═══ Capturing Configuration Snapshot ═══"
echo "Source : $SRC_COMMIT_FULL ($BRANCH)"
echo "Label  : ${LABEL:-"(none)"}"
echo "═══════════════════════════════════════════"

# --- Sync latest ~/.nanobot/ into config repo ---
cd "$CONFIG_REPO"

EXCLUDE_PATTERNS=(
    '.git'
    'backups/*'
    'collab-sessions*/'
    'request_logs/*'
    'workspace/*'
    'logs/*'
    '__pycache__'
    'node_modules'
)

RSYNC_EXCLUDES=()
for pat in "${EXCLUDE_PATTERNS[@]}"; do RSYNC_EXCLUDES+=(--exclude="$pat"); done

rsync -aqz --delete "${RSYNC_EXCLUDES[@]}" \
    "${HOME}/.nanobot/" .

# Stage everything except ignored patterns already covered by .gitignore
git add -A .

if git diff --cached --quiet; then
    echo "ℹ️  No configuration changes detected."
else
    COMMIT_MSG="snapshot @ ${SRC_COMMIT_FULL}

Branch: ${BRANCH}
Timestamp: ${TIMESTAMP}
Src-Commit-Hash-Full: ${SRC_COMMIT_FULL}
Src-Branch: ${BRANCH}
Hostname: $(hostname)
By-Cmd: capture-config.sh${LABEL:+ Label:$LABEL}"
    
    git commit -m "$COMMIT_MSG"
fi

# Apply primary tag (based on branch + short hash)
git tag -f "$TAG_MAP_BRANCH" HEAD

# If labeled, also apply human-readable alias tag
if [[ -n "$TAG_LABELED" ]]; then
    git tag -f "$TAG_LABELED" HEAD
fi

echo ""
echo "✅ Done!"
echo "   Config Repo Commit : $(git rev-parse HEAD)"
echo "   Primary Tag        : $TAG_MAP_BRANCH"
[[ -n "$TAG_LABELED" ]] && echo "   Alias Tag          : $TAG_LABELED"
echo ""
echo "To view available snapshots later:"
echo "   cd /root/nanobot-src && git --git-dir=.git/config-versions.git tag | grep cfg/"