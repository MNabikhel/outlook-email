"""``python -m controller_inbox``. In the CloseDesk app (the shortcut's window, the CloseDesk it starts), what is said is
added at the end of the app's log first; and when the window's own start can't even load CloseDesk (a part of Python
a company's policy blocks, say), that is said in a message box, since there is no console to show it (app_log.py)."""

import sys

from controller_inbox import app_log

if __name__ == "__main__":
    app_log.write_at_end()
    try:
        from controller_inbox.cli import main
    except Exception:
        if sys.argv[1:2] != ["app"]:
            raise
        raise SystemExit(app_log.cant_open()) from None
    try:
        code = main()
    except SystemExit as done:
        code = done.code
    # Done: what still runs beside it (a reply a model is still writing, its page gone) doesn't keep CloseDesk open.
    from controller_inbox.cli import leave_within

    leave_within(code)
    raise SystemExit(code)
