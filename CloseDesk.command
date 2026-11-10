#!/bin/bash
# Double-click (macOS) or run ./CloseDesk.command (Linux): read the drop folder, let the local model read,
# write today's digest, and open the dashboard.
#
# The first run sets everything up, and every start checks the set-up and puts right what isn't (after an
# update, a moved or replaced folder, a Python that went away). No admin rights are needed at any point:
# when the computer has no Python that fits, CloseDesk downloads one of its own into your user folder.
#
#   CloseDesk.command            start CloseDesk
#   CloseDesk.command --check    say what is and isn't in order, without starting it
#   CloseDesk.command --update   fetch the newest CloseDesk (mail and settings are kept)
cd "$(dirname "$0")" || exit 1
OS="$(uname -s)"

# macOS marks a download from the internet, and then wants an administrator to approve each double-click.
# These files are yours: taking the mark off needs no rights, and the asking stops.
if [ "$OS" = "Darwin" ] && xattr -p com.apple.quarantine CloseDesk.command >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine . 2>/dev/null || true
fi

# Where CloseDesk keeps the one tool it downloads (uv, which fetches Python): in your own folder.
if [ "$OS" = "Darwin" ]; then
  TOOLS="$HOME/Library/Application Support/CloseDesk"
else
  TOOLS="${XDG_DATA_HOME:-$HOME/.local/share}/CloseDesk"
fi
UV=""

finish() {
  # Keep the window open so the message can be read (when there is a window).
  if [ -t 0 ]; then read -r -p "Press Enter to close. " _; fi
  exit "${1:-1}"
}

python_ok() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

# A Python 3.11 or newer already on this computer: prints its path. launch.py decides whether it will do.
find_python() {
  local dirs="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin"
  local name found
  if [ -n "$CLOSEDESK_PYTHON" ] && python_ok "$CLOSEDESK_PYTHON"; then
    echo "$CLOSEDESK_PYTHON"
    return 0
  fi
  for name in python3.12 python3.11 python3.13 python3.14 python3 python; do
    found="$(PATH="$dirs" command -v "$name" 2>/dev/null)" || continue
    [ -n "$found" ] || continue
    # macOS keeps a stand-in at /usr/bin/python3. Without Apple's developer tools, running it only opens a
    # window asking an administrator to install them, so it is never run unless they are there.
    if [ "$OS" = "Darwin" ] && [ "${found#/usr/bin/}" != "$found" ] && ! xcode-select -p >/dev/null 2>&1; then
      continue
    fi
    if python_ok "$found"; then
      echo "$found"
      return 0
    fi
  done
  return 1
}

# uv: already installed, already fetched, or fetched now from its GitHub releases (one file, no installer).
get_uv() {
  UV="$(command -v uv 2>/dev/null)"
  [ -n "$UV" ] && return 0
  UV="$TOOLS/uv"
  [ -x "$UV" ] && return 0
  command -v curl >/dev/null 2>&1 || { UV=""; return 1; }
  local asset tmp
  case "$OS-$(uname -m)" in
    Darwin-arm64) asset=uv-aarch64-apple-darwin ;;
    Darwin-x86_64) asset=uv-x86_64-apple-darwin ;;
    Linux-x86_64) asset=uv-x86_64-unknown-linux-gnu ;;
    Linux-aarch64 | Linux-arm64) asset=uv-aarch64-unknown-linux-gnu ;;
    *) UV=""; return 1 ;;
  esac
  tmp="$(mktemp -d)" || { UV=""; return 1; }
  if curl -fsSL --retry 2 -o "$tmp/uv.tar.gz" "${CLOSEDESK_UV_URL:-https://github.com/astral-sh/uv/releases/latest/download}/$asset.tar.gz" 2>/dev/null \
    && tar -xzf "$tmp/uv.tar.gz" -C "$tmp" 2>/dev/null && [ -f "$tmp/$asset/uv" ]; then
    mkdir -p "$TOOLS" && mv "$tmp/$asset/uv" "$UV" && chmod +x "$UV"
  fi
  rm -rf "$tmp"
  [ -x "$UV" ] || { UV=""; return 1; }
}

# CloseDesk's own Python, in your user folder: nothing is installed on the computer and no rights are asked.
make_managed() {
  get_uv || return 1
  echo "Getting CloseDesk its own Python (no admin rights needed). This takes a minute..."
  rm -rf .venv
  # The computer's own certificates, so a company proxy's is trusted as it is by the browser.
  UV_SYSTEM_CERTS=1 UV_NATIVE_TLS=1 UV_PYTHON_PREFERENCE=only-managed \
    "$UV" venv --quiet --seed --python 3.12 .venv >/dev/null 2>&1
  [ -x .venv/bin/python ] && python_ok .venv/bin/python
}

want_managed="${CLOSEDESK_OWN_PYTHON:-}"
tries=0
while [ "$tries" -lt 4 ]; do
  tries=$((tries + 1))
  if [ -x .venv/bin/python ] && python_ok .venv/bin/python; then
    PY=.venv/bin/python
  else
    # No environment, or one whose Python is gone (it was removed, upgraded, or made on another computer).
    [ -d .venv ] && export CLOSEDESK_AGAIN=1
    rm -rf .venv
    if [ -z "$want_managed" ] && PY="$(find_python)"; then
      :
    elif make_managed; then
      PY=.venv/bin/python
    elif PY="$(find_python)"; then
      export CLOSEDESK_MANAGED_TRIED=1
    else
      echo
      echo "CloseDesk couldn't get Python, which it runs on."
      echo "It downloads its own copy (no admin rights needed), and that download didn't work:"
      echo "check the internet connection, then open CloseDesk again."
      echo "If this computer blocks such downloads, ask for Python 3.12 from https://www.python.org/downloads/"
      finish 1
    fi
  fi
  [ -n "$UV" ] && export CLOSEDESK_UV="$UV"
  "$PY" scripts/launch.py "$@"
  code=$?
  case "$code" in
    0 | 130) exit 0 ;;
    75) want_managed=1; rm -rf .venv ;;                    # launch.py: this needs a Python of CloseDesk's own
    76) export CLOSEDESK_REBUILT=1; rm -rf .venv ;;        # launch.py: the environment is beyond repair, make it again
    *)
      echo
      echo "CloseDesk stopped. The message above says why."
      finish "$code"
      ;;
  esac
done
echo "CloseDesk couldn't finish setting up. Open it again; if this repeats, run: ./CloseDesk.command --check"
finish 1
