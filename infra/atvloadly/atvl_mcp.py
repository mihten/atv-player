#!/usr/bin/env python3
"""Minimal MCP streamable-HTTP client for ATVLoadly's /mcp endpoint (LAN only).
usage: atvl_mcp.py list | call <tool> '<json-args>'"""
import json, sys, urllib.request

import os
from pathlib import Path


def _local_env():
    """KEY=value lines from the git-ignored local.env at the repo root (site-specific, not committed)."""
    env = {}
    f = Path(__file__).resolve().parents[2] / "local.env"
    if f.is_file():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    return env


URL = os.environ.get("ATVL_URL") or _local_env().get("ATVL_URL") or sys.exit("set ATVL_URL (http://<host>:5533/mcp) in local.env")
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def post(payload, sid=None):
    h = dict(HDR)
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(URL, json.dumps(payload).encode(), h, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        sid = r.headers.get("Mcp-Session-Id") or sid
        body = r.read().decode()
    msgs = []
    for line in body.splitlines():
        if line.startswith("data:"):
            msgs.append(json.loads(line[5:].strip()))
    if not msgs and body.strip():
        msgs.append(json.loads(body))
    return sid, msgs


def session():
    sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "atvl_mcp", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid


def main():
    sid = session()
    if sys.argv[1] == "list":
        _, m = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
        for t in m[-1]["result"]["tools"]:
            print(f"- {t['name']}: {t.get('description','')[:110]}")
            print("    args:", json.dumps(t.get("inputSchema", {}).get("properties", {}))[:400])
    else:
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        _, m = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": sys.argv[2], "arguments": args}}, sid)
        res = m[-1].get("result", m[-1])
        if "structuredContent" in res:
            print(json.dumps(res["structuredContent"], indent=1))
        else:
            print(json.dumps(res, indent=1)[:4000])


main()
