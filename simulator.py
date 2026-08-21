#!/usr/bin/env python3
"""
S7Comm PLC simulator built on the **native** Snap7 server.

Snap7's server is the de-facto reference S7 PLC simulator and speaks the real
S7comm protocol (ISO-on-TCP / RFC1006, TCP 102) — the same dialect gos7 (and
therefore benthos-umh's `s7comm` input) is tested against. We use
`python-snap7==1.3`, which ships and wraps the native `libsnap7` (works on
linux x86_64 and aarch64).

NOTE: python-snap7 >= 3.0 replaced the server with a pure-Python
reimplementation whose handshake is NOT compatible with gos7 ("ISO : Invalid
PDU received"), so this simulator deliberately pins 1.3.

Exposed datapoints, all in DB1 (big-endian, Siemens layout):

    address        type   meaning
    -----------    -----  -------------------------------------------------
    DB1.I0         INT    mode        0 = auto, 1 = manual, 2 = setup
    DB1.R4         REAL   temperature (deg C, oscillates ~20..80)
    DB1.DI8        DINT   rpm         (0 when not in auto, else ~1500 +/- noise)
    DB1.X12.0      BOOL   error       (occasionally flips true)
    DB1.DI16       DINT   good_parts  monotonic production counter
    DB1.DI20       DINT   scrap_parts monotonic scrap counter

good_parts / scrap_parts only ever count up. Nothing in the simulation loop
resets them: they start at 0 on process start and wrap back to 0 once they pass
S7_COUNTER_MAX, which is the behaviour a real PLC counter shows and which
downstream counter logic has to tolerate.

One part is booked per S7_CYCLE_TIME seconds of *auto-mode runtime* (default 60
=> 1 part/minute while running). The cycle clock is paused in manual/setup mode
— just like rpm drops to 0 there — so the counters plateau instead of banking
cycles and dumping a burst when auto resumes. Each part lands in scrap_parts
with probability S7_SCRAP_RATE, otherwise in good_parts, so good + scrap is the
total part count.

Keep the byte offsets below and the benthos `addresses:` list in sync.
"""

import ctypes
import math
import os
import random
import signal
import sys
import time

import snap7
from snap7.server import Server
from snap7 import util

# python-snap7 1.x exposes the server area constants in snap7.types
try:
    SRV_AREA_DB = snap7.types.srvAreaDB  # type: ignore[attr-defined]
except AttributeError:  # very old/new layouts
    SRV_AREA_DB = 5

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
TCP_PORT = int(os.environ.get("S7_PORT", "102"))
DB_NUMBER = int(os.environ.get("S7_DB", "1"))
DB_SIZE = 64  # bytes; plenty of headroom for the datapoints below
UPDATE_INTERVAL = float(os.environ.get("S7_UPDATE_INTERVAL", "1.0"))  # seconds

# Seconds of auto-mode runtime per produced part. 60 => one part per minute.
# The unprefixed CYCLE_TIME is accepted as an alias so both spellings work.
CYCLE_TIME = float(
    os.environ.get("S7_CYCLE_TIME") or os.environ.get("CYCLE_TIME") or "60"
)
# Share of parts booked as scrap instead of good (0.0 .. 1.0).
SCRAP_RATE = float(os.environ.get("S7_SCRAP_RATE", "0.05"))
# Counter wrap point. Default = DINT max, i.e. ~4000 years at 1 part/minute;
# lower it to exercise overflow handling downstream.
COUNTER_MAX = int(os.environ.get("S7_COUNTER_MAX", str(2**31 - 1)))

if CYCLE_TIME <= 0:
    raise SystemExit("S7_CYCLE_TIME must be > 0 (got %r)" % CYCLE_TIME)
if COUNTER_MAX < 1 or COUNTER_MAX > 2**31 - 1:
    raise SystemExit("S7_COUNTER_MAX must be 1..2147483647 to fit a DINT (got %r)" % COUNTER_MAX)

# Byte offsets inside DB<DB_NUMBER>
OFF_MODE = 0    # INT
OFF_TEMP = 4    # REAL
OFF_RPM = 8     # DINT
OFF_ERROR = 12  # BOOL, bit 0
OFF_GOOD = 16   # DINT
OFF_SCRAP = 20  # DINT

