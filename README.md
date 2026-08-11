# mxbroker

A stateless CLI over a **warm** STM32CubeMX session.

`STM32CubeMX.exe -q script.txt` boots a fresh JVM + GUI stack for every call — ~12s each,
and nothing survives between calls except the `.ioc` on disk. `mxbroker` keeps one
`STM32CubeMX.exe -i` session alive in a detached daemon instead. Each CLI invocation is a
loopback round-trip to it: **~1s**, and the loaded MCU / project state is still there.

No install, no dependencies — Python 3.11 stdlib only.

## Use

```
set CUBEMX_HOME=C:\Users\ayoub\Documents\mx

mxbroker start                          daemon up (pid 31932, port 53154)   [returns in 0.2s]
mxbroker "load STM32F407VGTx"           7779 OK      (first call waits ~8s for CubeMX warmup)
mxbroker "project name brokertest"      40 OK        (1.0s — different process, MCU still loaded)
mxbroker "csv pinout out\pins.csv"      13 OK        (0.8s — 100 pins, proof the session is live)
mxbroker stop
```

| Command | |
|---|---|
| `start [--cubemx-home DIR] [--gui]` | launch the daemon; `CUBEMX_HOME` is the fallback. `--gui` leaves CubeMX's window on screen, otherwise it is hidden |
| `stop` | send `exit` to CubeMX, shut the daemon down |
| `restart [--cubemx-home DIR] [--gui]` | |
| `status` | what the session actually holds — `running (pid, port, STM32F407V(E-G)Tx, last: set mode I2C1 I2C)`, or `warming up` / `busy: <cmd>` / `no mcu loaded` / `not running` |
| `"<cmd>" ["<cmd>" ...]` | run console commands in order, stop at the first `KO`. `--timeout SECONDS` (default 600), `--raw` |

Commands are CubeMX's own console vocabulary — `load <mcu>`, `config load|saveas`,
`project name|path|toolchain`, `set mode ...`, `set ip parameters ...`, `csv pinout <file>`,
`project generate`. The broker is a pipe, not a wrapper; run `mxbroker "help"` for the full list.

Exit codes: **0** OK · **1** KO or timeout · **2** usage / no daemon / CubeMX not found.

## How it works

The `-i` console prints `MX> ` as a prompt (no trailing newline) and terminates every
response with a line matching `^<digits> (OK|KO)$` — that line is both the end-of-response
delimiter and the status. The daemon reads stdout a character at a time (a `readline()` loop
would never see the newline-less prompt, so it could not tell "ready" from "still booting")
and serializes requests behind one lock; the client filters log4j noise out of the reply.

Most commands answer on the console (`get version` → `6.18.0`), but a few answer **through
log4j** instead — `get mode_param_list I2C1 I2C` emits
`<ts> [INFO] IP:1882 - I2C1 | ClockSpeed = 100000`. Those lines are kept and their prefix
stripped; every other timestamped line is CubeMX plumbing and is dropped. If something you
expect is still missing, `--raw` prints the stream untouched.

State lives in `.mxbroker.json` beside the script (port, pid, auth token); daemon stdout goes
to `.mxbroker.log`. The socket is loopback-only and token-checked — any local process can
connect to a listening port.

## Test

```
python test_mxbroker.py     # ok
```

Covers the response parser: `67768 OK` / `2 KO` terminate a response with the right status,
lookalikes don't, and log4j noise is stripped. No CubeMX process required.

## Limits

A warm session is **shared mutable state** — the opposite of the `-q` path, where every call
is a fresh process and the `.ioc` is the only truth. That is the trade this tool makes for
speed, so `status` reports what CubeMX is actually holding (asked of CubeMX, not inferred —
`config load` changes the MCU too) rather than just "daemon up". When in doubt,
`config load <ioc>` before a batch and you are back on solid ground.

One session, one command at a time. A crashed CubeMX is reported as `KO`, not auto-restarted —
`mxbroker restart`.

**No windows.** `-i` cannot run headless — `-Djava.awt.headless=true` dies at startup with
`java.awt.HeadlessException` — so CubeMX always builds its Swing frame. The daemon hides it
(`ShowWindow(SW_HIDE)`) once boot finishes, and spawns `java.exe` with `CREATE_NO_WINDOW` so
Windows doesn't hand the detached child a console of its own. `mxbroker start --gui` skips the
hide sweep and leaves the frame on screen — worth doing if a command ever hangs, since a modal
dialog is the usual cause (the STM32H7 power-supply prompt is the known one; those open after
the sweep and stay visible either way). For the stateless `-q` path (fresh process per call, parameter discovery
from the IP database) see the sibling `cubemx-tools` project.
