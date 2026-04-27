#!/usr/bin/env bash
set -e

echo ""
echo "  taktis Setup (Linux)"
echo "  =============================="
echo ""

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

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
    echo "[!] Python 3.10+ not found."
    echo "    Debian/Ubuntu:  sudo apt install python3 python3-venv python3-pip"
    echo "    Fedora:         sudo dnf install python3 python3-pip"
    echo "    Arch:           sudo pacman -S python python-pip"
    exit 1
fi

# --- Node.js 18+ ---
if ! command -v node &>/dev/null; then
    echo "[!] Node.js not found."
    echo "    Debian/Ubuntu:  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -"
    echo "                    sudo apt install -y nodejs"
    echo "    Fedora:         sudo dnf install nodejs"
    echo "    Arch:           sudo pacman -S nodejs npm"
    exit 1
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
