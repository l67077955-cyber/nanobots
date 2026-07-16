#!/bin/bash
# Count core agent lines (excluding channels/, cli/, providers/ adapters)
cd "$(dirname "$0")" || exit 1

echo "nanobot core agent line count"
echo "================================"
echo ""
echo "  Core = groupchat/ (GroupChatEngine multi-agent collaboration)"
echo ""

for dir in groupchat/runtime groupchat/context groupchat/display bus config cron heartbeat session utils tools; do
  count=$(find "nanobot/$dir" -name "*.py" 2>/dev/null -exec cat {} + | wc -l)
  printf "  %-24s %6s lines\n" "$dir/" "$count"
done

root=$(cat nanobot/__init__.py nanobot/__main__.py nanobot/agent/__init__.py 2>/dev/null | wc -l)
printf "  %-24s %6s lines\n" "(root+agent entry)" "$root"

echo ""
total=$(find nanobot/groupchat nanobot/bus nanobot/config nanobot/cron nanobot/heartbeat nanobot/session nanobot/utils nanobot/tools nanobot/command -name "*.py" 2>/dev/null | xargs cat 2>/dev/null | wc -l)
echo "  Core total (groupchat+bus+tools+…):  $total lines"
echo ""
echo "  (excludes: channels/, cli/, providers/, skills/)"
