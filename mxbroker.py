#!/usr/bin/env python3
"""mxbroker - one warm STM32CubeMX '-i' session behind a stateless CLI.

cubemx-tools drives 'STM32CubeMX.exe -q <script>': a fresh JVM+GUI boot (~12s) per
call. This holds ONE '-i' session in a detached daemon instead, so every CLI
invocation is just a loopback round-trip and the loaded MCU / project state survives
between separate commands.

Wire protocol of 'STM32CubeMX.exe -i' (verified on 6.18.0-RC3):
  - prompt is "MX> " with NO trailing newline, printed before the command's output
  - every response ends with a line matching '^<digits> (OK|KO)$'  -> delimiter + status
  - 'exit' prints "Bye bye" and the process ends, with no OK/KO line
"""

import ctypes
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue

HERE = Path(__file__).resolve().parent
NAME = "default"          # instance selected by --name; each gets its own daemon + CubeMX
STATE = HERE / ".mxbroker-default.json"
LOG = HERE / ".mxbroker-default.log"
PROMPT = "MX> "
DEFAULT_TIMEOUT = 600
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP - daemon must not hold the terminal.
DETACHED = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0
# CREATE_NO_WINDOW - the daemon has no console, so java.exe would be handed a fresh
# (empty) console window of its own. Mutually exclusive with DETACHED_PROCESS.
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

DONE = re.compile(r"^\d+ (OK|KO)$")
NOISE = re.compile(r"^\d{4}-\d{2}-\d{2}|^Picked up |^log4j ")
# 'get mode_param_list' & friends answer through log4j, not the console: their result is
# '<ts> [INFO] IP:1882 - I2C1 | ClockSpeed = 100000'. Every other timestamped line is
# plumbing (IPUIPlugin, PackLoader, PluginManager, ...), so keep this one logger and
# strip its prefix. Anything else buried in the log stream: run with --raw.
RESULT = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\S+\s+\[\w+\]\s+IP:\d+ - (.*)$")


# ---------------------------------------------------------------- pure helpers

def done_status(line):
    """'67768 OK' -> 'OK', '2 KO' -> 'KO', anything else -> None."""
    m = DONE.match(line.strip())
    return m.group(1) if m else None


def clean(text):
    """Strip launcher/log4j noise and bare prompts. Same rules as CubeMx.java:110."""
    out = []
    for line in text.replace(PROMPT, "", 1).split("\n"):
        line = line.rstrip("\r")
        result = RESULT.match(line)
        if result:
            out.append(result.group(1))
            continue
        if (NOISE.match(line)
                or "java.util.prefs" in line
                or "Could not open/create prefs" in line
                or line.strip() in ("", "MX>")):
            continue
        out.append(line)
    return "\n".join(out).strip()


def hide_windows(pid):
    """Hide every visible top-level window owned by pid (SW_HIDE).

    '-i' cannot run headless (-Djava.awt.headless=true dies with HeadlessException), so
    CubeMX always builds its Swing frame; nothing requires it to be on screen.
    ponytail: one sweep once CubeMX has booted. A modal dialog opened later (the STM32H7
    power-supply prompt is the known one) stays visible - which is what you want, since it
    is also what wedges the console. 'mxbroker start --gui' skips this entirely.
    """
    if sys.platform != "win32":
        return 0
    user32 = ctypes.windll.user32
    hidden = []

    def cb(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 0)
            hidden.append(hwnd)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(cb), None)
    return len(hidden)


def resolve_home(explicit):
    for cand in (explicit, os.environ.get("CUBEMX_HOME")):
        if cand:
            p = Path(cand)
            if (p / "STM32CubeMX.exe").exists():
                return p
            die(2, f"STM32CubeMX.exe not found in {p}")
    die(2, "STM32CubeMX.exe not found. Set CUBEMX_HOME or pass --cubemx-home <install dir>.")


# ---------------------------------------------------------------- MX session

