#!/usr/bin/env python3
"""LTVPlayer on the atv-audio headphones, server side (0.33.0): every check of test_mtv_sink.py (the TV Player's), run
for session "ltv" (own port 18104), plus LTVPlayer's codec switch.
    python3 test_ltv_sink.py   (needs ffmpeg + the server's deps)
"""
import os
import runpy
from pathlib import Path

os.environ["SINK_SCREEN"] = "ltv"
runpy.run_path(str(Path(__file__).resolve().parent / "test_mtv_sink.py"), run_name="__main__")
