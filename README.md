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
| `--name NAME` | on any command: address a second instance (own daemon + CubeMX). Default is `default` |
| `ls` | every instance and what it holds |
| `stop --all` | stop every instance, then kill orphaned CubeMX sessions |
| `status` | what the session actually holds — `running (pid, port, STM32F407V(E-G)Tx, last: set mode I2C1 I2C)`, or `warming up` / `busy: <cmd>` / `no mcu loaded` / `not running` |
| `"<cmd>" ["<cmd>" ...]` | run console commands in order, stop at the first `KO`. `--timeout SECONDS` (default 600), `--raw` |

Commands are CubeMX's own console vocabulary — `load <mcu>`, `config load|saveas`,
`project name|path|toolchain`, `set mode ...`, `set ip parameters ...`, `csv pinout <file>`,
`project generate`. The broker is a pipe, not a wrapper; run `mxbroker "help"` for the full list.

Exit codes: **0** OK · **1** KO or timeout · **2** usage / no daemon / CubeMX not found.

## Several instances

`--name` gives you a second everything — own daemon, own port, own CubeMX, own session
state. Two projects, no clobbering:

```powershell
mxbroker start                            # the 'default' instance
mxbroker --name h7 start
mxbroker "load STM32G474RETx"
mxbroker --name h7 "load STM32F407VGTx"

mxbroker ls
  default    pid 38932   STM32G474R(B-C-E)Tx, last: load STM32G474RETx
  h7         pid 18948   STM32F407V(E-G)Tx, last: load STM32F407VGTx
```

Each instance costs ~830 MB and a ~15s boot, so this is for parallel work, not for tidiness.

**Two CubeMX instances must not boot at the same time.** They share `~/.stm32cubemx`
(prefs, MCU/pack DB), and the second one to start into that contention wedges permanently:
0% CPU, no dialog, its window stuck on the bare `STM32CubeMX` title. Started one after the
other they coexist happily, so `start` waits for any other instance to finish warming up
before it spawns — you cannot hit this by accident, but it does mean a `start` can block
for ~15s and say so.

### Reaping strays

`stop` shuts down one instance. `stop --all` shuts down every instance, then kills CubeMX
sessions whose daemon is gone — kill a daemon from Task Manager and its `java.exe` lives on
holding ~830 MB with nothing to reap it:

```
mxbroker ls
  default    pid 38932   STM32G474R(B-C-E)Tx, last: load STM32G474RETx
  h7         stale, removed
mxbroker stop --all
  stopped default (pid 38932)
  killed 1 orphaned CubeMX process(es)
```

Only `java.exe` processes running `STM32CubeMX.exe ... -i` are candidates. A CubeMX you
opened yourself runs under its own launcher without `-i`, so the reaper cannot touch it.

## Paths with spaces

CubeMX splits its command line on whitespace, so a path with a space must reach it quoted —
and PowerShell 5.1 *strips* embedded quotes when it calls a native command, so the obvious
spelling silently truncates the path at the space:

```powershell
mxbroker "project path C:\Users\me\Nouveau dossier"    # -> CubeMX gets ...\Nouveau
mxbroker 'project path "C:\Users\me\Nouveau dossier"'  # -> same, quotes eaten
```

Pipe the commands in instead — stdin is never re-parsed by the shell:

```powershell
'project path "C:\Users\me\Nouveau dossier"' | mxbroker -
@('project path "C:\out dir"', 'config saveas "C:\out dir\app.ioc"') | mxbroker -
```

Or escape the inner quotes as `\"` if you want to stay on the argument form:

```powershell
mxbroker 'project path \"C:\Users\me\Nouveau dossier\"'
$cmd = 'config saveas \"' + $dir + '\app.ioc\"'; mxbroker $cmd
```

No trailing backslash inside the quotes — `dossier\` + `\"` collapses into an escaped
backslash and the quote is lost.

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

State lives in `.mxbroker-<name>.json` beside the script (port, pid, auth token); daemon
stdout goes to `.mxbroker-<name>.log`. The socket is loopback-only and token-checked — any local process can
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
`mxbroker restart`. A command that hits `--timeout` keeps running inside CubeMX; the next
command first waits for the old one's (discarded) output to finish arriving, so replies can
never shift onto the wrong command — if it is still going, you get a `KO` telling you to wait
or restart.

**No windows.** `-i` cannot run headless — `-Djava.awt.headless=true` dies at startup with
`java.awt.HeadlessException` — so CubeMX always builds its Swing frame. The daemon hides it
(`ShowWindow(SW_HIDE)`) once boot finishes, and spawns `java.exe` with `CREATE_NO_WINDOW` so
Windows doesn't hand the detached child a console of its own. `mxbroker start --gui` skips the
hide sweep and leaves the frame on screen — worth doing if a command ever hangs, since a modal
dialog is the usual cause (the STM32H7 power-supply prompt is the known one; those open after
the sweep and stay visible either way). For the stateless `-q` path (fresh process per call, parameter discovery
from the IP database) see the sibling `cubemx-tools` project.