class Session:
    """The single live 'STM32CubeMX.exe -i' process. One command at a time."""

    def __init__(self, home, show_gui=False):
        self.show_gui = show_gui
        self.last = "-"  # last command handed to MX, for 'status'
        java = home / "jre" / "bin" / "java.exe"
        self.proc = subprocess.Popen(
            [str(java) if java.exists() else "java", "-jar",
             str(home / "STM32CubeMX.exe"), "-i"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
            creationflags=NO_WINDOW)
        self.q = Queue()
        self.lock = threading.Lock()
        self.ready = False
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._warmup, daemon=True).start()

    def _pump(self):
        # Char-at-a-time: the prompt has no newline, so readline() would never yield it
        # and we could not tell "MX is ready" from "MX is still booting".
        buf = ""
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:
                break
            buf += ch
            if ch == "\n" or buf.endswith(PROMPT):
                self.q.put(buf)
                buf = ""
        self.q.put(None)

    def _warmup(self):
        """Swallow the ~200 lines of boot noise; hold the lock so requests just wait."""
        with self.lock:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                try:
                    item = self.q.get(timeout=deadline - time.monotonic())
                except Empty:
                    break
                if item is None or item.endswith(PROMPT):
                    break
            for _ in range(50 if not self.show_gui else 0):
                if hide_windows(self.proc.pid):  # frame can map a moment after the prompt
                    break
                time.sleep(0.2)
            self.ready = True

    def run(self, cmd, timeout):
        with self.lock:
            if self.proc.poll() is not None:
                return "CubeMX session died (exit %s)" % self.proc.returncode, "KO"
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
            chunks, deadline = [], time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return "".join(chunks), "TIMEOUT"
                try:
                    item = self.q.get(timeout=left)
                except Empty:
                    return "".join(chunks), "TIMEOUT"
                if item is None:
                    return "".join(chunks), "KO"
                chunks.append(item)
                status = done_status(item)
                if status:  # raw on the wire; the client decides how to filter it
                    return "".join(chunks), status

    def state(self):
        """One line of what the session actually holds - the point of 'status'.

        A warm session is mutable shared state, so 'daemon is up' is not the useful
        answer; 'which MCU is loaded' is. Asked of CubeMX rather than inferred from the
        commands seen, because 'config load' changes it too.
        """
        if self.proc.poll() is not None:
            return "CubeMX exited"
        if not self.ready:
            return "warming up"
        # ponytail: benign race - a command starting right here just makes status queue
        # behind it, same as any other request.
        if self.lock.locked():
            return f"busy: {self.last}"
        out, status = self.run("get mcu name", 30)
        lines = [l for l in clean(out).splitlines() if not done_status(l)]
        mcu = lines[0] if status == "OK" and lines else "no mcu loaded"
        return f"{mcu}, last: {self.last}"

    def kill(self):
        try:
            self.proc.stdin.write("exit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=15)
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)  # so 'stop' does not return while CubeMX is still teardown


# ---------------------------------------------------------------- daemon

def serve(home, show_gui=False):
    session = Session(home, show_gui)
    token = os.urandom(16).hex()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                req = json.loads(self.rfile.readline() or "{}")
            except ValueError:
                return
            if req.get("token") != token:
                self.wfile.write(b'{"output":"bad token","status":"KO"}\n')
                return
            op = req.get("op", "run")
            if op == "ping":
                reply = {"output": session.state(), "status": "OK"}
            elif op == "stop":
                reply = {"output": "stopped", "status": "OK"}
                threading.Thread(target=shutdown, daemon=True).start()
            else:
                session.last = req["cmd"]
                out, status = session.run(req["cmd"], req.get("timeout", DEFAULT_TIMEOUT))
                reply = {"output": out, "status": status}
            self.wfile.write((json.dumps(reply) + "\n").encode())

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)

    def shutdown():
        session.kill()
        server.shutdown()

    port = server.server_address[1]
    STATE.write_text(json.dumps(
        {"port": port, "pid": os.getpid(), "token": token, "cubemx_home": str(home)}))
    print(f"daemon up (pid {os.getpid()}, port {port}, home {home})", flush=True)
    try:
        server.serve_forever()
    finally:
        session.kill()
        STATE.unlink(missing_ok=True)


# ---------------------------------------------------------------- client

