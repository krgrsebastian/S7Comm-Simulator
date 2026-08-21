"""Datapoint layout, state codes and the state-precedence rule.

Pure Python on purpose: nothing in `profiles/` imports snap7, so the process
models can be driven by the test suite without a PLC. `simulator.py` is the only
module that touches the wire.
"""

from dataclasses import dataclass

# S7 type tags. The value doubles as the benthos address letter.
INT = "I"
DINT = "DI"
REAL = "R"
BOOL = "X"

# Bytes each type occupies, used to check that a profile's block fits DB_SIZE.
WIDTH = {INT: 2, DINT: 4, REAL: 4, BOOL: 1}


@dataclass(frozen=True)
class DP:
    """One datapoint: where it lives, how it is typed, what it is called."""

    offset: int
    kind: str
    name: str
    bit: int = 0

    def address(self, db: int = 1) -> str:
        """The benthos-umh `s7comm` address for this datapoint.

        Matches the plugin's parser (`s7comm_plugin/s7comm.go`): I/DI/R take a
        byte offset, X additionally requires a bit 0..7.
        """
        if self.kind == BOOL:
            return "DB%d.X%d.%d" % (db, self.offset, self.bit)
        return "DB%d.%s%d" % (db, self.kind, self.offset)


class State:
    """Machine state codes published at DB1.I2.

    0-40 are shared by every profile; the higher blocks are process phases and
    are documented in each profile's README section.
    """

    IDLE = 0
    RUNNING = 10
    SETUP = 20
    FAULT = 30
    STOPPED = 40
    # oven
    CHARGING = 50
    HEATING = 51
    SOAKING = 52
    QUENCHING = 53
    DISCHARGING = 54
    # washing
    FILLING = 60
    WASHING = 61
    RINSING = 62
    DRYING = 63
    DRAINING = 64
    # cnc
    TOOL_CHANGE = 70


STATE_NAMES = {
    State.IDLE: "idle",
    State.RUNNING: "running",
    State.SETUP: "setup",
    State.FAULT: "fault",
    State.STOPPED: "stopped",
    State.CHARGING: "charging",
    State.HEATING: "heating",
    State.SOAKING: "soaking",
    State.QUENCHING: "quenching",
    State.DISCHARGING: "discharging",
    State.FILLING: "filling",
    State.WASHING: "washing",
    State.RINSING: "rinsing",
    State.DRYING: "drying",
    State.DRAINING: "draining",
    State.TOOL_CHANGE: "tool_change",
}

MODE_AUTO = 0
MODE_MANUAL = 1
MODE_SETUP = 2
MODE_NAMES = {MODE_AUTO: "auto", MODE_MANUAL: "manual", MODE_SETUP: "setup"}

# The core block: ten words that mean the same thing on every profile. Anything
# only some profiles could fill belongs in a profile block instead.
CORE_DATAPOINTS = (
    DP(0, INT, "mode"),
    DP(2, INT, "state"),
    DP(4, REAL, "temperature"),
    DP(8, DINT, "speed"),
    DP(12, BOOL, "error", bit=0),
    DP(12, BOOL, "warning", bit=1),
    DP(16, DINT, "good_parts"),
    DP(20, DINT, "scrap_parts"),
    DP(24, INT, "fault_code"),
    DP(28, DINT, "part_id"),
)

# Profile blocks start here; the core block occupies 0..31.
PROFILE_BLOCK_START = 32


def resolve_state(mode: int, phase: int, fault_stopping: bool) -> int:
    """Collapse (fault, mode, phase) into the single published state code.

    A stopping fault wins over everything so an alarm is never hidden by the
    operator having left the machine in setup. Warnings deliberately do not
    appear here: a warning leaves the machine running.
    """
    if fault_stopping:
        return State.FAULT
    if mode == MODE_SETUP:
        return State.SETUP
    if mode == MODE_MANUAL:
        return State.STOPPED
    return phase
