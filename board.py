#!/usr/bin/env python3
"""
Claude Central — MTA-style arrival board for Claude Code

  python3 board.py          Start the board
  Press 'q' or Ctrl+C       Quit
"""

import curses
import threading
import time
import signal
import subprocess
import os
import sys
from collections import defaultdict
from typing import Optional
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

# ── State ────────────────────────────────────────────────────────────────────

tasks = {}
lock = threading.Lock()
DONE_TTL = 30
should_quit = threading.Event()
total_sessions = [0]
_status_flash = {}  # task_id -> timestamp of last status change
_selected_idx = [0]  # index of currently selected task row
_confirm_kill = [False]  # True when waiting for Y/N

class TaskCreate(BaseModel):
    id: str
    name: str
    shell_pid: Optional[int] = None
    cwd: Optional[str] = None

class TaskUpdate(BaseModel):
    status: str
    exit_code: Optional[int] = None

# ── API ──────────────────────────────────────────────────────────────────────

app = FastAPI()

@app.post("/task")
def create_task(body: TaskCreate):
    cwd = body.cwd
    if cwd and cwd.strip() and cwd.strip() != "/":
        group = os.path.basename(os.path.normpath(cwd))
    else:
        group = "General"
    with lock:
        tasks[body.id] = {
            "id": body.id,
            "name": body.name,
            "status": "IDLE",
            "shell_pid": body.shell_pid,
            "claude_pid": None,
            "cwd": cwd,
            "group": group,
            "started_at": time.time(),
            "work_started_at": None,
            "finished_at": None,
            "exit_code": None,
            "high_count": 0,
            "low_count": 0,
        }
        total_sessions[0] += 1
        _status_flash[body.id] = time.time()
    return {"ok": True}

@app.patch("/task/{task_id}")
def update_task(task_id: str, body: TaskUpdate):
    with lock:
        if task_id not in tasks:
            return {"ok": False, "error": "not found"}
        t = tasks[task_id]
        old_status = t["status"]
        t["status"] = body.status
        t["exit_code"] = body.exit_code
        if body.status == "RUNNING" and old_status != "RUNNING":
            t["work_started_at"] = time.time()
        if body.status in ("DONE", "FAILED"):
            t["finished_at"] = time.time()
        if body.status != old_status:
            _status_flash[task_id] = time.time()
    return {"ok": True}

@app.get("/tasks")
def list_tasks():
    with lock:
        return list(tasks.values())

