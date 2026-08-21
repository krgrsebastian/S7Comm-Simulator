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

Pick a machine with S7_MACHINE_TYPE: `generic` (the default, unchanged from
v0.2), `cnc`, `oven` or `washing`. Every profile publishes the same ten core
words in DB1 bytes 0..31 and its own process parameters from byte 32 up; see
README.md for the layouts.

This module owns the wire and the counters. The process models live in
`profiles/`, which never imports snap7 so it can be driven by the test suite
without a PLC.
"""

import ctypes
import os
import random
import signal
import sys
import time

from profiles import PROFILES
from profiles.core import (
    BOOL,
    CORE_DATAPOINTS,
    DINT,
    INT,
    MODE_AUTO,
    MODE_MANUAL,
    MODE_NAMES,
    MODE_SETUP,
    REAL,
    STATE_NAMES,
    resolve_state,
)

# The benthos-umh s7comm input reads at most this many addresses per
# AGReadMulti (s7comm_plugin/s7comm.go: maxItemsPerBatch). Beyond it a profile
# is read in two round-trips that arrive looking like one snapshot, so a
# measurement can tear from its counter.
BENTHOS_MAX_ITEMS_PER_BATCH = 20

# ---------------------------------------------------------------------------
# Configuration — seven variables, and only S7_MACHINE_TYPE is new in v0.3.
# Everything about a machine's process is a constant in its profile.
# ---------------------------------------------------------------------------
TCP_PORT = int(os.environ.get("S7_PORT", "102"))
DB_NUMBER = int(os.environ.get("S7_DB", "1"))
DB_SIZE = 128  # bytes; core block 0..31, profile block 32 up
UPDATE_INTERVAL = float(os.environ.get("S7_UPDATE_INTERVAL", "1.0"))  # seconds
MACHINE_TYPE = (
    os.environ.get("S7_MACHINE_TYPE") or os.environ.get("MACHINE_TYPE") or "generic"
).strip().lower()
SCRAP_RATE = float(os.environ.get("S7_SCRAP_RATE", "0.05"))
COUNTER_MAX = int(os.environ.get("S7_COUNTER_MAX", str(2**31 - 1)))

if MACHINE_TYPE not in PROFILES:
    raise SystemExit(
        "S7_MACHINE_TYPE must be one of %s (got %r)"
        % (", ".join(sorted(PROFILES)), MACHINE_TYPE)
    )
PROFILE_CLASS = PROFILES[MACHINE_TYPE]

# Seconds per machine cycle: one part on a part-at-a-time machine, one charge or
# basket on a batch machine. Default comes from the profile.
CYCLE_TIME = float(
    os.environ.get("S7_CYCLE_TIME")
    or os.environ.get("CYCLE_TIME")
    or PROFILE_CLASS.DEFAULT_CYCLE_TIME
)

if CYCLE_TIME <= 0:
    raise SystemExit("S7_CYCLE_TIME must be > 0 (got %r)" % CYCLE_TIME)
if COUNTER_MAX < 1 or COUNTER_MAX > 2**31 - 1:
    raise SystemExit(
        "S7_COUNTER_MAX must be 1..2147483647 to fit a DINT (got %r)" % COUNTER_MAX
    )

DATAPOINTS = CORE_DATAPOINTS + PROFILE_CLASS.DATAPOINTS


def log(msg: str) -> None:
    print(msg, flush=True)


def addresses_block(db: int = DB_NUMBER) -> str:
    """The benthos `addresses:` list for this profile, ready to paste."""
    width = max(len(dp.address(db)) for dp in DATAPOINTS) + 2
    lines = []
    for dp in DATAPOINTS:
        quoted = '"%s"' % dp.address(db)
        lines.append("      - %s# %s" % (quoted.ljust(width + 1), dp.name))
    return "\n".join(lines)


def encode(buf: bytearray, values: dict, util) -> None:
    """Write every datapoint into the scratch buffer, big-endian."""
    for dp in DATAPOINTS:
        value = values[dp.name]
        if dp.kind == INT:
            util.set_int(buf, dp.offset, int(value))
        elif dp.kind == DINT:
            util.set_dint(buf, dp.offset, int(value))
        elif dp.kind == REAL:
            util.set_real(buf, dp.offset, float(value))
        elif dp.kind == BOOL:
            util.set_bool(buf, dp.offset, dp.bit, bool(value))
        else:  # pragma: no cover - guarded by the DP definitions
            raise ValueError("unknown kind %r for %s" % (dp.kind, dp.name))


def bump(counter: int, delta: int) -> int:
    """Advance a counter, wrapping to 0 past COUNTER_MAX. Never decreases."""
    for _ in range(delta):
        counter = 0 if counter >= COUNTER_MAX else counter + 1
    return counter


def main() -> int:
    if "--addresses" in sys.argv[1:]:
        # Deliberately before the snap7 import: dumping the layout is a pure
        # bookkeeping job and must work anywhere, native library or not.
        print(addresses_block())
        return 0

    import snap7
    from snap7.server import Server
    from snap7 import util

    # python-snap7 1.x exposes the server area constants in snap7.types
    try:
        srv_area_db = snap7.types.srvAreaDB  # type: ignore[attr-defined]
    except AttributeError:  # very old/new layouts
        srv_area_db = 5

    if PROFILE_CLASS.block_end() > DB_SIZE:
        raise SystemExit(
            "profile %s needs %d bytes but DB_SIZE is %d"
            % (MACHINE_TYPE, PROFILE_CLASS.block_end(), DB_SIZE)
        )

    rng = random.Random()
    profile = PROFILE_CLASS(cycle_time=CYCLE_TIME, scrap_rate=SCRAP_RATE, rng=rng)

    server = Server()
    # The native Snap7 server keeps a pointer to this ctypes buffer, so writing
    # into it in place is immediately visible to connected clients.
    db = (ctypes.c_uint8 * DB_SIZE)()
    server.register_area(srv_area_db, DB_NUMBER, db)
    server.start(tcpport=TCP_PORT)

    log(
        "S7 simulator (native Snap7) listening on 0.0.0.0:%d  (DB%d, %d bytes)"
        % (TCP_PORT, DB_NUMBER, DB_SIZE)
    )
    log("Machine: %s — %s" % (MACHINE_TYPE, PROFILE_CLASS.DESCRIPTION))
    log(
        "Cycle: %gs per cycle, %d part(s) per cycle; counters wrap at %d"
        % (CYCLE_TIME, PROFILE_CLASS.PARTS_PER_CYCLE, COUNTER_MAX)
    )
    if PROFILE_CLASS is PROFILES["cnc"]:
        log(
            "Note: S7_SCRAP_RATE is not used by the cnc profile — every scrapped "
            "part is one that measured outside tolerance."
        )
    log("Datapoints (%d): %s" % (len(DATAPOINTS), "  ".join(
        "%s=%s" % (dp.address(), dp.name) for dp in DATAPOINTS)))
    if PROFILE_CLASS.ALARMS:
        log("Alarms: %s" % "  ".join(
            "%d=%s%s" % (code, name, "" if stopping else " (warning)")
            for code, (name, stopping) in sorted(PROFILE_CLASS.ALARMS.items())))
    if len(DATAPOINTS) > BENTHOS_MAX_ITEMS_PER_BATCH:
        log(
            "WARNING: %d addresses exceeds benthos-umh's %d per AGReadMulti — it "
            "will read this profile in several round-trips that look like one "
            "snapshot." % (len(DATAPOINTS), BENTHOS_MAX_ITEMS_PER_BATCH)
        )

    running = {"on": True}

    def shutdown(signum, _frame):
        log("Received signal %s, stopping..." % signum)
        running["on"] = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # snap7.util setters operate on a bytearray; fill a scratch buffer then copy
    # it into the registered ctypes area in one shot.
    buf = bytearray(DB_SIZE)

    mode = MODE_AUTO
    good_parts = 0
    scrap_parts = 0
    part_id = 0
    last_now = time.monotonic()
    tick = 0

    while running["on"]:
        tick += 1
        now = time.monotonic()
        dt = now - last_now
        last_now = now

        # Mode is the operator: mostly auto, changing roughly every ~15s.
        if tick % 15 == 0:
            mode = rng.choice([MODE_AUTO, MODE_AUTO, MODE_AUTO, MODE_MANUAL, MODE_SETUP])

        # Production time only runs in auto; the profile additionally freezes
        # itself while a stopping fault is latched.
        dt_run = dt if mode == MODE_AUTO else 0.0
        result = profile.step(dt, dt_run)

        good_parts = bump(good_parts, result.good_delta)
        scrap_parts = bump(scrap_parts, result.scrap_delta)
        part_id = bump(part_id, result.good_delta + result.scrap_delta)

        state = resolve_state(mode, result.phase, result.stopping_code != 0)
        values = {
            "mode": mode,
            "state": state,
            "temperature": result.temperature,
            "speed": result.speed,
            "error": result.stopping_code != 0,
            "warning": result.stopping_code == 0 and result.warning_code != 0,
            "good_parts": good_parts,
            "scrap_parts": scrap_parts,
            "fault_code": result.fault_code,
            "part_id": part_id,
        }
        values.update(result.values)
        encode(buf, values, util)
        ctypes.memmove(db, bytes(buf), DB_SIZE)

        alarm = ""
        if result.fault_code:
            name = PROFILE_CLASS.ALARMS[result.fault_code][0]
            alarm = "  %s %d %s" % (
                "WARN" if values["warning"] else "FAULT",
                result.fault_code,
                name,
            )
        log(
            "mode=%-6s state=%-11s temp=%7.1f speed=%-5d good=%-8d scrap=%-6d part=%-8d%s"
            % (
                MODE_NAMES[mode],
                STATE_NAMES.get(state, "?"),
                result.temperature,
                result.speed,
                good_parts,
                scrap_parts,
                part_id,
                alarm,
            )
        )

        time.sleep(UPDATE_INTERVAL)

    server.stop()
    server.destroy()
    log("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
