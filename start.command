#!/bin/zsh
# Bấm đúp file này trong Finder để mở Video Shuffler
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
if [ ! -d .venv ]; then
  uv venv --python 3.13 -q
fi
.venv/bin/python -c "import webview" 2>/dev/null || uv pip install -q -e .
.venv/bin/vshuffle-ui