def pid_alive(pid):
    """Check if a PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def kill_process_tree(pid):
    """Kill a process and all its children."""
    try:
        # First kill children
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    child_pid = int(line.strip())
                    kill_process_tree(child_pid)
                except (ValueError, OSError):
                    pass
        # Then kill the process itself
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass


def kill_task_by_index(visible_tasks):
    """Kill the task at the current selected index."""
    idx = _selected_idx[0]
    if idx < 0 or idx >= len(visible_tasks):
        return
    t = visible_tasks[idx]
    if t["status"] not in ("IDLE", "RUNNING"):
        return
    tid = t["id"]
    # Kill claude child first, then shell
    claude_pid = t.get("claude_pid")
    shell_pid = t.get("shell_pid")
    if claude_pid:
        kill_process_tree(claude_pid)
    if shell_pid:
        kill_process_tree(shell_pid)
    # Update task status
    with lock:
        if tid in tasks:
            tasks[tid]["status"] = "KILLED"
            tasks[tid]["finished_at"] = time.time()
            tasks[tid]["exit_code"] = -15
            _status_flash[tid] = time.time()


def get_child_pids(parent_pid):
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,comm"],
            capture_output=True, text=True, timeout=5
        )
        pids = []
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 3:
                pid, ppid, comm = int(parts[0]), int(parts[1]), parts[2]
                if ppid == parent_pid and ("claude" in comm.lower() or "node" in comm.lower()):
                    pids.append(pid)
        return pids
    except Exception:
        return []

def get_cpu_usage(pid):
    try:
        all_pids = [pid]
        try:
            result = subprocess.run(
                ["pgrep", "-P", str(pid)],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    all_pids.append(int(line.strip()))
        except Exception:
            pass
        total = 0.0
        for p in all_pids:
            try:
                result = subprocess.run(
                    ["ps", "-o", "%cpu=", "-p", str(p)],
                    capture_output=True, text=True, timeout=5
                )
                cpu = result.stdout.strip()
                if cpu:
                    total += float(cpu)
            except Exception:
                pass
        return total
    except Exception:
        return 0.0

def cpu_monitor_loop():
    while not should_quit.is_set():
        with lock:
            active = [
                (t["id"], t["shell_pid"], t.get("claude_pid"))
                for t in tasks.values()
                if t["status"] in ("IDLE", "RUNNING") and t["shell_pid"]
            ]

        for tid, shell_pid, claude_pid in active:
            # ── Check if the shell process is dead ──
            if not pid_alive(shell_pid):
                with lock:
                    if tid in tasks and tasks[tid]["status"] in ("IDLE", "RUNNING"):
                        tasks[tid]["status"] = "DONE"
                        tasks[tid]["finished_at"] = time.time()
                        tasks[tid]["exit_code"] = 0
                        _status_flash[tid] = time.time()
                continue

            # ── Also check if claude child died but shell is still alive ──
            if claude_pid and not pid_alive(claude_pid):
                with lock:
                    if tid in tasks and tasks[tid]["status"] in ("IDLE", "RUNNING"):
                        tasks[tid]["status"] = "DONE"
                        tasks[tid]["finished_at"] = time.time()
                        tasks[tid]["exit_code"] = 0
                        _status_flash[tid] = time.time()
                continue

            # ── Find claude child if we haven't yet ──
            if not claude_pid:
                children = get_child_pids(shell_pid)
                if children:
                    claude_pid = children[0]
                    with lock:
                        if tid in tasks:
                            tasks[tid]["claude_pid"] = claude_pid

            if not claude_pid:
                continue

            # ── Check CPU ──
            cpu = get_cpu_usage(claude_pid)

            with lock:
                if tid not in tasks:
                    continue
                tt = tasks[tid]
                if cpu > 5:
                    tt["high_count"] = tt.get("high_count", 0) + 1
                    tt["low_count"] = 0
                else:
                    tt["low_count"] = tt.get("low_count", 0) + 1
                    tt["high_count"] = 0

                if tt["high_count"] >= 2 and tt["status"] != "RUNNING":
                    tt["status"] = "RUNNING"
                    tt["work_started_at"] = time.time()
                    _status_flash[tid] = time.time()
                elif tt["low_count"] >= 2 and tt["status"] != "IDLE":
                    tt["status"] = "IDLE"
                    _status_flash[tid] = time.time()

        should_quit.wait(2)

# ── MTA Display ──────────────────────────────────────────────────────────────

def fmt_elapsed(secs):
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return "{}h{:02d}m".format(h, m)
    if m > 0:
        return "{}m{:02d}s".format(m, s)
    return "{}s".format(s)

def safe_addstr(scr, y, x, text, attr=0):
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        scr.addnstr(y, x, text, w - x - 1, attr)
    except curses.error:
        pass

def safe_hline(scr, y, x, ch, length, attr=0):
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        scr.hline(y, x, ch, min(length, w - x - 1), attr)
    except curses.error:
        pass

USE_ASCII = os.environ.get("CLAUDE_BOARD_ASCII", "") == "1"

# Box-drawing characters (with ASCII fallback)
if USE_ASCII:
    BOX_TL, BOX_TR, BOX_BL, BOX_BR = "+", "+", "+", "+"
    BOX_H, BOX_V = "-", "|"
    BOX_LT, BOX_RT = "+", "+"
    IND_RUN_ON, IND_RUN_OFF, IND_IDLE, IND_DONE, IND_FAIL, IND_KILLED = "*", "o", "o", "V", "X", "#"
    SYM_ARROW, SYM_DOT, SYM_SELECT = ">", ".", ">"
else:
    BOX_TL, BOX_TR, BOX_BL, BOX_BR = "\u2554", "\u2557", "\u255a", "\u255d"
    BOX_H, BOX_V = "\u2550", "\u2551"
    BOX_LT, BOX_RT = "\u2560", "\u2563"
    IND_RUN_ON, IND_RUN_OFF, IND_IDLE, IND_DONE, IND_FAIL, IND_KILLED = "\u25cf", "\u25cb", "\u25cb", "\u2713", "\u2717", "\u2620"
    SYM_ARROW, SYM_DOT, SYM_SELECT = "\u25b6", "\u25aa", "\u25b8"


def draw_box(scr, y, x, width, height, attr=0):
    """Draw a double-line Unicode box border."""
    safe_addstr(scr, y, x, BOX_TL + BOX_H * (width - 2) + BOX_TR, attr)
    for i in range(1, height - 1):
        safe_addstr(scr, y + i, x, BOX_V, attr)
        safe_addstr(scr, y + i, x + width - 1, BOX_V, attr)
    safe_addstr(scr, y + height - 1, x, BOX_BL + BOX_H * (width - 2) + BOX_BR, attr)


def draw_hsep(scr, y, x, width, attr=0):
    """Draw an interior horizontal separator with T-junctions."""
    safe_addstr(scr, y, x, BOX_LT + BOX_H * (width - 2) + BOX_RT, attr)


TICKER_BASE = "Welcome to Claude Central {} Sessions are auto-detected {} Status updates every 2s".format(SYM_DOT, SYM_DOT)
_ticker_offset = [0]


def build_ticker(visible, now):
    """Build dynamic ticker with live session info."""
    parts = [TICKER_BASE]
    total = total_sessions[0]
    if total:
        parts.append("{} total sessions".format(total))
    # Find last completed task
    done_tasks = [t for t in visible if t["status"] in ("DONE", "FAILED", "KILLED") and t.get("finished_at")]
    if done_tasks:
        last = max(done_tasks, key=lambda t: t["finished_at"])
        ago = int(now - last["finished_at"])
        if ago < 60:
            ago_str = "{}s ago".format(ago)
        else:
            ago_str = "{}m ago".format(ago // 60)
        parts.append("last done: {} {}".format(last["name"][:16], ago_str))
    running = sum(1 for t in visible if t["status"] == "RUNNING")
    if running:
        parts.append("{} active now".format(running))
    sep = " {} ".format(SYM_DOT)
    return sep.join(parts)


def display_loop(stdscr):
    curses.start_color()
    curses.use_default_colors()
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(500)

    # ── Color pairs (256-color with 8-color fallback) ──
    if curses.COLORS >= 256:
        curses.init_pair(1, 208, 0)     # amber on black
        curses.init_pair(2, 29, 0)      # green on black
        curses.init_pair(3, 196, 0)     # red on black
        curses.init_pair(4, 255, 0)     # white on black
        curses.init_pair(5, 255, 25)    # white on MTA blue
        curses.init_pair(6, 242, 0)     # dim on black
        curses.init_pair(7, 238, 0)     # border on black
        curses.init_pair(8, 0, 208)     # badge: black on amber
        curses.init_pair(9, 242, 0)     # ticker
        curses.init_pair(10, 208, 25)   # header accent: amber on blue
        curses.init_pair(11, 133, 0)    # killed: purple on black
        curses.init_pair(12, 74, 0)     # group header: cyan on black
    else:
        curses.init_pair(1, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(9, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(10, curses.COLOR_YELLOW, curses.COLOR_BLUE)
        curses.init_pair(11, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
        curses.init_pair(12, curses.COLOR_CYAN, curses.COLOR_BLACK)

    AMBER = curses.color_pair(1) | curses.A_BOLD
    GREEN = curses.color_pair(2) | curses.A_BOLD
    RED = curses.color_pair(3) | curses.A_BOLD
    WHITE = curses.color_pair(4) | curses.A_BOLD
    HEADER = curses.color_pair(5) | curses.A_BOLD
    DIM = curses.color_pair(6)
    BORDER = curses.color_pair(7)
    BADGE = curses.color_pair(8) | curses.A_BOLD
    TICKER = curses.color_pair(9)
    HEADER_ACCENT = curses.color_pair(10) | curses.A_BOLD
    KILLED_C = curses.color_pair(11) | curses.A_BOLD
    GROUP_HDR = curses.color_pair(12) | curses.A_BOLD

    # Force black background
    try:
        stdscr.bkgd(" ", curses.color_pair(7))
    except curses.error:
        pass

    BW = 70

    while not should_quit.is_set():
        stdscr.erase()
        now = time.time()
        h, w = stdscr.getmaxyx()
        ox = max(0, (w - BW) // 2)

        # ── Gather visible tasks ──
        with lock:
            all_visible = []
            for t in tasks.values():
                if t["status"] in ("IDLE", "RUNNING"):
                    all_visible.append(dict(t))
                elif t["status"] in ("DONE", "FAILED", "KILLED") and t["finished_at"] and (now - t["finished_at"]) < DONE_TTL:
                    all_visible.append(dict(t))

        # ── Group tasks by directory ──
        status_order = {"RUNNING": 0, "IDLE": 1, "DONE": 2, "KILLED": 3, "FAILED": 4}
        groups = defaultdict(list)
        for t in all_visible:
            groups[t.get("group", "General")].append(t)

        # Sort tasks within each group by status
        for g in groups:
            groups[g].sort(key=lambda t: status_order.get(t["status"], 99))

        # Sort groups: groups with RUNNING first, then IDLE, then rest; alpha tiebreaker
        def group_sort_key(name):
            tlist = groups[name]
            best = min(status_order.get(t["status"], 99) for t in tlist)
            return (best, name.lower())
        sorted_groups = sorted(groups.keys(), key=group_sort_key)

        # Build display_rows and flat_tasks
        display_rows = []
        flat_tasks = []
        for gname in sorted_groups:
            tlist = groups[gname]
            display_rows.append(("header", gname, len(tlist)))
            for t in tlist:
                display_rows.append(("task", t, len(flat_tasks)))
                flat_tasks.append(t)

        num_groups = len(sorted_groups)
        visible = flat_tasks  # keep for summary/ticker compatibility

        # ── Calculate board height for vertical centering ──
        content_rows = max(len(flat_tasks) + num_groups, 1)
        # rows: top border(1) + header(1) + hsep(1) + col headers(1) + thin sep(1)
        #      + content_rows + blank(1) + hsep(1) + summary(1) + addr line(1)
        #      + ticker(1) + bottom border(1)
        board_h = 1 + 1 + 1 + 1 + 1 + content_rows + 1 + 1 + 1 + 1 + 1 + 1
        oy = max(0, (h - board_h) // 2)

        row = oy

        # ── Top border ──
        draw_box(stdscr, row, ox, BW, board_h, BORDER)

        # ── Header row (MTA blue background) ──
        row += 1
        header_inner = " " * (BW - 2)
        safe_addstr(stdscr, row, ox + 1, header_inner, HEADER)
        safe_addstr(stdscr, row, ox + 2, " [C] ", BADGE)
        safe_addstr(stdscr, row, ox + 7, " Claude Central", HEADER)
        clock_text = time.strftime("%H:%M:%S") + " "
        safe_addstr(stdscr, row, ox + BW - 2 - len(clock_text), clock_text, HEADER_ACCENT)

        # ── Separator after header ──
        row += 1
        draw_hsep(stdscr, row, ox, BW, BORDER)

        # ── Column headers ──
        row += 1
        safe_addstr(stdscr, row, ox + 4, "DESTINATION", AMBER)
        safe_addstr(stdscr, row, ox + 32, "STATUS", AMBER)
        safe_addstr(stdscr, row, ox + 54, "TIME", AMBER)

        # ── Thin separator ──
        row += 1
        thin_line = "\u2500" * (BW - 6) if not USE_ASCII else "-" * (BW - 6)
        safe_addstr(stdscr, row, ox + 3, thin_line, DIM)

        # ── Task rows ──
        row += 1
        # Clamp selection index
        if flat_tasks:
            _selected_idx[0] = max(0, min(_selected_idx[0], len(flat_tasks) - 1))
        else:
            _selected_idx[0] = 0

        if not flat_tasks:
            dots = "." * (int(now) % 4)
            safe_addstr(stdscr, row, ox + 4, "Waiting for sessions" + dots, DIM)
            row += 1
        else:
            grp_dash = "\u2500" if not USE_ASCII else "-"
            for dr in display_rows:
                if dr[0] == "header":
                    _, gname, gcount = dr
                    label = " {} ({}) ".format(gname, gcount)
                    left_dashes = 2
                    right_dashes = max(1, (BW - 6) - left_dashes - len(label))
                    hdr_line = grp_dash * left_dashes + label + grp_dash * right_dashes
                    safe_addstr(stdscr, row, ox + 3, hdr_line, GROUP_HDR)
                    row += 1
                else:
                    _, t, flat_idx = dr
                    name = t["name"][:26]
                    st = t["status"]
                    selected = (flat_idx == _selected_idx[0])

                    # Selection cursor
                    if selected:
                        safe_addstr(stdscr, row, ox + 1, SYM_SELECT, AMBER)

                    # Flash: bold+reverse for 2s after status change
                    flash_time = _status_flash.get(t["id"], 0)
                    flashing = (now - flash_time) < 2.0

                    if st == "RUNNING":
                        el = fmt_elapsed(now - (t["work_started_at"] or t["started_at"]))
                        ind = IND_RUN_ON if int(now * 2) % 2 == 0 else IND_RUN_OFF
                        c = AMBER | curses.A_REVERSE if flashing else AMBER
                        nc = WHITE | curses.A_REVERSE if flashing else WHITE
                        safe_addstr(stdscr, row, ox + 3, ind, c)
                        safe_addstr(stdscr, row, ox + 5, name, nc)
                        safe_addstr(stdscr, row, ox + 32, "Running", c)
                        safe_addstr(stdscr, row, ox + 54, el, c)
                    elif st == "IDLE":
                        el = fmt_elapsed(now - t["started_at"])
                        c = DIM | curses.A_REVERSE if flashing else DIM
                        safe_addstr(stdscr, row, ox + 3, IND_IDLE, c)
                        safe_addstr(stdscr, row, ox + 5, name, c)
                        safe_addstr(stdscr, row, ox + 32, "Waiting", c)
                        safe_addstr(stdscr, row, ox + 54, el, c)
                    elif st == "DONE":
                        el = fmt_elapsed(t["finished_at"] - t["started_at"])
                        c = GREEN | curses.A_REVERSE if flashing else GREEN
                        safe_addstr(stdscr, row, ox + 3, IND_DONE, c)
                        safe_addstr(stdscr, row, ox + 5, name, c)
                        safe_addstr(stdscr, row, ox + 32, "Complete", c)
                        safe_addstr(stdscr, row, ox + 54, el, c)
                    elif st == "KILLED":
                        el = fmt_elapsed(t["finished_at"] - t["started_at"])
                        c = KILLED_C | curses.A_REVERSE if flashing else KILLED_C
                        safe_addstr(stdscr, row, ox + 3, IND_KILLED, c)
                        safe_addstr(stdscr, row, ox + 5, name, c)
                        safe_addstr(stdscr, row, ox + 32, "Killed", c)
                        safe_addstr(stdscr, row, ox + 54, el, c)
                    elif st == "FAILED":
                        el = fmt_elapsed(t["finished_at"] - t["started_at"])
                        ec = t.get("exit_code")
                        label = "Failed ({})".format(ec) if ec is not None else "Failed"
                        c = RED | curses.A_REVERSE if flashing else RED
                        safe_addstr(stdscr, row, ox + 3, IND_FAIL, c)
                        safe_addstr(stdscr, row, ox + 5, name, c)
                        safe_addstr(stdscr, row, ox + 32, label, c)
                        safe_addstr(stdscr, row, ox + 54, el, c)
                    row += 1

        # ── Skip blank row (covered by box border interior) ──
        row += 1

        # ── Bottom separator ──
        # row is now at the hsep position inside draw_box
        # We re-draw the hsep at this position
        draw_hsep(stdscr, row - 1, ox, BW, BORDER)

        # ── Service summary ──
        running = sum(1 for t in visible if t.get("status") == "RUNNING")
        idle = sum(1 for t in visible if t.get("status") == "IDLE")
        summary_parts = []
        if running:
            summary_parts.append("{} running".format(running))
        if idle:
            summary_parts.append("{} waiting".format(idle))
        if not summary_parts:
            summary_parts.append("All quiet")
        summary_text = "{} {}".format(SYM_ARROW, "  ".join(summary_parts))
        safe_addstr(stdscr, row, ox + 3, summary_text, DIM)

        # ── Address + controls ──
        row += 1
        safe_addstr(stdscr, row, ox + 4, "localhost:8080", DIM)
        controls = "[K] Kill  [Q] Quit"
        safe_addstr(stdscr, row, ox + BW - 2 - len(controls), controls, DIM)

        # ── Scrolling ticker (dynamic) ──
        row += 1
        ticker_w = BW - 6
        if ticker_w > 0:
            ticker_msg = build_ticker(visible, now)
            padded = ticker_msg + "   "
            idx = _ticker_offset[0] % len(padded)
            display = (padded[idx:] + padded)[:ticker_w]
            safe_addstr(stdscr, row, ox + 3, display, TICKER)
            _ticker_offset[0] += 1

        # ── Kill confirmation bar ──
        if _confirm_kill[0] and flat_tasks:
            sel = _selected_idx[0]
            if 0 <= sel < len(flat_tasks):
                tname = flat_tasks[sel]["name"][:20]
                prompt = " Kill \"{}\"?  [Y] Yes  [N] No ".format(tname)
                px = max(0, (w - len(prompt)) // 2)
                py = h - 1
                safe_addstr(stdscr, py, px, prompt, RED | curses.A_REVERSE)

        stdscr.refresh()

        try:
            key = stdscr.getch()

            # ── Kill confirmation mode ──
            if _confirm_kill[0]:
                if key == ord('y') or key == ord('Y'):
                    kill_task_by_index(flat_tasks)
                _confirm_kill[0] = False
                continue

            # ── Normal mode ──
            if key == ord('q') or key == ord('Q'):
                should_quit.set()
                break
            elif key == curses.KEY_UP:
                _selected_idx[0] = max(0, _selected_idx[0] - 1)
            elif key == curses.KEY_DOWN:
                _selected_idx[0] += 1  # clamped at top of loop
            elif key == ord('k') or key == ord('K'):
                if flat_tasks:
                    sel = _selected_idx[0]
                    if 0 <= sel < len(flat_tasks) and flat_tasks[sel]["status"] in ("IDLE", "RUNNING"):
                        _confirm_kill[0] = True
        except Exception:
            pass

# ── Main ─────────────────────────────────────────────────────────────────────

def handle_signal(sig, frame):
    should_quit.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    server_thread = threading.Thread(
        target=uvicorn.run, args=(app,),
        kwargs={"host": "0.0.0.0", "port": 8080, "log_level": "error"},
        daemon=True,
    )
    server_thread.start()

    monitor_thread = threading.Thread(target=cpu_monitor_loop, daemon=True)
    monitor_thread.start()

    try:
        curses.wrapper(display_loop)
    except KeyboardInterrupt:
        should_quit.set()
    finally:
        print("Board stopped.")
