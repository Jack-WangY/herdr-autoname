"""Copy each agent pane's session topic into herdr workspace tokens topic1..topic4.

Modes, chosen by argv[1]. run serves the event stream in this process. start detaches, replaces any
older helper for the same herdr server and runs. stop ends this server's helper.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import socket
import sys
import time

SOCK = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
STATE = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr/plugins/herdr-autoname")
LOCK = os.path.join(STATE, hashlib.sha1(SOCK.encode()).hexdigest()[:12] + ".lock")
LOG = os.path.join(STATE, "herdr-autoname.log")
LOG_LIMIT = 1 << 20
SLOTS = 4
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
EVENTS = ("pane.updated", "pane.created", "pane.closed", "pane.exited", "pane.moved",
          "pane.agent_detected", "tab.moved", "workspace.created", "workspace.closed")

Topics = tuple[str, ...]
Desired = dict[str, Topics]

sent: dict[str, Topics] = {}


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


def call(method: str, params: dict) -> dict:
    # herdr closes a connection after one response, so every request gets its own.
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(SOCK)
        s.sendall(json.dumps({"id": "1", "method": method, "params": params}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                raise EOFError("herdr closed the connection mid-response")
            buf += chunk
    return json.loads(buf)


def desired(snap: dict) -> Desired:
    # Layouts cover every tab and each tab has its own origin, so rank by tab number first.
    tab_no = {t["tab_id"]: t["number"] for t in snap.get("tabs", [])}
    rect = {p["pane_id"]: (p["rect"]["x"], p["rect"]["y"])
            for lay in snap["layouts"] for p in lay["panes"]}
    ranked: dict[str, list[tuple]] = {w["workspace_id"]: [] for w in snap["workspaces"]}
    for p in snap["panes"]:
        title = (p.get("terminal_title_stripped") or "").strip()
        if p.get("agent") and title and p["workspace_id"] in ranked:
            pid = p["pane_id"]
            if pid in rect:
                order = (0, tab_no.get(p.get("tab_id"), 0), *rect[pid])
            else:
                num = pid.rpartition("p")[2]
                order = (1, int(num) if num.isdigit() else 0)
            ranked[p["workspace_id"]].append((order, title))
    return {ws: tuple(title for _, title in sorted(ps))[:SLOTS] for ws, ps in ranked.items()}


def tokens(topics: Topics) -> dict[str, str | None]:
    return {f"topic{i + 1}": topics[i] if i < len(topics) else None for i in range(SLOTS)}


def report(ws: str, topics: Topics) -> None:
    # Record intent first so a signal mid-report still gets this workspace cleared on exit.
    sent[ws] = topics
    reply = call("workspace.report_metadata", {
        "workspace_id": ws, "source": "autoname", "tokens": tokens(topics), "seq": time.time_ns()})
    if "error" in reply:
        del sent[ws]
        log(f"error {ws} {reply['error']}")
        return
    log(f"{ws} " + (" ".join(f"{k}={v!r}" for k, v in tokens(topics).items() if v) or "cleared"))


def reconcile() -> None:
    want = desired(call("session.snapshot", {})["result"]["snapshot"])
    for ws in sent.keys() - want.keys():
        del sent[ws]
    for ws, topics in want.items():
        if sent.get(ws) != topics:
            report(ws, topics)


def clear() -> None:
    try:
        for ws in list(sent):
            report(ws, ())
    except Exception as e:
        log(f"clear failed: {e!r}")


def subscribe(s: socket.socket) -> None:
    s.settimeout(5)
    s.connect(SOCK)
    req = {"id": "sub", "method": "events.subscribe",
           "params": {"subscriptions": [{"type": t} for t in EVENTS]}}
    s.sendall(json.dumps(req).encode() + b"\n")
    if b"subscription_started" not in s.recv(65536):
        raise ConnectionError("herdr refused the event subscription")


def wait_for_events(s: socket.socket) -> None:
    """Block with no timeout until an event arrives, then swallow the rest of the burst."""
    s.settimeout(None)
    if not s.recv(65536):
        raise EOFError("herdr closed the event stream")
    s.settimeout(0.3)
    try:
        while s.recv(65536):
            pass
        raise EOFError("herdr closed the event stream")
    except socket.timeout:
        pass


def interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def run() -> int:
    for sig in STOP_SIGNALS:
        signal.signal(sig, interrupt)
    try:
        with socket.socket(socket.AF_UNIX) as s:
            try:
                subscribe(s)
            except OSError as e:
                log(f"cannot connect to {SOCK}: {e!r}")
                return 1
            log(f"connected {SOCK}")
            while True:
                # A slow or failed snapshot must not end the helper; the next event retries.
                # Only the event stream closing means herdr is gone.
                try:
                    reconcile()
                except Exception as e:
                    log(f"reconcile failed: {e!r}")
                wait_for_events(s)
    except KeyboardInterrupt:
        log("signalled")
    except Exception as e:
        log(f"disconnected: {e!r}")
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)  # a second starter's SIGTERM must not cut the clear short
    clear()
    log("stopped")
    return 0


def holder_pid(fd: int) -> int:
    raw = os.pread(fd, 32, 0).strip()
    return int(raw) if raw.isdigit() else 0


def take_lock(fd: int, timeout: float) -> int:
    """Hold the lock on fd, SIGTERMing each holder once. Returns the last pid signalled, 0 if free."""
    # Retrying instead of one blocking flock keeps two concurrent starts from leaving a second,
    # blocked helper behind: the loser sees the winner's pid and replaces it.
    deadline = time.monotonic() + timeout
    killed = 0
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return killed
        except BlockingIOError:
            pass
        pid = holder_pid(fd)
        if pid and pid != killed:
            killed = pid
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"lock still held by {pid}")
        time.sleep(0.05)


def start() -> int:
    os.makedirs(STATE, exist_ok=True)
    if os.fork():
        return 0
    os.setsid()
    if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_LIMIT:
        os.replace(LOG, LOG + ".1")
    # herdr keeps the startup entry "running" until the child lets go of its inherited stdio.
    null = os.open(os.devnull, os.O_RDONLY)
    out = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(null, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    os.close(null)
    os.close(out)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        take_lock(fd, 10)
    except TimeoutError as e:
        log(f"start failed: {e}")
        return 1
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return run()


def stop() -> int:
    os.makedirs(STATE, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        pid = take_lock(fd, 5)
    except TimeoutError as e:
        print(f"not stopped: {e}")
        return 1
    print(f"stopped {pid}" if pid else "not running")
    return 0


if __name__ == "__main__":
    mode = {"run": run, "start": start, "stop": stop}.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if mode is None:
        sys.exit("usage: herdr-autoname.py run|start|stop")
    sys.exit(mode())
