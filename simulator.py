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
    DB1.I0         INT    mode      0 = auto, 1 = manual, 2 = setup
    DB1.R4         REAL   temperature  (deg C, oscillates ~20..80)
    DB1.DI8        DINT   rpm          (0 when not in auto, else ~1500 +/- noise)
    DB1.X12.0      BOOL   error        (occasionally flips true)

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

# Byte offsets inside DB<DB_NUMBER>
OFF_MODE = 0   # INT
OFF_TEMP = 4   # REAL
OFF_RPM = 8    # DINT
OFF_ERROR = 12  # BOOL, bit 0

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
        % (DB_NUMBER, DB_NUMBER, DB_NUMBER, DB_NUMBER))

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
    tick = 0

    while running["on"]:
        tick += 1
        elapsed = time.monotonic() - t0

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

        # --- encode (big-endian) and publish --------------------------------
        util.set_int(buf, OFF_MODE, mode)
        util.set_real(buf, OFF_TEMP, temperature)
        util.set_dint(buf, OFF_RPM, rpm)
        util.set_bool(buf, OFF_ERROR, 0, error)

        ctypes.memmove(db, bytes(buf), DB_SIZE)

        log("mode=%-6s temp=%5.1f  rpm=%-5d error=%s"
            % (MODE_NAMES.get(mode, "?"), temperature, rpm, error))

        time.sleep(UPDATE_INTERVAL)

    server.stop()
    server.destroy()
    log("Stopped.")


if __name__ == "__main__":
    sys.exit(main())