def set_instance(name):
    global NAME, STATE, LOG
    NAME = name
    STATE = HERE / f".mxbroker-{name}.json"
    LOG = HERE / f".mxbroker-{name}.log"


def cubemx_pids():
    """PIDs of broker-spawned CubeMX sessions: java.exe running 'STM32CubeMX.exe ... -i'.

    A hand-launched CubeMX runs under its own launcher as STM32CubeMX.exe and has no -i,
    so the reaper can never take out a GUI session you opened yourself.
    """
    if sys.platform != "win32":
        return []
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
         "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
        capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit() and "STM32CubeMX.exe" in cmd and cmd.rstrip().endswith("-i"):
            pids.append(int(pid))
    return pids


def instances():
    return sorted(f.stem.split("-", 1)[1] for f in HERE.glob(".mxbroker-*.json"))


def load_state():
    if not STATE.exists():
        return None
    try:
        return json.loads(STATE.read_text())
    except ValueError:
        return None


def ask(state, req, timeout):
    sock = socket.create_connection(("127.0.0.1", state["port"]), timeout=10)
    sock.settimeout(timeout + 30)
    with sock:
        req["token"] = state["token"]
        sock.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            part = sock.recv(65536)
            if not part:
                break
            data += part
    return json.loads(data.decode())


def alive(state):
    """A connect+ping is the only honest liveness check; no pid tables needed."""
    if not state:
        return None
    try:
        return ask(state, {"op": "ping"}, 10)
    except OSError:
        return None


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def pop_opt(args, name, default=None):
    if name not in args:
        return default
    i = args.index(name)
    if i + 1 >= len(args):
        die(2, f"{name} needs a value")
    args.pop(i)
    return args.pop(i)


def pop_flag(args, name):
    if name not in args:
        return False
    args.remove(name)
    return True


USAGE = """mxbroker - stateless CLI over a warm STM32CubeMX -i session

Any command takes --name NAME to address a second instance (own daemon, own CubeMX,
~830MB each); without it you get the 'default' one.

  mxbroker start [--cubemx-home DIR] [--gui]
                                       launch the daemon (CUBEMX_HOME is the fallback).
                                       CubeMX's window is hidden unless you pass --gui.
  mxbroker stop                        exit CubeMX, shut the daemon down
  mxbroker stop --all                  stop every instance, then kill CubeMX sessions
                                       whose daemon died and left them running
  mxbroker restart [--cubemx-home DIR] [--gui]
  mxbroker status
  mxbroker ls                          every instance and what it holds
  mxbroker "<cmd>" ["<cmd>" ...]       run console commands, stop at the first KO
                                       [--timeout SECONDS, default 600] [--raw]
                                       --raw prints CubeMX's log stream unfiltered
  ... | mxbroker -                     read commands from stdin, one per line. Use this
                                       for paths with spaces - no shell quoting to fight:
                                         'project path "C:\\out dir"' | mxbroker -

Console commands are CubeMX's own: load <mcu>, config load|saveas, project name|path|
toolchain, set mode ..., set ip parameters ..., csv pinout <file>, project generate.
Exit codes: 0 OK, 1 KO or timeout, 2 usage / no daemon / CubeMX not found."""


def wait_for_warmups(cap=180):
    """Never let two CubeMX instances boot at the same time.

    They share ~/.stm32cubemx (prefs, MCU/pack DB), and a second one booting into that
    contention wedges permanently: 0% CPU, no dialog, its frame stuck on the bare title.
    Staggered, instances coexist fine - so just queue the warmups.
    """
    mine, deadline, told = NAME, time.monotonic() + cap, False
    while time.monotonic() < deadline:
        booting = None
        for other in instances():
            if other == mine:
                continue
            set_instance(other)  # load_state/alive read the globals; restored below
            ping = alive(load_state())
            if ping and ping["output"] == "warming up":
                booting = other
                break
        set_instance(mine)
        if not booting:
            return
        if not told:
            print(f"waiting for {booting} to finish booting (two at once wedge each other)")
            told = True
        time.sleep(2)


