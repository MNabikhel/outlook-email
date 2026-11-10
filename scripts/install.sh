#!/bin/bash
# Install CloseDesk from Terminal, or update the copy already there. No admin rights are needed, and macOS
# asks none of its "can't be opened" questions (a download made this way isn't marked as from the internet):
#
#   curl -fsSL https://raw.githubusercontent.com/MNabikhel/outlook-email/main/scripts/install.sh | bash
#
# CloseDesk goes in ~/CloseDesk (or the folder named in CLOSEDESK_HOME) and is started. Run again later, this
# updates that copy; the mail and settings in it are kept.
set -u
DEST="${CLOSEDESK_HOME:-$HOME/CloseDesk}"
URL="${CLOSEDESK_DOWNLOAD_URL:-https://github.com/MNabikhel/outlook-email/archive/refs/heads/main.tar.gz}"

# Started through a pipe (curl | bash): give CloseDesk the keyboard back, so Ctrl+C and its questions work.
start() {
  if [ -t 0 ] || ! (exec </dev/tty) 2>/dev/null; then
    exec bash "$DEST/CloseDesk.command" "$@"
  fi
  exec bash "$DEST/CloseDesk.command" "$@" </dev/tty
}

if [ -f "$DEST/CloseDesk.command" ]; then
  echo "CloseDesk is already in $DEST: updating it."
  start --update
fi
if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
  echo "$DEST already holds something else. Move it away, or name another folder: CLOSEDESK_HOME=~/SomeFolder"
  exit 1
fi

echo "Downloading CloseDesk into $DEST ..."
tmp="$(mktemp -d)" || exit 1
if ! curl -fsSL --retry 2 "$URL" | tar -xz -C "$tmp"; then
  rm -rf "$tmp"
  echo "The download didn't work. Check the internet connection and run this again."
  exit 1
fi
src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ ! -f "$src/CloseDesk.command" ]; then
  rm -rf "$tmp"
  echo "The download isn't CloseDesk. Nothing was installed."
  exit 1
fi
rmdir "$DEST" 2>/dev/null
mkdir -p "$(dirname "$DEST")" && mv "$src" "$DEST" || { rm -rf "$tmp"; echo "Couldn't put CloseDesk in $DEST."; exit 1; }
rm -rf "$tmp"
chmod +x "$DEST/CloseDesk.command" "$DEST/CloseDesk-Update.command" "$DEST/scripts/"*.sh 2>/dev/null
echo "CloseDesk is in $DEST. From now on, double-click CloseDesk.command there."
start
