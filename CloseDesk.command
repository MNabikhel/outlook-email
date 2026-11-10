#!/bin/bash
# Double-click (macOS) or run ./CloseDesk.command (Linux): read the drop folder, let the local model read,
# write today's digest, and open the dashboard.
#
# The first run sets everything up, and every start checks the set-up and puts right what isn't (after an
# update, a moved or replaced folder, a Python that went away).
#
# Python 3.11 or newer is the one thing CloseDesk needs on the computer. The Python that is there is used; none
# is ever installed unasked. Where there is none, CloseDesk says so and how to get it.
#
#   CloseDesk.command               start CloseDesk
#   CloseDesk.command --check       say what is and isn't in order, without starting it
#   CloseDesk.command --update      fetch the newest CloseDesk (mail and settings are kept)
#   CloseDesk.command --get-python  only when asked: download a Python for CloseDesk alone into your user
#                                   folder (no admin rights; nothing is installed on the computer), and use it
cd "$(dirname "$0")" || exit 1
OS="$(uname -s)"

# macOS marks a download from the internet, and then wants an administrator to approve each double-click.
# These files are yours: taking the mark off needs no rights, and the asking stops.
if [ "$OS" = "Darwin" ] && xattr -p com.apple.quarantine CloseDesk.command >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine . 2>/dev/null || true
fi

# Where --get-python keeps the one tool it downloads (uv, which fetches Python): in your own folder.
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

# A Python 3.11 or newer already on this computer: prints its path.
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

# --get-python: a Python for CloseDesk alone, in your user folder. Nothing is installed on the computer and no
# rights are asked. Never done unasked.
make_managed() {
  get_uv || return 1
  echo "Downloading a Python for CloseDesk into your user folder (no admin rights needed). This takes a minute..."
  rm -rf .venv
  # The computer's own certificates, so a company proxy's is trusted as it is by the browser.
  UV_SYSTEM_CERTS=1 UV_NATIVE_TLS=1 UV_PYTHON_PREFERENCE=only-managed \
    "$UV" venv --quiet --seed --python 3.12 .venv >/dev/null 2>&1
  [ -x .venv/bin/python ] && python_ok .venv/bin/python
}

# --get-python is for this script, not for CloseDesk: taken out of what is passed on.
get_python=""
args=()
for arg in "$@"; do
  if [ "$arg" = "--get-python" ]; then get_python=1; else args+=("$arg"); fi
done
# Asked for: the environment is made again, with the downloaded Python.
[ -n "$get_python" ] && rm -rf .venv

no_python() {
  echo
  echo "CloseDesk needs Python 3.11 or newer (3.12 is the best choice), and this computer has none it can use."
  echo
  echo "Install Python from https://www.python.org/downloads/ and open CloseDesk again."
  if [ "$OS" = "Darwin" ]; then
    echo
    echo "Python's installer asks for an administrator on a Mac. If you don't have one, CloseDesk can download"
    echo "a copy of Python for itself into your own folder instead (nothing is installed on the computer)."
    echo "Paste this into Terminal:"
    echo
    echo "  bash \"$PWD/CloseDesk.command\" --get-python"
  else
    echo
    echo "Without the rights to install it, CloseDesk can download a copy for itself into your own folder:"
    echo
    echo "  bash \"$PWD/CloseDesk.command\" --get-python"
  fi
  finish 1
}

tries=0
while [ "$tries" -lt 3 ]; do
  tries=$((tries + 1))
  if [ -x .venv/bin/python ] && python_ok .venv/bin/python; then
    PY=.venv/bin/python
  elif [ -n "$get_python" ]; then
    if ! make_managed; then
      echo
      echo "The download didn't work. Check the internet connection and try again."
      echo "Nothing was changed, except that CloseDesk will set itself up again on its next start."
      finish 1
    fi
    PY=.venv/bin/python
  else
    # No environment, or one whose Python is gone (it was removed, upgraded, or made on another computer).
    [ -d .venv ] && export CLOSEDESK_AGAIN=1
    rm -rf .venv
    PY="$(find_python)" || no_python
  fi
  [ -n "$UV" ] && export CLOSEDESK_UV="$UV"
  "$PY" scripts/launch.py ${args[@]+"${args[@]}"}
  code=$?
  case "$code" in
    0 | 130) exit 0 ;;
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
