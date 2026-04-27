#!/usr/bin/env bash
set -e

echo ""
echo "  taktis Setup (macOS)"
echo "  =============================="
echo ""

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# --- Check for Homebrew ---
if ! command -v brew &>/dev/null; then
    echo "[!] Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi
echo "[OK] Homebrew found"

# --- Python 3.10+ ---
check_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver major minor
            ver="$($cmd --version 2>&1 | awk '{print $2}')"
            major="$(echo "$ver" | cut -d. -f1)"
            minor="$(echo "$ver" | cut -d. -f2)"
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                PYTHON_CMD="$cmd"
                echo "[OK] Python $ver ($cmd)"
                return 0
            fi
        fi
    done
    return 1
}

if ! check_python; then
    echo "[!] Python 3.10+ not found. Installing via Homebrew..."
    brew install python@3.12
    PYTHON_CMD="python3"
    if ! check_python; then
        echo "[X] Failed to install Python. Please install manually: brew install python@3.12"
        exit 1
    fi
fi

# --- Node.js 18+ ---
if ! command -v node &>/dev/null; then
    echo "[!] Node.js not found. Installing via Homebrew..."
    brew install node
fi
echo "[OK] Node.js $(node --version)"

# --- Claude Code CLI ---
if ! command -v claude &>/dev/null; then
    echo "[!] Claude Code CLI not found. Installing..."
    npm install -g @anthropic-ai/claude-code
fi
echo "[OK] Claude Code CLI found"

# --- Virtual environment ---
if [ ! -d .venv ]; then
    echo ""
    echo "Creating virtual environment..."
    "$PYTHON_CMD" -m venv .venv
fi
echo "[OK] Virtual environment ready"

# --- Install dependencies ---
echo ""
echo "Installing Python dependencies..."
source .venv/bin/activate
pip install -r requirements.txt
echo "[OK] Dependencies installed"

# --- Auth reminder ---
echo ""
echo "============================================="
echo "  Almost done! Authenticate with Claude:"
echo ""
echo "    claude login"
echo ""
echo "  (Skip if ANTHROPIC_API_KEY is already set)"
echo "============================================="
echo ""

# --- Create start.sh in project root ---
cat > "$PROJECT_ROOT/start.sh" << 'STARTEOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate
python3 run.py
STARTEOF
chmod +x "$PROJECT_ROOT/start.sh"

echo "[OK] Setup complete!"
echo ""
echo "To start taktis, run: ./start.sh"
echo "Web UI will be at: http://localhost:8080"
echo ""
