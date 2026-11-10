#!/bin/bash
# Double-click to update CloseDesk to the newest version. Your mail and settings are kept, and the files it
# replaces are put in .previous. Then double-click CloseDesk as usual.
cd "$(dirname "$0")" || exit 1
bash ./CloseDesk.command --update
code=$?
# CloseDesk.command has already asked when it stopped on a problem.
if [ "$code" -eq 0 ] && [ -t 0 ]; then read -r -p "Press Enter to close. " _; fi
exit "$code"
