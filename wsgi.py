"""Entry point for a hosted Edgeboard.

    waitress-serve --host 0.0.0.0 --port $PORT wsgi:app

The local dashboard (python main.py dashboard) is unchanged. A hosted copy
runs the same app; the hourly sync and auto-pick worker starts with it unless
EDGEBOARD_SCHEDULER=0, which is how a second web process avoids running a
duplicate worker.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from src.web.app import create_app  # noqa: E402

app = create_app()

if os.getenv("EDGEBOARD_SCHEDULER", "1") != "0":
    from src.picker.scheduler import start as start_scheduler

    start_scheduler()
