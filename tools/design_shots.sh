#!/bin/bash
# Design screenshots on the iOS/tvOS simulators (macOS CI runner, see .github/workflows/design.yml).
#   tools/design_shots.sh <AtvPlayer.app> <AtvPlayerPhone.app> <out dir> [theme ...]
# Needs a THROWAWAY media server on 127.0.0.1:8080 with the demo library (tools/demo_library.py) and
# tools/design_driver.py setup already run. Screens are opened with the -demoRoute launch argument.
set -uo pipefail
TV_APP=$1 PHONE_APP=$2 OUT=$3
shift 3
THEMES=("$@")
[ ${#THEMES[@]} -eq 0 ] && THEMES=(classic)
mkdir -p "$OUT"
DRV=(python3 tools/design_driver.py)
TV_ID=eu.mihten.atvplayer
PHONE_ID=eu.mihten.atvplayer.phone

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

TV=$(pick tvOS "Apple TV 4K (3rd generation)" "Apple TV 4K (3rd generation) (at 1080p)" "Apple TV")
PHONE=$(pick iOS "iPhone 17 Pro" "iPhone 16 Pro" "iPhone 17" "iPhone 16")
echo "tvOS simulator: $TV   iOS simulator: $PHONE"
for dev in "$TV" "$PHONE"; do
  xcrun simctl boot "$dev" 2>/dev/null
  xcrun simctl bootstatus "$dev" -b >/dev/null
done
xcrun simctl ui "$PHONE" appearance dark
xcrun simctl status_bar "$PHONE" override --time 9:41 --batteryState charged --batteryLevel 100 \
  --wifiBars 3 --cellularMode active --cellularBars 4 2>/dev/null
xcrun simctl install "$TV" "$TV_APP"
xcrun simctl install "$PHONE" "$PHONE_APP"

launch() {  # $1 = device, $2 = bundle id, $3 = theme, $4 = route
  xcrun simctl launch --terminate-running-process "$1" "$2" \
    -serverAddress 127.0.0.1:8080 -appTheme "$3" -demoRoute "$4" >/dev/null
}

shot() {  # $1 = device, $2 = file name
  xcrun simctl io "$1" screenshot --type=png "$OUT/$2.png" >/dev/null 2>&1 && echo "  $2.png"
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
  wait $pid 2>/dev/null
  echo "  $name.mp4"
}

tv_transitions() {
  "${DRV[@]}" cmd tv next; sleep 5
  "${DRV[@]}" cmd tv next; sleep 5
  "${DRV[@]}" cmd tv prev '{"position": 0}'; sleep 5
  "${DRV[@]}" cmd tv pause; sleep 3
  "${DRV[@]}" cmd tv play; sleep 3
}

phone_transitions() {
  "${DRV[@]}" cmd phone next; sleep 5
  "${DRV[@]}" cmd phone next; sleep 5
  "${DRV[@]}" cmd phone pause; sleep 3
  "${DRV[@]}" cmd phone play; sleep 3
}

# Something in both queues first, so every screen has a Now Playing state.
"${DRV[@]}" play tv music "Harbor Lights"
"${DRV[@]}" play phone music "Low Tide"

for theme in "${THEMES[@]}"; do
  echo "== Apple TV, $theme"
  for route in home videos playlists playlist folder settings; do
    launch "$TV" "$TV_ID" "$theme" "$route"
    sleep 8
    shot "$TV" "tv-$theme-$route"
  done
  launch "$TV" "$TV_ID" "$theme" player
  sleep 9
  shot "$TV" "tv-$theme-player"
  record "$TV" "tv-$theme-transitions" tv_transitions
  shot "$TV" "tv-$theme-player-2"
  "${DRV[@]}" play tv videos "Afterglow"
  sleep 4
  shot "$TV" "tv-$theme-video"
  "${DRV[@]}" play tv music "Harbor Lights"
  sleep 2

  echo "== iPhone, $theme"
  for route in library videos playlists playlist queue settings; do
    launch "$PHONE" "$PHONE_ID" "$theme" "$route"
    sleep 7
    shot "$PHONE" "phone-$theme-$route"
  done
  launch "$PHONE" "$PHONE_ID" "$theme" player
  sleep 8
  shot "$PHONE" "phone-$theme-player"
  record "$PHONE" "phone-$theme-transitions" phone_transitions
  "${DRV[@]}" play phone videos "Undertow"
  sleep 6
  shot "$PHONE" "phone-$theme-video"
  "${DRV[@]}" play phone music "Low Tide"
  sleep 2
done
ls -la "$OUT"