MODE_NAMES = {0: "auto", 1: "manual", 2: "setup"}


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    server = Server()

    # The native Snap7 server keeps a pointer to this ctypes buffer, so writing
    # into it in place is immediately visible to connected clients.
    db = (ctypes.c_uint8 * DB_SIZE)()
    server.register_area(SRV_AREA_DB, DB_NUMBER, db)

    server.start(tcpport=TCP_PORT)
    log(f"S7 simulator (native Snap7) listening on 0.0.0.0:{TCP_PORT}  (DB{DB_NUMBER}, {DB_SIZE} bytes)")
    log("Datapoints: DB%d.I0=mode  DB%d.R4=temperature  DB%d.DI8=rpm  DB%d.X12.0=error"
        "  DB%d.DI16=good_parts  DB%d.DI20=scrap_parts"
        % (DB_NUMBER, DB_NUMBER, DB_NUMBER, DB_NUMBER, DB_NUMBER, DB_NUMBER))
    log("Cycle time: %gs/part while in auto (%.2f parts/min), scrap rate %g%%, counters wrap at %d"
        % (CYCLE_TIME, 60.0 / CYCLE_TIME, SCRAP_RATE * 100.0, COUNTER_MAX))

    running = {"on": True}

    def shutdown(signum, _frame):
        log(f"Received signal {signum}, stopping...")
        running["on"] = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # snap7.util setters operate on a bytearray; fill a scratch buffer then copy
    # it into the registered ctypes area in one shot.
    buf = bytearray(DB_SIZE)

    mode = 0
    t0 = time.monotonic()
    last_now = t0
    tick = 0

    # Production counters. Only ever incremented (and wrapped at COUNTER_MAX);
    # nothing in the loop resets them.
    good_parts = 0
    scrap_parts = 0
    # Accumulated auto-mode runtime not yet converted into parts. Drained by
    # whole cycles, so the long-run rate is exactly one part per CYCLE_TIME
    # regardless of UPDATE_INTERVAL, and CYCLE_TIME < UPDATE_INTERVAL simply
    # books several parts per tick.
    run_seconds = 0.0

    while running["on"]:
        tick += 1
        now = time.monotonic()
        elapsed = now - t0
        dt = now - last_now
        last_now = now

        # --- simulate values -------------------------------------------------
        # Mode: change roughly every ~15s, mostly auto
        if tick % 15 == 0:
            mode = random.choice([0, 0, 0, 1, 2])

        # Temperature: slow sine 20..80 deg C plus a little noise
        temperature = 50.0 + 30.0 * math.sin(elapsed / 20.0) + random.uniform(-1.5, 1.5)

        # RPM: only spins in auto mode
        if mode == 0:
            rpm = int(1500 + 200 * math.sin(elapsed / 7.0) + random.uniform(-50, 50))
        else:
            rpm = 0

        # Error: ~3% chance per tick to be set, otherwise clear
        error = random.random() < 0.03

        # Parts: the cycle clock only runs in auto mode, so the counters freeze
        # (rather than bank cycles) while the machine is in manual/setup.
        if mode == 0:
            run_seconds += dt
        while run_seconds >= CYCLE_TIME:
            if random.random() < SCRAP_RATE:
                scrap_parts = 0 if scrap_parts >= COUNTER_MAX else scrap_parts + 1
            else:
                good_parts = 0 if good_parts >= COUNTER_MAX else good_parts + 1
            run_seconds -= CYCLE_TIME

        # --- encode (big-endian) and publish --------------------------------
        util.set_int(buf, OFF_MODE, mode)
        util.set_real(buf, OFF_TEMP, temperature)
        util.set_dint(buf, OFF_RPM, rpm)
        util.set_bool(buf, OFF_ERROR, 0, error)
        util.set_dint(buf, OFF_GOOD, good_parts)
        util.set_dint(buf, OFF_SCRAP, scrap_parts)

        ctypes.memmove(db, bytes(buf), DB_SIZE)

        log("mode=%-6s temp=%5.1f  rpm=%-5d error=%-5s good=%-8d scrap=%d"
            % (MODE_NAMES.get(mode, "?"), temperature, rpm, error, good_parts, scrap_parts))

        time.sleep(UPDATE_INTERVAL)

    server.stop()
    server.destroy()
    log("Stopped.")


if __name__ == "__main__":
    sys.exit(main())
