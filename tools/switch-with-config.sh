#!/bin/bash
set -euo pipefail

SWITCH_TO="${1:-}"
[[ -z "$SWITCH_TO" ]] && { echo "Usage: $0 <branch-or-tag-or-hash>"; exit 1; }

SRC=/root/nanobot-src
CFG=/root/nanobot-src/.git/config-versions.git

echo "═══ Switching source + matching config ═══"
echo "Target: $SWITCH_TO"

# Step 1: Resolve what the SOURCE will become AFTER checkout
cd "$SRC"
ORIGINAL=$(git rev-parse HEAD)
RESOLVED=$(git rev-parse "$SWITCH_TO^{commit}" 2>/dev/null || echo "")
if [[ -z "$RESOLVED" ]]; then
    echo "❌ Cannot resolve '$SWITCH_TO' in source repo"
    exit 1
fi
SHORT_RESOLVED="${RESOLVED:0:36}"

echo "Resolved SHA: $RESOLVED"

# Step 2: Find corresponding config tag in CFG repo
cd "$CFG"
BEST_TAG=""
BEST_SCORE=-1

while IFS= read -r TAG; do
    HASH_IN_TAG="${TAG##*-}"
    SHORT_SRC="${SHORT_RESOLVED:0:21}"
    
    # Count matching prefix chars (exact match scores higher than case-fold)
    MLEN=0 MAXLEN=${#HASH_IN_TAG}
    while ((MLEN < MAXLEN && MLEN < ${#SHORT_SRC})); do
        CA="${HASH_IN_TAG:$MLEN:1}" CB="${SHORT_SRC:$MLEN:1}"
        LC_A=$(printf '%s' "$CA" | tr '[:upper:]' '[:lower:]')
        LC_B=$(printf '%s' "$CB" | tr '[:upper:]' '[:lower:]')
        if [[ "$LC_A" != "$LC_B" ]]; then break; fi
        ((MLEN++))
    done
    
    SCORE=$((MLEN * 2))
    
    if [[ $SCORE -gt $BEST_SCORE ]]; then
        BEST_SCORE=$SCORE
        BEST_TAG=$TAG
    fi
done < <(git tag -l 'cfg/**' 2>/dev/null || true)

if [[ -n "$BEST_TAG" ]]; then
    CONFIG_CHECKOUT=$(git rev-parse "$BEST_TAG^{commit}")
    echo "Config matched via tag: $BEST_TAG -> ${CONFIG_CHECKOUT:0:43}"
else
    CONFIG_CHECKOUT=""
    echo "⚠️  No matching config snapshot found. Will keep current config."
fi

# Step 3: Execute switches (first dry-run info)
echo ""
echo "Actions to perform:"
echo "  ① Source checkout : $ORIGINAL -> $RESOLVED ($SWITCH_TO)"
if [[ -n "$CONFIG_CHECKOUT" ]]; then
    CURRENT_CONF=$(cd "$CFG" && git rev-parse HEAD)
    echo "  ② Config checkout : ${CURRENT_CONF} -> ${CONFIG_CHECKOUT}"
fi

# Confirm unless piped/no-TTY  
#if [[ ! -t 0 ]]; then PROCEED=y; else read -rp "Proceed? [Y/n] " PROCEED; fi  
PROCEED=y  
[[ "${PROCEED:-y}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# Actually switch config FIRST (so source change doesn't affect us mid-way)
if [[ -n "$CONFIG_CHECKOUT" ]]; then
    cd "$CFG"
    git checkout --force "$BEST_TAG" >/dev/null 2>&1
    
    # Rsync checked-out config BACK to ~/.nanobot/
    EXCL=('--exclude=.git')
    if [[ -f .gitignore ]]; then
        while IFS= read -r line; do EXCL+=(--exclude="$line"); done <.gitignore
    fi
    
    rsync -aqz --delete "${EXCL[@]}" "./" "${HOME}/.nanobot/"
    
    echo "✅ Config restored from snapshot [$BEST_TAG]"
fi

# Now switch source code  
cd "$SRC"
git checkout --force "$SWITCH_TO" >/dev/null 2>&1

NEW_STATE=$(git rev-parse HEAD)
echo "✅ Source switched to ${NEW_STATE} ($SWITCH_TO)"

echo ""
echo "Done! Restart nanobot now?"
