# ATV Player

Self-hosted Apple TV player for a home music + music-video library: one queue mixing FLAC
songs and music videos, with next / previous / shuffle, controllable from other devices.

## Status

Personal project, in daily use. Two parts: a tvOS app (SwiftUI + TVVLCKit, plays original files)
and a small media server for a LAN host (`server/`).

## How it is built (no Mac needed)

- `project.yml` is an [XcodeGen](https://github.com/yonaskolb/XcodeGen) spec; the Xcode
  project is generated on the runner, not committed.
- `.github/workflows/build.yml` runs on GitHub's `macos-26` runner (Xcode 26.6, tvOS 26.5
  SDK), builds an **unsigned** Release app for `appletvos`, zips it as `AtvPlayer.ipa` and
  publishes it as a GitHub release `build-<n>`.
- Signing and installation happen on the LAN with
  [ATVLoadly](https://github.com/bitxeno/atvloadly), which signs with a free Apple ID and
  re-signs before the 7-day expiry.

Deployment target: tvOS 17.0.

## ATVLoadly image

`infra/atvloadly/Dockerfile` builds a local ATVLoadly image: upstream `bitxeno/atvloadly:v0.7.1`
with `plumesign` rebuilt from `bitxeno/PlumeImpactor@v2.7.0-patch.1` plus the one-line Windows iTunes
Client-Info change from `claration/impactor#290` (Impactor v2.7.1). Apple blocked the previous
client info on 2026-10-14 (`bitxeno/atvloadly#215`). The binary itself is not committed; rebuild it
on Ubuntu 22.04 with `cargo build --release -p plumesign`. Return to the upstream image once it ships the fix.

## Media server

`server/` runs on a LAN host: scans read-only library folders into SQLite (duplicates hidden,
lossless preferred), streams original files with HTTP Range (`/media/<id>`), serves cover art /
video frame grabs (`/art/<id>`) and owns one shared queue. The Apple TV app connects as
`role=player`, the web remote at `/` as `role=remote`; all changes are pushed over WebSocket.

Site-specific settings are not in this repo: copy `server/site.example/` to `server/site/` and
put the deploy target / ATVLoadly address in `local.env` at the repo root (both git-ignored),
then deploy with `server/deploy.sh`. `server/test_e2e.py` runs an end-to-end test with a
simulated player; the other `test_*.py` start their own throwaway instance.
