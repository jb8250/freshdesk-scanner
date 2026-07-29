#!/usr/bin/env bash
set -euo pipefail

echo "=== Freshdesk Scanner Setup (Mac) ==="
echo ""

# Check for python3.11
if ! command -v python3.11 &>/dev/null; then
  echo "Installing python@3.11 via brew..."
  brew install python@3.11
else
  echo "OK: python3.11 already installed"
fi

# Create venv
if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3.11 -m venv .venv
else
  echo "OK: .venv already exists"
fi

# Activate venv
source .venv/bin/activate

# Install deps
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Download spacy model
echo "Downloading spacy language model..."
python -m spacy download en_core_web_lg

# Set up API key directory
mkdir -p ~/.config/furtouch
chmod 700 ~/.config/furtouch

# Prompt for API key if not present
if [ ! -f ~/.config/furtouch/freshdesk_api_key ]; then
  echo ""
  echo "=== Freshdesk API Key Setup ==="
  echo "Paste your Freshdesk API key below. It will be saved to ~/.config/furtouch/freshdesk_api_key"
  echo "(You can find it in Freshdesk: Admin > Security > API Keys)"
  echo ""
  read -r -p "API Key: " api_key
  echo "$api_key" > ~/.config/furtouch/freshdesk_api_key
  chmod 600 ~/.config/furtouch/freshdesk_api_key
  echo "OK: API key saved"
else
  echo "OK: API key already exists at ~/.config/furtouch/freshdesk_api_key"
fi

echo ""
echo "=== Setup complete! ==="
echo "To run the scanner:"
echo "  source .venv/bin/activate"
echo "  flask run --host 127.0.0.1 --port 5050"
echo ""
echo "Then open: http://127.0.0.1:5050/queue"
