"""For the CloseDesk window's check on Linux (``tests/launcher_smoke.py --window``): with this folder on PYTHONPATH and
CLOSEDESK_STAND_IN_WINDOW=1, the window opens here too, as the stand-in pywebview beside this file."""

import os

if os.environ.get("CLOSEDESK_STAND_IN_WINDOW") == "1":
    try:
        from controller_inbox import app_window
    except ImportError:  # a Python without CloseDesk (the computer's, making the environment)
        pass
    else:
        app_window.why_no_window = lambda **_: None