def cmd_start(args):
    gui = ["--gui"] if pop_flag(args, "--gui") else []
    home = resolve_home(pop_opt(args, "--cubemx-home"))
    if alive(load_state()):
        print("already running")
        return 0
    wait_for_warmups()
    STATE.unlink(missing_ok=True)
    with open(LOG, "w") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                          "_daemon", "--name", NAME, "--cubemx-home", str(home)] + gui,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=DETACHED, close_fds=True)
    for _ in range(100):
        state = load_state()
        if state:
            print(f"daemon up ({NAME}, pid {state['pid']}, port {state['port']})")
            print("CubeMX is warming up (~15s); the first command will wait for it.")
            return 0
        time.sleep(0.1)
    die(2, f"daemon did not start; see {LOG}")


def cmd_ls():
    if not instances():
        print("no instances")
        return 2
    for name in instances():
        set_instance(name)
        state = load_state()
        ping = alive(state)
        if ping:
            print(f"{name:<10} pid {state['pid']:<7} {ping['output']}")
        else:
            STATE.unlink(missing_ok=True)
            print(f"{name:<10} stale, removed")
    return 0


def cmd_stop_all():
    for name in instances():
        set_instance(name)
        state = load_state()
        if alive(state):
            ask(state, {"op": "stop"}, 30)
            for _ in range(200):
                if not STATE.exists():
                    break
                time.sleep(0.1)
            print(f"stopped {name} (pid {state['pid']})")
        else:
            STATE.unlink(missing_ok=True)
    # Every daemon is down now, so a '-i' session still alive has lost its owner: killing
    # a daemon from Task Manager leaves ~830MB of CubeMX behind with nothing to reap it.
    orphans = cubemx_pids()
    for pid in orphans:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    print(f"killed {len(orphans)} orphaned CubeMX process(es)" if orphans else "no orphans")
    return 0


def main(argv):
    args = argv[1:]
    # 'help' is NOT listed here on purpose: it is CubeMX's own command, so it forwards.
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if args else 2

    set_instance(pop_opt(args, "--name", "default"))
    if not args:
        die(2, "--name needs a command after it")
    op = args[0]
    if op == "ls":
        return cmd_ls()
    if op == "_daemon":
        serve(resolve_home(pop_opt(args, "--cubemx-home")), pop_flag(args, "--gui"))
        return 0
    if op == "start":
        return cmd_start(args[1:])
    if op == "restart":
        main([argv[0], "stop"])
        return cmd_start(args[1:])

    # Paths with spaces need quotes MX can see, but PowerShell strips embedded quotes on
    # the way to a native command. Piping the commands in dodges argv parsing entirely.
    if op == "-":
        # PowerShell puts a UTF-8 BOM ahead of the first piped line; CubeMX would read it
        # as part of the command name and answer with a usage dump.
        piped = sys.stdin.read().lstrip("﻿ï»¿")
        args = args[1:] + [l.strip() for l in piped.splitlines() if l.strip()]

    state = load_state()
    if op == "status":
        ping = alive(state)
        if not ping:
            STATE.unlink(missing_ok=True)
            print("not running")
            return 2
        print(f"running (pid {state['pid']}, port {state['port']}, {ping['output']})")
        return 0
    if op == "stop":
        if pop_flag(args, "--all"):
            return cmd_stop_all()
        if not alive(state):
            STATE.unlink(missing_ok=True)
            print("not running")
            return 0
        ask(state, {"op": "stop"}, 30)
        for _ in range(200):  # daemon removes the state file as it goes down
            if not STATE.exists():
                break
            time.sleep(0.1)
        print("stopped")
        return 0

    raw = pop_flag(args, "--raw")
    timeout = int(pop_opt(args, "--timeout", DEFAULT_TIMEOUT))
    if not alive(state):
        die(2, "daemon not running - start it with: mxbroker start")
    for cmd in args:
        reply = ask(state, {"op": "run", "cmd": cmd, "timeout": timeout}, timeout)
        print(reply["output"] if raw else clean(reply["output"]))
        if reply["status"] != "OK":
            print(f"[{reply['status']}] {cmd}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
