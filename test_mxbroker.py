"""Self-check for the response parser and the session read loop. No CubeMX needed."""

import io
import tempfile
import threading
from pathlib import Path
from queue import Queue

import mxbroker
from mxbroker import Session, clean, done_status

# the two terminators seen on the wire
assert done_status("67768 OK\r") == "OK"
assert done_status("2 KO") == "KO"
assert done_status("74 OK\r\n") == "OK"
# ...and things that only look like them
assert done_status("OK") is None
assert done_status("MX> 67768 OK") is None
assert done_status("Loading OK for 1 mcu") is None
assert done_status("") is None

RAW = """MX> log4j user configuration file not found: C:\\Users\\x/.stm32cubemx/log4j2.xml
2026-08-12 00:02:04,993 [INFO] MicroXplorer:468 - Change Database Path :
2026-08-12 00:33:04,391 [INFO] IPUIPlugin:83 - create IPUIPlugin
Picked up JAVA_TOOL_OPTIONS: -Dfoo
Could not open/create prefs root node
java.util.prefs.WindowsPreferences
PA0 : ADC1_IN0
2026-08-12 00:33:04,391 [INFO] IP:1882 - I2C1 | ClockSpeed = 100000

67768 OK
MX> """
out = clean(RAW)
# the IP logger carries 'get mode_param_list' results; every other timestamped line is noise
assert out.splitlines() == ["PA0 : ADC1_IN0",
                            "I2C1 | ClockSpeed = 100000",
                            "67768 OK"], out
assert clean("MX> Bye bye") == "Bye bye"


# --- Session.run against a hand-fed queue: normal reply, timeout, and the desync
# --- hazard (a timed-out command's late output must not answer the next command).

class FakeProc:
    returncode = None
    stdin = io.StringIO()
    def poll(self):
        return None

s = Session.__new__(Session)
s.proc, s.q, s.lock, s.ready, s.stale = FakeProc(), Queue(), threading.Lock(), True, 0

s.q.put("PA0 : ADC1_IN0\n")
s.q.put("12 OK\n")
assert s.run("csv pinout x", 5) == ("PA0 : ADC1_IN0\n12 OK\n", "OK")

# slow command times out with nothing read yet
assert s.run("project generate", 0.05) == ("", "TIMEOUT")
assert s.stale == 1

# ...its output lands later; the next command must skip past the old terminator
s.q.put("generating...\n")
s.q.put("99 OK\n")          # terminator of the timed-out command
s.q.put("v6.18.0\n")
s.q.put("3 OK\n")           # terminator of OUR command
assert s.run("get version", 5) == ("v6.18.0\n3 OK\n", "OK")
assert s.stale == 0

# still running and nothing arrives: honest KO, and still owed one terminator
assert s.run("x", 0.05) == ("", "TIMEOUT")
assert s.run("y", 0.05)[1] == "KO"
assert s.stale == 1


# --- CLI routing: --name must reach 'restart', and must never escape the script dir.

mxbroker.HERE = Path(tempfile.mkdtemp())  # so no real instance's state file is touched

started = []
mxbroker.cmd_start = lambda args: started.append(mxbroker.NAME) or 0
mxbroker.main(["mxbroker", "--name", "h7", "restart"])
assert started == ["h7"], started  # used to stop and restart 'default' instead

for bad in ("../x", r"..\\outside", "a/b", r"C:\x", "", "no spaces"):
    try:
        mxbroker.main(["mxbroker", "--name", bad, "status"])
    except SystemExit as e:
        assert e.code == 2, bad
    else:
        assert False, f"accepted --name {bad!r}"

print("ok")
