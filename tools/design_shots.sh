#!/bin/bash
# Design screenshots on the iOS/tvOS simulators (macOS CI runner, see .github/workflows/design.yml).
#   tools/design_shots.sh <AtvPlayer.app> <AtvPlayerPhone.app> <out dir> <platforms: tv,phone> [theme ...]
# Needs a THROWAWAY media server on 127.0.0.1:8080 with the demo library (tools/demo_library.py) and
# tools/design_driver.py setup already run. Screens are opened with the -demoRoute launch argument.
# One simulator at a time (two big simulators froze the runner), every simctl call has a watchdog.
set -uo pipefail
TV_APP=$1 PHONE_APP=$2 OUT=$3 PLATFORMS=$4
EXTRA_ROUTES=${EXTRA_ROUTES:-}  # env: more -demoRoute screens per theme (e.g. "theme lyrics")
shift 4
THEMES=("$@")
[ ${#THEMES[@]} -eq 0 ] && THEMES=(aurora)
mkdir -p "$OUT"
DRV=(python3 tools/design_driver.py)
TV_ID=eu.mihten.atvplayer
PHONE_ID=eu.mihten.atvplayer.phone

t() {  # t <seconds> <command...>: run with a watchdog (perl alarm; macOS has no timeout(1))
  local secs=$1
  shift
  perl -e 'alarm shift; exec @ARGV' "$secs" "$@"
  local rc=$?
  [ $rc -ne 0 ] && echo "  ! rc=$rc: $*" >&2
  return 0
}

pick() {  # $1 = platform word in the runtime id (tvOS / iOS), $2.. = preferred device names
  xcrun simctl list devices available -j | python3 -c '
import json, sys
platform, prefs = sys.argv[1], sys.argv[2:]
data = json.load(sys.stdin)["devices"]
runtimes = sorted((r for r in data if f".{platform}-" in r), key=lambda r: [int(x) for x in r.split("-")[1:] if x.isdigit()])
for runtime in reversed(runtimes):
    devices = data[runtime]
    for pref in prefs:
        for d in devices:
            if d["name"] == pref:
                print(d["udid"]); sys.exit()
    if devices:
        print(devices[0]["udid"]); sys.exit()
' "$@"
}

boot() {  # $1 = udid
  local start=$SECONDS
  t 400 xcrun simctl boot "$1"
  t 400 xcrun simctl bootstatus "$1" -b >/dev/null
  echo "  booted $1 in $((SECONDS - start)) s"
}

launch() {  # $1 = device, $2 = bundle id, $3 = theme, $4 = route
  t 90 xcrun simctl terminate "$1" "$2" 2>/dev/null
  t 90 xcrun simctl launch "$1" "$2" -serverAddress 127.0.0.1:8080 -appTheme "$3" -demoRoute "$4" >/dev/null
}

shot() {  # $1 = device, $2 = file name
  t 60 xcrun simctl io "$1" screenshot --type=png "$OUT/$2.png" >/dev/null 2>&1
  [ -f "$OUT/$2.png" ] && echo "  $2.png  (t=$SECONDS s)"
}

record() {  # $1 = device, $2 = file name, rest = command run while recording
  local dev=$1 name=$2
  shift 2
  xcrun simctl io "$dev" recordVideo --codec=h264 --force "$OUT/$name.mp4" >/dev/null 2>&1 &
  local pid=$!
  sleep 2
  "$@"
  sleep 1
  kill -INT $pid 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 $pid 2>/dev/null || break; sleep 1; done
  kill -9 $pid 2>/dev/null
  wait $pid 2>/dev/null
  echo "  $name.mp4"
}

play() {  # $1 = session: the server loads the queue paused after a relaunch
  t 30 "${DRV[@]}" cmd "$1" play
}

open_player() {  # $1 = device, $2 = bundle id, $3 = theme, $4 = session: home screen, the player opens after ~5 s
  launch "$1" "$2" "$3" open-player
  play "$4"
  sleep 12
}

tv_transitions() {
  t 30 "${DRV[@]}" cmd tv next; sleep 5
  t 30 "${DRV[@]}" cmd tv next; sleep 5
  t 30 "${DRV[@]}" cmd tv prev '{"position": 0}'; sleep 5
  t 30 "${DRV[@]}" cmd tv pause; sleep 3
  t 30 "${DRV[@]}" cmd tv play; sleep 3
}

phone_transitions() {
  t 30 "${DRV[@]}" cmd phone next; sleep 5
  t 30 "${DRV[@]}" cmd phone next; sleep 5
  t 30 "${DRV[@]}" cmd phone pause; sleep 3
  t 30 "${DRV[@]}" cmd phone play; sleep 3
}

if [[ $PLATFORMS == *tv* ]]; then
  TV=$(pick tvOS "Apple TV 4K (3rd generation) (at 1080p)" "Apple TV 4K (3rd generation)" "Apple TV")
  echo "== tvOS simulator $TV"
  boot "$TV"
  t 120 xcrun simctl install "$TV" "$TV_APP"
  t 30 "${DRV[@]}" play tv music "Harbor Lights"
  for theme in "${THEMES[@]}"; do
    echo "== Apple TV, $theme"
    for route in home playlist folder settings $EXTRA_ROUTES; do
      launch "$TV" "$TV_ID" "$theme" "$route"
      play tv
      sleep 7
      shot "$TV" "tv-$theme-$route"
    done
    record "$TV" "tv-$theme-open" open_player "$TV" "$TV_ID" "$theme" tv
    launch "$TV" "$TV_ID" "$theme" player
    play tv
    sleep 8
    shot "$TV" "tv-$theme-player"
    record "$TV" "tv-$theme-transitions" tv_transitions
    shot "$TV" "tv-$theme-player-later"
    t 30 "${DRV[@]}" play tv videos "Afterglow"
    sleep 3
    shot "$TV" "tv-$theme-video"
    t 30 "${DRV[@]}" play tv music "Harbor Lights"
    sleep 2
  done
  t 120 xcrun simctl shutdown "$TV"
fi

if [[ $PLATFORMS == *phone* ]]; then
  PHONE=$(pick iOS "iPhone 17 Pro" "iPhone 16 Pro" "iPhone 17" "iPhone 16")
  echo "== iOS simulator $PHONE"
  boot "$PHONE"
  t 30 xcrun simctl ui "$PHONE" appearance dark
  t 30 xcrun simctl status_bar "$PHONE" override --time 9:41 --batteryState charged --batteryLevel 100 \
    --wifiBars 3 --cellularMode active --cellularBars 4
  t 120 xcrun simctl install "$PHONE" "$PHONE_APP"
  t 30 "${DRV[@]}" play phone music "Low Tide"
  for theme in "${THEMES[@]}"; do
    echo "== iPhone, $theme"
    for route in library playlist queue settings $EXTRA_ROUTES; do
      launch "$PHONE" "$PHONE_ID" "$theme" "$route"
      play phone
      sleep 6
      shot "$PHONE" "phone-$theme-$route"
    done
    record "$PHONE" "phone-$theme-open" open_player "$PHONE" "$PHONE_ID" "$theme" phone
    launch "$PHONE" "$PHONE_ID" "$theme" player
    play phone
    sleep 7
    shot "$PHONE" "phone-$theme-player"
    record "$PHONE" "phone-$theme-transitions" phone_transitions
    t 30 "${DRV[@]}" play phone videos "Undertow"
    sleep 6
    shot "$PHONE" "phone-$theme-video"
    t 30 "${DRV[@]}" play phone music "Low Tide"
    sleep 2
  done
  t 120 xcrun simctl shutdown "$PHONE"
fi
ls -la "$OUT"
