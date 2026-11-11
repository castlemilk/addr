#!/bin/sh
# Rebuild the web bundle safely: pause any MPS fine-tune (SIGSTOP/SIGCONT) so the build has the machine, refuse to run
# beside scene extraction, restart the launchd service only on success (a killed `next build` wipes .next and leaves
# the running server unable to serve its JavaScript chunks). Usage: task web:rebuild  (or sh scripts/rebuild-web.sh)
cd /Users/benebsworth/projects/addr
LOG=.run/build-web-$(date -u +%Y%m%dT%H%M%SZ).log
if pgrep -f "scene_extract --scene-id" >/dev/null; then echo "scene extraction is running; wait for it before building" >&2; exit 2; fi
FT=$(pgrep -f "finetune_mps" | head -1)
[ -n "$FT" ] && kill -STOP "$FT" && echo "paused fine-tune $FT" > "$LOG"
trap '[ -n "$FT" ] && kill -CONT "$FT" && echo "resumed fine-tune $FT" >> "$LOG"' EXIT
NODE_OPTIONS=--max-old-space-size=6144 pnpm build >> "$LOG" 2>&1; code=$?
echo "build exit=$code" >> "$LOG"
if [ "$code" = "0" ] && [ -f .next/BUILD_ID ]; then
  launchctl kickstart -k gui/$(id -u)/com.geoau.addr.web
  for i in $(seq 1 18); do sleep 10; c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "http://localhost:3008/scenes?scene=house-25822&status=all"); echo "try $i http $c" >> "$LOG"; [ "$c" = "200" ] && break; done
  curl -s --max-time 120 "http://localhost:3008/api/scenes?id=house-25822" | python3 -c "import json,sys; d=json.load(sys.stdin); print('stale', d.get('stale'), 'recovery', 'present' if d.get('recovery') else None, '|', d.get('recovery_reason'))" >> "$LOG" 2>&1
fi
tail -4 "$LOG"
