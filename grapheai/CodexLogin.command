#!/bin/bash
# GrapheAI - sign OpenAI's Codex CLI in with your ChatGPT account (Plus/Pro),
# installing the CLI first if it is missing. Then every GrapheAI app can use
# the ChatGPT-subscription backend (GPT-5.6 family, GPT-6-Astra, ...).
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.codex/bin:$HOME/.npm-global/bin:$PATH"
for d in "$HOME"/.nvm/versions/node/*/bin; do [ -d "$d" ] && PATH="$d:$PATH"; done
export PATH

find_codex() { command -v codex 2>/dev/null; }
CODEX=$(find_codex)
if [ -z "$CODEX" ]; then
  echo "Codex CLI is not installed yet."
  if command -v brew >/dev/null 2>&1; then
    read -r -p "Install it now with Homebrew (brew install --cask codex)? [y/N] " a
    [ "$a" = "y" ] || [ "$a" = "Y" ] && brew install --cask codex
  elif command -v npm >/dev/null 2>&1; then
    read -r -p "Install it now with npm (npm install -g @openai/codex)? [y/N] " a
    [ "$a" = "y" ] || [ "$a" = "Y" ] && npm install -g @openai/codex
  else
    echo "Install Homebrew (https://brew.sh) or Node (https://nodejs.org) first, then run:"
    echo "    brew install --cask codex      or      npm install -g @openai/codex"
  fi
  CODEX=$(find_codex)
fi
if [ -z "$CODEX" ]; then
  echo "Codex CLI still not found - see SYSTEM_GUIDE.md › Claude access."
  read -r -p "Press Return to close." _
  exit 1
fi
echo "Using $CODEX ($("$CODEX" --version 2>/dev/null))"
if "$CODEX" login status >/dev/null 2>&1; then
  echo "Already signed in: $("$CODEX" login status 2>&1 | head -1)"
  read -r -p "Sign in again with a different account? [y/N] " a
  if [ "$a" = "y" ] || [ "$a" = "Y" ]; then "$CODEX" login; fi
else
  echo "A browser window will open: choose 'Sign in with ChatGPT' and use your ChatGPT Pro account."
  "$CODEX" login
fi
echo
"$CODEX" login status
echo
echo "Done. In any GrapheAI app choose 'ChatGPT subscription (Plus/Pro via Codex CLI)' in the"
echo "sidebar, click Re-check, and pick GPT-6-Astra or a GPT-5.6 model."
read -r -p "Press Return to close." _
