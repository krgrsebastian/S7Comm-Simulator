"""3-axis milling centre: one part per cycle, measured.

Every part gets a diameter and a coolant pressure, resampled once per part and
then held — which is what a real PLC does, and why `part_id` exists for
consumers to dedup on.

The diameter is the interesting one. It creeps toward the upper tolerance as the
tool wears and steps back to nominal at the tool change, so an X-bar chart shows
a real trend rather than noise. Coolant pressure sits beside it as a deliberately
capable process, so the two have visibly different Cpk.

Scrap on this profile comes from one place only: a part measuring outside
tolerance. S7_SCRAP_RATE is ignored, so scrap_parts reconciles exactly against
the measurements.
"""

from .base import Profile, StepResult
from .core import DP, INT, REAL, State

# --- baked process constants (documented in the README, deliberately not env) --
DIA_NOMINAL = 25.0      # mm
DIA_TOL = 0.05          # mm, +/-
DIA_SIGMA = 0.008       # mm, short-term process spread
WEAR_DRIFT = 0.045      # mm the diameter walks across one full tool life
TOOL_LIFE = 40          # parts per tool
PRESSURE_NOMINAL = 4.5  # bar
PRESSURE_SIGMA = 0.05   # bar
PRESSURE_TOL = 0.3      # bar, +/- process limit. Not published as an address;
                        # it exists so coolant pressure has a Cpk to compare
                        # against the diameter's, and it is a far more capable
                        # process on purpose.
COOLANT_FULL = 100.0    # %
COOLANT_EMPTY = 5.0     # % at end of tool life
COOLANT_WARN = 20.0     # % warning threshold
LOAD_LIMIT = 95.0       # % spindle load that trips an overload

# Alarm codes
TOOL_BREAK = 101
COOLANT_LOW = 102
SPINDLE_OVERLOAD = 103


class Cnc(Profile):
    NAME = "cnc"
    DESCRIPTION = "milling centre with per-part SPC measurements"
    DEFAULT_CYCLE_TIME = 45.0
    DATAPOINTS = (
        DP(32, REAL, "measured_diameter"),
        DP(36, REAL, "coolant_pressure"),
        DP(40, REAL, "coolant_level"),
        DP(44, INT, "tool_number"),
        DP(46, INT, "tool_life_pct"),
        DP(48, REAL, "spindle_load"),
        DP(52, REAL, "feed_rate"),
    )
    ALARMS = {
        TOOL_BREAK: ("tool break", True),
        COOLANT_LOW: ("coolant low", False),
        SPINDLE_OVERLOAD: ("spindle overload", True),
    }

    def __init__(self, cycle_time, scrap_rate, rng):
        super().__init__(cycle_time, scrap_rate, rng)
        self.run_seconds = 0.0
        self.tool_number = 1
        self.parts_in_tool = 0
        # Measurements are held between parts; seed them with a first part so a
        # reader never sees 0.0.
        self.measured_diameter = DIA_NOMINAL
        self.coolant_pressure = PRESSURE_NOMINAL
        self.spindle_load = 60.0
        self._changing_for = 0.0
        self._change_pending = False

    # -- helpers ----------------------------------------------------------
    @property
    def wear_fraction(self) -> float:
        return min(1.0, self.parts_in_tool / float(TOOL_LIFE))

    @property
    def tool_life_pct(self) -> int:
        return int(round(100.0 * (1.0 - self.wear_fraction)))

    @property
    def coolant_level(self) -> float:
        return COOLANT_FULL - (COOLANT_FULL - COOLANT_EMPTY) * self.wear_fraction

    @property
    def _breaks(self) -> bool:
        """Every 4th tool breaks instead of finishing its life."""
        return self.tool_number % 4 == 0

    @property
    def _overloads(self) -> bool:
        """Every 4th tool, offset from the breaking one, trips an overload."""
        return self.tool_number % 4 == 2

    def _load(self) -> float:
        """Spindle load rises with wear, and further on the tool due to trip."""
        load = 60.0 + 30.0 * self.wear_fraction
        if self._overloads and self.wear_fraction >= 0.95:
            load += 8.0
        return load + self.rng.uniform(-1.5, 1.5)

    def _begin_tool_change(self) -> None:
        """Start the change; the new tool is only in the spindle once it ends.

        Completing it on the trailing edge keeps tool_number, tool_life_pct and
        measured_diameter describing the *same* tool within one snapshot — a
        consumer grouping measurements by tool would otherwise file the last
        part of each tool under the next one.
        """
        if self._change_pending:
            return
        # At least two seconds so a one-second poller always sees the state,
        # even when S7_CYCLE_TIME is turned right down for a fast-forward.
        self._changing_for = max(0.1 * self.cycle_time, 2.0)
        self._change_pending = True

    def on_fault_cleared(self) -> None:
        # A broken tool or a tripped spindle is followed by a tool change.
        self._begin_tool_change()

    # -- the tick ---------------------------------------------------------
    def step(self, dt, dt_run):
        self._tick_fault(dt)

        # A stopping fault freezes the process; so does a tool change, which is
        # real downtime and should show up as such.
        if self.faulted:
            dt_run = 0.0

        phase = State.RUNNING
        if self._change_pending:
            self._changing_for -= dt
            if self._changing_for <= 0.0:
                self.tool_number += 1
                self.parts_in_tool = 0
                self._change_pending = False
            else:
                phase = State.TOOL_CHANGE
                dt_run = 0.0

        good = scrap = 0
        self.run_seconds += dt_run
        while self.run_seconds >= self.cycle_time:
            self.run_seconds -= self.cycle_time
            self.parts_in_tool += 1

            wear = WEAR_DRIFT * self.wear_fraction
            self.measured_diameter = DIA_NOMINAL + wear + self.rng.gauss(0.0, DIA_SIGMA)
            self.coolant_pressure = PRESSURE_NOMINAL + self.rng.gauss(0.0, PRESSURE_SIGMA)

            # The only scrap mechanism on this profile.
            if abs(self.measured_diameter - DIA_NOMINAL) > DIA_TOL:
                scrap += 1
            else:
                good += 1

            self.spindle_load = self._load()
            if self._breaks and self.wear_fraction >= 0.9:
                self.raise_fault(TOOL_BREAK, 2.0 * self.cycle_time)
                break
            if self._overloads and self.spindle_load > LOAD_LIMIT:
                self.raise_fault(SPINDLE_OVERLOAD, 1.5 * self.cycle_time)
                break
            if self.parts_in_tool >= TOOL_LIFE:
                self._begin_tool_change()
                break

        if not good and not scrap:
            # No part completed this tick: refresh the load reading only.
            self.spindle_load = self._load()

        warning = COOLANT_LOW if self.coolant_level < COOLANT_WARN else 0
        running = phase == State.RUNNING and dt_run > 0.0

        return StepResult(
            phase=phase,
            temperature=35.0 + 15.0 * self.wear_fraction + self.rng.uniform(-0.4, 0.4),
            speed=int(8000 + self.rng.uniform(-40, 40)) if running else 0,
            stopping_code=self.stopping_code,
            warning_code=warning,
            good_delta=good,
            scrap_delta=scrap,
            values={
                "measured_diameter": self.measured_diameter,
                "coolant_pressure": self.coolant_pressure,
                "coolant_level": self.coolant_level,
                "tool_number": self.tool_number,
                "tool_life_pct": self.tool_life_pct,
                "spindle_load": self.spindle_load,
                "feed_rate": 850.0 + self.rng.uniform(-8, 8) if running else 0.0,
            },
        )
