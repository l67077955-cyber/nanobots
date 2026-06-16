#!/bin/bash
# tag-cleanup.sh — audit and optionally clean nanobot-src git tags
#
# Default: dry-run only (prints plan, changes nothing).
# Use --execute to apply deletions. Use --tag-stable to create v-stable-YYYYMMDD.
#
# Examples:
#   ./tools/tag-cleanup.sh                    # full audit (dry-run)
#   ./tools/tag-cleanup.sh --duplicates-only  # only show duplicate tags
#   ./tools/tag-cleanup.sh --execute          # delete approved duplicates
#   ./tools/tag-cleanup.sh --tag-stable       # dry-run: propose v-stable-YYYYMMDD on HEAD
#   ./tools/tag-cleanup.sh --tag-stable --execute --date 20260616
#
set -euo pipefail

SRC="${NANOBOT_SRC:-/root/nanobot-src}"
cd "$SRC"

EXECUTE=false
DUPLICATES_ONLY=false
TAG_STABLE=false
TAG_DATE=""
INCLUDE_BACKUPS=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) EXECUTE=true; shift ;;
    --duplicates-only) DUPLICATES_ONLY=true; shift ;;
    --tag-stable) TAG_STABLE=true; shift ;;
    --date) TAG_DATE="${2:-}"; shift 2 ;;
    --include-backups) INCLUDE_BACKUPS=true; shift ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
  esac
done

# ── Tags to delete: redundant aliases on the same commit ──────────────────
# Format: "keep|delete|delete|..."
DUPLICATE_GROUPS=(
  "v0.1.5|015|v0.1.5-stable"
  "v0.1.6|v0.1.6-stable"
  "stable-pre-config-change|stable/pre-history-settings-defaults-update"
)

# ── Tags off current stable lineage (informational, not auto-deleted) ─────
OBSOLETE_TAGS=(
  v-stable-20260517
  v-stable-20260517-plus-fixes
  v-stable-20260605
  v0.2.0
  v0.2.1
)

# ── Backup tags — only deleted with --include-backups --execute ───────────
BACKUP_TAGS=(
  backup-before-27-rollback-20260601
  backup-before-rollback-20260530-113325
  backup-before-rollback-20260608-165600
  backup-dev-before-reset-20260602
  v-backup-20260606-dev-7b2326ed
)

header() { echo ""; echo "═══ $1 ═══"; }

is_ancestor() {
  local tag="$1" ref="${2:-HEAD}"
  git merge-base --is-ancestor "$tag" "$ref" 2>/dev/null
}

tag_info() {
  local t="$1"
  if ! git rev-parse "$t" >/dev/null 2>&1; then
    echo "  (missing)"
    return
  fi
  local short msg date on_lineage
  short=$(git rev-parse --short "$t^{commit}" 2>/dev/null)
  msg=$(git log -1 --format='%s' "$t^{commit}" 2>/dev/null | cut -c1-60)
  date=$(git log -1 --format='%ci' "$t^{commit}" 2>/dev/null | cut -c1-10)
  if is_ancestor "$t"; then
    on_lineage="✅ on stable lineage"
  else
    on_lineage="⚠️  off stable lineage"
  fi
  echo "  $t → $short ($date) $on_lineage"
  echo "       $msg"
}

delete_tag() {
  local t="$1"
  if ! git rev-parse "$t" >/dev/null 2>&1; then
    echo "  skip $t (not found)"
    return
  fi
  if $EXECUTE; then
    git tag -d "$t"
    echo "  deleted $t"
  else
    echo "  would delete $t → $(git rev-parse --short "$t^{commit}")"
  fi
}

# ── Report ────────────────────────────────────────────────────────────────
header "Repository"
echo "  path:   $SRC"
echo "  HEAD:   $(git rev-parse --short HEAD) $(git log -1 --format='%s' HEAD | cut -c1-50)"
echo "  branch: $(git branch --show-current)"
echo "  tags:   $(git tag -l | wc -l) local, $(git ls-remote --tags nanobots 2>/dev/null | wc -l) remote"

if $TAG_STABLE; then
  header "Propose stable tag"
  if [[ -z "$TAG_DATE" ]]; then
    TAG_DATE=$(date +%Y%m%d)
  fi
  NEW_TAG="v-stable-${TAG_DATE}"
  if git rev-parse "$NEW_TAG" >/dev/null 2>&1; then
    echo "  ❌ $NEW_TAG already exists → $(git rev-parse --short "$NEW_TAG^{commit}")"
    exit 1
  fi
  echo "  would create: $NEW_TAG → $(git rev-parse --short HEAD)"
  if $EXECUTE; then
    git tag "$NEW_TAG"
    echo "  ✅ created $NEW_TAG"
  fi
fi

header "Duplicate tags (auto-clean candidates)"
for group in "${DUPLICATE_GROUPS[@]}"; do
  IFS='|' read -ra parts <<< "$group"
  keep="${parts[0]}"
  echo ""
  echo "  keep: $keep"
  tag_info "$keep"
  for ((i= 1; i < ${#parts[@]}; i++)); do
    dup="${parts[i]}"
    if git rev-parse "$keep^{commit}" "$dup^{commit}" >/dev/null 2>&1; then
      khash=$(git rev-parse "$keep^{commit}")
      dhash=$(git rev-parse "$dup^{commit}")
      if [[ "$khash" != "$dhash" ]]; then
        echo "  ⚠️  $dup points to different commit — skip"
        tag_info "$dup"
        continue
      fi
    fi
    delete_tag "$dup"
  done
done

if $DUPLICATES_ONLY; then
  echo ""
  echo "Dry-run complete. Re-run with --execute to apply duplicate deletions."
  exit 0
fi

header "Nearest tags to HEAD"
git tag -l | while read -r t; do
  ahead=$(git rev-list --count "$t"..HEAD 2>/dev/null || echo 9999)
  echo "$ahead $t"
done | sort -n | head -8 | while read -r ahead t; do
  echo "  +$ahead commits | $t → $(git rev-parse --short "$t^{commit}" 2>/dev/null)"
done

header "Obsolete tags (off stable lineage — keep for reference, do not auto-delete)"
for t in "${OBSOLETE_TAGS[@]}"; do
  tag_info "$t"
done

header "Backup tags (delete only with --include-backups --execute)"
for t in "${BACKUP_TAGS[@]}"; do
  tag_info "$t"
  if $INCLUDE_BACKUPS; then
    delete_tag "$t"
  fi
done

header "2026-06-16 feature milestones (on stable lineage)"
for t in \
  prompt-config-overhaul-20260616 \
  prompt-collapse-configurable-20260616 \
  broadcast-ux-polish-20260616 \
  broadcast-active-state-clarify-20260616 \
  broadcast-clean-warning-20260616
do
  tag_info "$t"
done

header "Summary"
if $EXECUTE; then
  echo "  Mode: EXECUTE — changes applied."
  echo "  Remaining tags: $(git tag -l | wc -l)"
else
  echo "  Mode: DRY-RUN — no changes made."
  echo ""
  echo "  Next steps:"
  echo "    ./tools/tag-cleanup.sh --execute                  # delete duplicate aliases"
  echo "    ./tools/tag-cleanup.sh --tag-stable --date 20260616 --execute  # tag current HEAD"
  echo "    ./tools/tag-cleanup.sh --include-backups --execute             # also remove backup tags"
fi