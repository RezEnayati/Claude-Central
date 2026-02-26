# Claude Central

A terminal dashboard for monitoring and launching [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions. Displays a real-time board UI with status tracking, CPU usage, and session lifecycle management.

![Python 3](https://img.shields.io/badge/python-3.10+-blue) ![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)

## Prerequisites

- **macOS** (uses AppleScript for terminal window management)
- **Python 3.10+**
- **zsh** or **bash**
- **Claude Code CLI** installed and available on your `PATH`
- Python packages: `fastapi`, `uvicorn`, `pydantic`

## Install

```bash
git clone https://github.com/RezEnayati/Claude-Central.git ~/.claude-board
cd ~/.claude-board
pip install fastapi uvicorn pydantic
```

## Quick Start

### 1. Set up the shell wrapper

Add the wrapper to your shell profile so every `claude` invocation is automatically tracked on the board:

```bash
# zsh
echo 'source ~/.claude-board/claude-board-wrapper.sh' >> ~/.zshrc
source ~/.zshrc

# bash
echo 'source ~/.claude-board/claude-board-wrapper.sh' >> ~/.bashrc
source ~/.bashrc
```

### 2. Start the board

```bash
python3 ~/.claude-board/board.py
```

This launches the dashboard and a local API server on `localhost:8080`. Any Claude sessions already running will be auto-discovered.

Now running `claude` in any terminal will register the session on the board.

## Usage

### Keyboard Controls

| Key | Action |
|-----|--------|
| `N` | Launch a new Claude session (opens directory picker) |
| `K` | Kill the selected session (with confirmation) |
| `Q` | Quit the board |
| `Up/Down` | Navigate the session list |

### Directory Picker

| Key | Action |
|-----|--------|
| `/` | Switch to path input mode |
| `Tab` | Auto-complete the typed path |
| `Enter` | Launch session in the selected directory |
| `Esc` | Cancel |

### Session States

| Status | Meaning |
|--------|---------|
| **Waiting** | Session is idle (CPU < 5%) |
| **Running** | Session is actively working (CPU > 5%) |
| **Complete** | Session finished successfully |
| **Failed** | Session exited with a non-zero code |
| **Killed** | Session was manually terminated |

Completed/failed/killed sessions remain visible for 30 seconds before clearing.

## How It Works

The board runs three threads:

1. **API server** (FastAPI on port 8080) — receives task registration and status updates from the shell wrapper
2. **CPU monitor** — polls process stats every 2 seconds, detects running/idle transitions, and marks sessions as done when their process exits
3. **Display loop** (curses) — renders the board UI and handles keyboard input

The shell wrapper (`claude-board-wrapper.sh`) intercepts `claude` commands, registers them with the API server before execution, and reports exit status when they finish.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BOARD_SERVER` | `http://localhost:8080` | API server URL (used by the wrapper) |
| `REAL_CLAUDE` | auto-detected | Path to the real `claude` binary |
| `CLAUDE_BOARD_ASCII` | `0` | Set to `1` for ASCII-only box drawing |
