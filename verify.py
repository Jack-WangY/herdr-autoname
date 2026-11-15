"""Behaviour test for herdr-autoname against a herdr lab session.

Usage: python3 -I verify.py

Steps 0 to 6 run the helper's `run` mode directly. Steps R and R2 cover exits and a fake herdr whose
first snapshot fails. P0 to P5 drive the helper through the plugin.
The live helper (a LaunchAgent, or the plugin in your own session) is part of the baseline and must survive.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "herdr-autoname.py")
PLUGIN = "herdr-autoname"
SOCK = os.path.expanduser("~/.config/herdr/sessions/lab/herdr.sock")
LOCK = os.path.join(os.path.expanduser(f"~/.local/state/herdr/plugins/{PLUGIN}"),
                    hashlib.sha1(SOCK.encode()).hexdigest()[:12] + ".lock")
LAB_DIR = tempfile.TemporaryDirectory(prefix="herdr-autoname-lab-")
LAB_TOML = os.path.join(LAB_DIR.name, "config.toml")
HERDR = shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")
INSIDE_HERDR = ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID",
                "HERDR_BIN_PATH")
ENV = {k: v for k, v in os.environ.items() if k not in INSIDE_HERDR}

LEFT1, RIGHT1, RIGHT2, LEFT2, NEWPANE = (
    "lab left one", "lab right one", "lab right two", "lab left two", "lab new pane")

failures: list[str] = []


def rpc(method, params):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(SOCK)
        s.sendall(json.dumps({"id": "verify", "method": method, "params": params}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    msg = json.loads(buf)
    if "error" in msg:
        raise RuntimeError(f"{method}: {msg['error']}")
    return msg["result"]


def herdr(*args):
    r = subprocess.run([HERDR, "--session", "lab", *args], env=ENV, capture_output=True, text=True, timeout=20)
    if r.returncode:
        raise RuntimeError(f"herdr {' '.join(args)}: {r.stdout}{r.stderr}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def snapshot():
    return rpc("session.snapshot", {})["snapshot"]


def ws_tokens(ws):
    return rpc("workspace.get", {"workspace_id": ws})["workspace"].get("tokens", {})


def clear_tokens(ws):
    rpc("workspace.report_metadata", {"workspace_id": ws, "source": "autoname", "seq": time.time_ns(),
                                      "tokens": {f"topic{i}": None for i in range(1, 5)}})


def wait_until(check, timeout, interval=0.1):
    end = time.time() + timeout
    while True:
        got = check()
        if got[0] or time.time() > end:
            return got
        time.sleep(interval)


def pane_title(pane):
    return next(p for p in snapshot()["panes"] if p["pane_id"] == pane).get("terminal_title_stripped")


def set_title(pane, title):
    herdr("pane", "run", pane, f"printf '\\033]0;{title}\\007'")
    ok, got = wait_until(lambda: (pane_title(pane) == title, pane_title(pane)), 5)
    if not ok:
        raise RuntimeError(f"setup: {pane} title is {got!r}, wanted {title!r}")


def make_agent(pane):
    herdr("pane", "report-agent", "--source", "lab", "--agent", "claude", "--state", "idle", pane)


def check(step, cond, detail):
    if cond:
        print(f"PASS {step}")
    else:
        failures.append(step)
        print(f"FAIL {step}: {detail}")


def expect(step, want):
    def state():
        got = {ws: ws_tokens(ws) for ws in want}
        return got == want, got
    ok, got = wait_until(state, 3)
    check(step, ok, f"expected {want} got {got}")


def start_helper():
    return subprocess.Popen(["/usr/bin/python3", "-I", HELPER, "run"], env={**ENV, "HERDR_SOCKET_PATH": SOCK},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rss_and_cpu(pid):
    kb, cpu = subprocess.run(["ps", "-o", "rss=,time=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
    return int(kb), cpu


def footprint(pid):
    out = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True).stdout
    m = re.search(r"Footprint: (\d+) (\w+)", out)
    return f"{m[1]} {m[2]}" if m else "unavailable"


def helper_pids():
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    # Anchored so a shell whose command text merely mentions the helper does not count.
    helper = re.compile(r"python\S* -I (\S*/)?herdr-autoname\.py( run| start)?$", re.I)
    pids = (int(line.split(None, 1)[0]) for line in out.splitlines() if helper.search(line.strip()))
    return sorted(pid for pid in pids if pid != os.getpid())


def lock_pid():
    try:
        with open(LOCK) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def lab_up():
    try:
        rpc("ping", {})
        return True, None
    except OSError:
        return False, None


def ensure_lab():
    """Start the lab server if it is down. Returns True when this call started it."""
    if lab_up()[0]:
        return False
    with open(LAB_TOML, "w") as f:
        f.write("onboarding = false\n")
    subprocess.Popen(["perl", "-e", "alarm 3600; exec @ARGV", HERDR, "--session", "lab", "server"],
                     env={**ENV, "HERDR_CONFIG_PATH": LAB_TOML},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if not wait_until(lab_up, 15)[0]:
        raise RuntimeError("setup: lab server did not start")
    return True


def stop_lab():
    herdr("session", "stop", "lab", "--json")
    if not wait_until(lambda: (not lab_up()[0], None), 10)[0]:
        raise RuntimeError("setup: lab server did not stop")


def ensure_shape():
    labels = {w["label"]: w["workspace_id"] for w in herdr("workspace", "list")["result"]["workspaces"]}
    if "Web app" not in labels:
        if "w1" in labels.values():
            herdr("workspace", "rename", "w1", "Web app")
        else:
            herdr("workspace", "create", "--label", "Web app", "--no-focus")
    if "Notes" not in labels:
        herdr("workspace", "create", "--label", "Notes", "--no-focus")
    labels = {w["label"]: w["workspace_id"] for w in herdr("workspace", "list")["result"]["workspaces"]}
    if (labels.get("Web app"), labels.get("Notes")) != ("w1", "w2"):
        raise RuntimeError(f"setup: workspaces are {labels}, wanted w1 'Web app' and w2 'Notes'")
    if sum(p["workspace_id"] == "w1" for p in snapshot()["panes"]) == 1:
        herdr("pane", "split", "w1:p1", "--direction", "right", "--no-focus")


def lab_panes():
    snap = snapshot()
    by_ws = {}
    for p in snap["panes"]:
        by_ws.setdefault(p["workspace_id"], []).append(p)
    rects = {p["pane_id"]: p["rect"]["x"] for lay in snap["layouts"] for p in lay["panes"]}
    w1 = sorted((p["pane_id"] for p in by_ws.get("w1", [])), key=lambda pid: rects.get(pid, 0))
    if len(w1) != 2:
        raise RuntimeError(f"setup: w1 has panes {w1}, wanted exactly two")
    return w1, sorted(by_ws.get("w2", []), key=lambda p: p["pane_id"])


def report_titles(w1):
    for pane in w1:
        make_agent(pane)
    set_title(w1[0], LEFT1)
    set_title(w1[1], RIGHT1)


def establish_start_state():
    ensure_shape()
    w1, w2 = lab_panes()
    if not w2:
        raise RuntimeError("setup: w2 has no panes")
    for extra in w2[1:]:
        herdr("pane", "close", extra["pane_id"])
    if w2[0].get("agent"):
        # herdr 0.9.5 rejects release-agent options placed before the pane id.
        herdr("pane", "release-agent", w2[0]["pane_id"], "--source", "lab", "--agent", "claude")
    for ws in ("w1", "w2"):
        clear_tokens(ws)
    report_titles(w1)
    return w1[0], w1[1], w2[0]["pane_id"]


def verify_desired():
    try:
        spec = importlib.util.spec_from_file_location("autoname", HELPER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        check_desired(mod)
    except Exception as e:
        failures.append("0 desired()")
        print(f"FAIL 0 desired(): {e!r}")


def check_desired(mod):
    def pane(pid, title, agent="claude", tab=None):
        p = {"pane_id": pid, "workspace_id": pid.split(":")[0], "tab_id": tab or pid.split(":")[0] + ":t1"}
        if agent:
            p["agent"] = agent
        if title is not None:
            p["terminal_title_stripped"] = title
        return p

    def rect(pid, x, y):
        return {"pane_id": pid, "rect": {"x": x, "y": y, "width": 10, "height": 10}}

    snap = {
        "workspaces": [{"workspace_id": w} for w in ("wA", "wB", "wC")],
        "tabs": [{"tab_id": "wA:t1", "number": 1}, {"tab_id": "wA:t2", "number": 2}, {"tab_id": "wC:t1", "number": 1}],
        "layouts": [
            {"workspace_id": "wA", "tab_id": "wA:t1", "panes": [
                rect("wA:p1", 60, 0), rect("wA:p2", 0, 20), rect("wA:p3", 0, 0)]},
            {"workspace_id": "wA", "tab_id": "wA:t2", "panes": [rect("wA:p7", 0, 0)]},
        ],
        "panes": [
            pane("wA:p7", "second tab origin", tab="wA:t2"),
            pane("wA:p1", "right"), pane("wA:p2", "left-bottom"), pane("wA:p3", "left-top"),
            pane("wA:p4", "   "), pane("wA:p5", "plain shell", agent=None), pane("wA:p6", None),
            pane("wC:p10", "ten"), pane("wC:p2", "two"), pane("wC:p11", "eleven"), pane("wC:p3", "three"),
            pane("wC:p9", "nine"), pane("wC:p1", "plain", agent=None),
            pane("wZ:p1", "orphan"),
        ],
    }
    want = {"wA": ("left-top", "left-bottom", "right", "second tab origin"), "wB": (), "wC": ("two", "three", "nine", "ten")}
    got = mod.desired(snap)
    check("0a desired()", got == want, f"expected {want} got {got}")
    want_tokens = {"topic1": "a", "topic2": "b", "topic3": None, "topic4": None}
    got_tokens = mod.tokens(("a", "b"))
    check("0b tokens()", got_tokens == want_tokens, f"expected {want_tokens} got {got_tokens}")


def run_helper_steps():
    left, right, shell = establish_start_state()
    expect("0c start state tokens", {"w1": {}, "w2": {}})
    helper = start_helper()
    try:
        expect("1", {"w1": {"topic1": LEFT1, "topic2": RIGHT1}, "w2": {}})

        set_title(right, RIGHT2)
        expect("2", {"w1": {"topic1": LEFT1, "topic2": RIGHT2}, "w2": {}})

        new = herdr("pane", "split", shell, "--direction", "right", "--no-focus")["result"]["pane"]["pane_id"]
        make_agent(new)
        set_title(new, NEWPANE)
        expect("3a", {"w1": {"topic1": LEFT1, "topic2": RIGHT2}, "w2": {"topic1": NEWPANE}})
        herdr("pane", "close", new)
        expect("3b", {"w1": {"topic1": LEFT1, "topic2": RIGHT2}, "w2": {}})

        helper.kill()
        helper.wait()
        set_title(left, LEFT2)
        rpc("workspace.report_metadata", {"workspace_id": "w2", "source": "autoname", "seq": time.time_ns(),
                                          "tokens": {"topic4": "stale"}})
        left_over = {"w1": {"topic1": LEFT1, "topic2": RIGHT2}, "w2": {"topic4": "stale"}}
        got = {ws: ws_tokens(ws) for ws in left_over}
        check("4 setup: kill -9 leaves tokens behind", got == left_over, f"expected {left_over} got {got}")
        helper = start_helper()
        expect("4", {"w1": {"topic1": LEFT2, "topic2": RIGHT2}, "w2": {}})

        rss_kb, cpu = rss_and_cpu(helper.pid)
        helper.send_signal(signal.SIGTERM)
        expect("5", {"w1": {}, "w2": {}})
        try:
            code = helper.wait(3)
        except subprocess.TimeoutExpired:
            code = None
        check("5 exit code", code == 0, f"expected 0 got {code}")

        print(f"INFO 6 helper RSS {rss_kb} KB, CPU {cpu}")
    finally:
        if helper.poll() is None:
            helper.terminate()
            try:
                helper.wait(3)
            except subprocess.TimeoutExpired:
                helper.kill()
        for ws in ("w1", "w2"):
            clear_tokens(ws)


def check_dead_socket_exit():
    try:
        code = subprocess.run(["/usr/bin/python3", "-I", HELPER, "run"], timeout=5, capture_output=True,
                              env={**ENV, "HERDR_SOCKET_PATH": os.path.join(LAB_DIR.name, "no-herdr.sock")}
                              ).returncode
    except subprocess.TimeoutExpired:
        code = "still running after 5 s"
    check("R run on a dead socket exits", code == 1, f"expected exit code 1 got {code}")


def check_failed_snapshot_survives():
    sock_dir = tempfile.mkdtemp(prefix="hda-", dir="/tmp")
    path = os.path.join(sock_dir, "s")
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(8)
    snapshots, reports, stream = [], [], []
    fake = {"workspaces": [{"workspace_id": "w1"}], "tabs": [], "layouts": [],
            "panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "agent": "claude",
                       "terminal_title_stripped": "fake topic"}]}

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            req = json.loads(conn.makefile().readline())
            if req["method"] == "events.subscribe":
                conn.sendall(b'{"id":"sub","result":{"type":"subscription_started"}}\n')
                stream.append(conn)
                continue
            if req["method"] == "session.snapshot":
                snapshots.append(req)
                if len(snapshots) == 1:
                    conn.close()
                    continue
                reply = {"id": req["id"], "result": {"snapshot": fake}}
            else:
                reports.append(req["params"]["tokens"])
                reply = {"id": req["id"], "result": {"type": "ok"}}
            conn.sendall(json.dumps(reply).encode() + b"\n")
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    helper = subprocess.Popen(["/usr/bin/python3", "-I", HELPER, "run"], env={**ENV, "HERDR_SOCKET_PATH": path},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(lambda: (bool(snapshots and stream), None), 5)
        time.sleep(0.5)
        check("R2 helper survives a failed snapshot", helper.poll() is None,
              f"expected running, exited {helper.poll()}")
        try:
            stream[0].sendall(b'{"event":"pane_updated","data":{}}\n')
        except (IndexError, OSError):
            pass
        want = [{"topic1": "fake topic", "topic2": None, "topic3": None, "topic4": None}]
        ok, got = wait_until(lambda: (reports[:1] == want, reports[:1]), 5)
        check("R2 next event retries the snapshot", ok, f"expected {want} got {got}")
        if stream:
            stream[0].close()
        try:
            code = helper.wait(5)
        except subprocess.TimeoutExpired:
            code = "still running after 5 s"
        check("R2 helper exits when the event stream closes", code == 0, f"expected exit code 0 got {code}")
    finally:
        if helper.poll() is None:
            helper.kill()
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


def linked_plugin():
    plugins = herdr("plugin", "list", "--json")["result"]["plugins"]
    return next((p for p in plugins if p["plugin_id"] == PLUGIN), None)


def invoke(action):
    herdr("plugin", "action", "invoke", f"{PLUGIN}.{action}")


def action_output(action, since_ms):
    """Stdout of the newest finished run of an action started after since_ms."""
    def newest():
        logs = herdr("plugin", "log", "list", "--plugin", PLUGIN, "--limit", "20")["result"]["logs"]
        runs = [e for e in logs if e["started_unix_ms"] >= since_ms and e["command"][-1] == action
                and e.get("action_id") and e["status"] != "running"]
        runs.sort(key=lambda e: e["started_unix_ms"])
        return bool(runs), (runs[-1]["stdout"] or "").strip() if runs else None
    return wait_until(newest, 5, 0.25)[1]


def settle(step, want_tokens, want_helpers, replaced=None, lock_is_helper=False):
    want = {"tokens": want_tokens, "helpers": want_helpers}
    if replaced is not None:
        want["lock pid replaced"] = True
    if lock_is_helper:
        want["lock pid is a helper"] = True

    def state():
        pids = helper_pids()
        got = {"tokens": {ws: ws_tokens(ws) for ws in want_tokens}, "helpers": len(pids)}
        if replaced is not None:
            got["lock pid replaced"] = lock_pid() not in (0, replaced)
        if lock_is_helper:
            got["lock pid is a helper"] = lock_pid() in pids
        return got == want, got
    ok, got = wait_until(state, 3, 0.25)
    check(step, ok, f"expected {want} got {got}")


def run_plugin_steps(baseline, linked_before):
    base = len(baseline)
    if linked_before and linked_before["plugin_root"] != HERE:
        check("P0 plugin linked from this repo", False,
              f"expected {HERE} got {linked_before['plugin_root']}; unlink it first")
        return
    if not linked_before:
        herdr("plugin", "link", HERE)
    entry = linked_plugin()
    check("P0 plugin linked from this repo", entry is not None and entry["plugin_root"] == HERE and entry["enabled"],
          f"expected enabled {HERE} got {entry}")

    both = {"w1": {"topic1": LEFT1, "topic2": RIGHT1}, "w2": {}}
    cleared = {"w1": {}, "w2": {}}
    left, right, shell = establish_start_state()

    invoke("start")
    settle("P1 start action", both, base + 1, lock_is_helper=True)
    print(f"INFO P1 helper phys_footprint {footprint(lock_pid())}")

    first = lock_pid()
    invoke("start")
    settle("P2 second start replaces the helper", both, base + 1, replaced=first, lock_is_helper=True)

    second = lock_pid()
    if second in helper_pids():
        # Pausing the old helper makes both starters see the lock held at once, which natural timing rarely does.
        os.kill(second, signal.SIGSTOP)
        try:
            for proc in [subprocess.Popen([HERDR, "--session", "lab", "plugin", "action", "invoke", f"{PLUGIN}.start"],
                                          env=ENV, stdout=subprocess.DEVNULL) for _ in range(2)]:
                proc.wait(20)
            time.sleep(1.5)
        finally:
            os.kill(second, signal.SIGCONT)
    settle("P2b two overlapping starts leave one helper", both, base + 1, replaced=second, lock_is_helper=True)
    time.sleep(3)
    print(f"INFO P2b helper phys_footprint after 3 s idle {footprint(lock_pid())}")

    t0 = int(time.time() * 1000)
    pid = lock_pid()
    invoke("stop")
    settle("P3 stop action", cleared, base)
    got = action_output("stop", t0)
    check("P3 stop output", got == f"stopped {pid}", f"expected 'stopped {pid}' got {got!r}")

    t0 = int(time.time() * 1000)
    invoke("stop")
    got = action_output("stop", t0)
    check("P3b stop with nothing running", got == "not running", f"expected 'not running' got {got!r}")

    t0 = int(time.time() * 1000)
    stop_lab()
    ensure_lab()
    ensure_shape()
    ok, got = wait_until(lambda: (len(helper_pids()) == base + 1 and lock_pid() in helper_pids(), None), 5, 0.25)
    check("P4 startup hook spawns a helper", ok, f"expected {base + 1} helpers with a lock holder got {helper_pids()}")
    report_titles(lab_panes()[0])
    settle("P4 tokens after restart", both, base + 1, lock_is_helper=True)

    def startup_entry():
        logs = herdr("plugin", "log", "list", "--plugin", PLUGIN, "--limit", "20")["result"]["logs"]
        runs = [e for e in logs if e["started_unix_ms"] >= t0 and e.get("event") == "startup"]
        return bool(runs) and all(e["status"] != "running" for e in runs), [(e["status"], e["exit_code"]) for e in runs]
    ok, got = wait_until(startup_entry, 5, 0.25)
    check("P4 startup entry finishes", ok and got == [("succeeded", 0)],
          f"expected [('succeeded', 0)] got {got}")

    stop_lab()
    ok, got = wait_until(lambda: (len(helper_pids()) == base, helper_pids()), 3, 0.25)
    check("P5 helper exits when the server stops", ok, f"expected {base} helpers got {got}")

    ensure_lab()
    wait_until(lambda: (lock_pid() in helper_pids(), None), 5, 0.25)
    invoke("stop")
    ok, got = wait_until(lambda: (len(helper_pids()) == base, helper_pids()), 3, 0.25)
    check("P5 stop after restart leaves no lab helper", ok, f"expected {base} helpers got {got}")


def stop_lab_helper(fresh):
    """End the lab server's helper, such as the one a plugin startup hook spawns on a fresh server."""
    if fresh and linked_plugin():
        wait_until(lambda: (lock_pid() in helper_pids(), None), 5, 0.25)
    pid = lock_pid()
    if pid in helper_pids():
        os.kill(pid, signal.SIGTERM)
        wait_until(lambda: (pid not in helper_pids(), None), 5, 0.25)


def cleanup(linked_before):
    try:
        fresh = ensure_lab()
        stop_lab_helper(fresh)
        for ws in ("w1", "w2"):
            clear_tokens(ws)
        if not linked_before and linked_plugin():
            herdr("plugin", "unlink", PLUGIN)
    except Exception as e:
        print(f"INFO cleanup: {e!r}")


def run():
    fresh = ensure_lab()
    ensure_shape()
    linked_before = linked_plugin()
    stop_lab_helper(fresh)
    baseline = helper_pids()
    print(f"INFO baseline helpers {baseline}")
    try:
        verify_desired()
        run_helper_steps()
        check_dead_socket_exit()
        check_failed_snapshot_survives()
        run_plugin_steps(baseline, linked_before)
        survivors = [pid for pid in baseline if pid in helper_pids()]
        check("live helpers survive", survivors == baseline, f"expected {baseline} got {survivors}")
    finally:
        cleanup(linked_before)

try:
    run()
except Exception as e:
    failures.append("error")
    print(f"FAIL error: {e!r}")
sys.exit(1 if failures else 0)
