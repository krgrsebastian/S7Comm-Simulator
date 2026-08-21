"""Basket washer: a small batch process.

Baskets of parts run through fill / wash / rinse / dry / drain, so this books
about thirty parts at a time — a third counter magnitude between the CNC's one
and the furnace's two hundred.

Its alarms come from the bath itself. Detergent depletes basket by basket until
it warns; contamination builds until the filter clogs and stops the machine,
which forces a bath change that resets both. Every alarm here has a number you
can watch climbing towards it.
"""

from .base import PhaseMachine, Profile, StepResult
from .core import DP, DINT, INT, REAL, State

# --- baked process constants -------------------------------------------------
BASKET_SIZE = 30
BATH_SETPOINT = 60.0        # deg C
PUMP_RPM = 2900
PRESSURE_NOMINAL = 3.2      # bar
PRESSURE_SAG = 1.6          # bar, when the pump is struggling
PRESSURE_LIMIT = 2.0        # bar, below this the pump alarm trips
FLOW_NOMINAL = 45.0         # l/min
CONDUCTIVITY_FRESH = 200.0  # uS/cm after a bath change
CONDUCTIVITY_PER_BASKET = 55.0
CONDUCTIVITY_LIMIT = 1200.0  # uS/cm, filter clogs above this
DETERGENT_FULL = 100.0      # %
DETERGENT_PER_BASKET = 5.5
DETERGENT_WARN = 15.0       # %
# Shares of the cycle; see the note in oven.py on why nothing goes below 0.10.
PHASES = (
    (State.FILLING, 0.10),
    (State.WASHING, 0.35),
    (State.RINSING, 0.25),
    (State.DRYING, 0.20),
    (State.DRAINING, 0.10),
)

DETERGENT_LOW = 301
FILTER_CLOGGED = 302
PUMP_PRESSURE_LOW = 303


class Washing(Profile):
    NAME = "washing"
    DESCRIPTION = "basket washer, one basket of parts per cycle"
    DEFAULT_CYCLE_TIME = 120.0
    PARTS_PER_CYCLE = BASKET_SIZE
    DATAPOINTS = (
        DP(32, REAL, "pump_pressure"),
        DP(36, REAL, "conductivity"),
        DP(40, REAL, "detergent_level"),
        DP(44, REAL, "flow_rate"),
        DP(48, DINT, "basket_number"),
        DP(52, INT, "phase_remaining_s"),
    )
    ALARMS = {
        DETERGENT_LOW: ("detergent low", False),
        FILTER_CLOGGED: ("filter clogged", True),
        PUMP_PRESSURE_LOW: ("pump pressure low", True),
    }

    def __init__(self, cycle_time, scrap_rate, rng):
        super().__init__(cycle_time, scrap_rate, rng)
        self.phases = PhaseMachine(PHASES, cycle_time)
        self.basket_number = 1
        self.baskets_since_bath_change = 0
        self.temperature = BATH_SETPOINT
        self.pump_pressure = 0.0
        self.flow_rate = 0.0
        self._sagged_basket = None
        self._booked_number = None

    # -- derived bath condition -------------------------------------------
    @property
    def conductivity(self) -> float:
        return CONDUCTIVITY_FRESH + CONDUCTIVITY_PER_BASKET * self.baskets_since_bath_change

    @property
    def detergent_level(self) -> float:
        return max(
            0.0, DETERGENT_FULL - DETERGENT_PER_BASKET * self.baskets_since_bath_change
        )

    def _change_bath(self) -> None:
        self.baskets_since_bath_change = 0

    def on_fault_cleared(self) -> None:
        # A clogged filter is cleared by changing the bath, which also tops up
        # the detergent.
        if self.conductivity > CONDUCTIVITY_LIMIT:
            self._change_bath()

    def step(self, dt, dt_run):
        self._tick_fault(dt)
        phase = self.phases.state
        # Pumping needs the machine to be running, not merely to be in a wet
        # phase: in manual or setup the pump is off, so it can neither push
        # pressure nor trip a pressure alarm.
        pumping = (
            phase in (State.WASHING, State.RINSING)
            and not self.faulted
            and dt_run > 0.0
        )

        # Every 6th basket the pump sags below its limit and trips. Once per
        # basket only: without the latch it would re-trip the instant its own
        # fault cleared and the basket would never finish.
        sagging = (
            pumping
            and self.basket_number % 6 == 0
            and self._sagged_basket != self.basket_number
        )
        if pumping:
            self.pump_pressure = (PRESSURE_SAG if sagging else PRESSURE_NOMINAL) + self.rng.uniform(-0.08, 0.08)
            self.flow_rate = FLOW_NOMINAL + self.rng.uniform(-1.5, 1.5)
        else:
            self.pump_pressure = 0.0
            self.flow_rate = 0.0

        if pumping and self.pump_pressure < PRESSURE_LIMIT:
            self._sagged_basket = self.basket_number
            self.raise_fault(PUMP_PRESSURE_LOW, 0.3 * self.cycle_time)
        elif self.conductivity > CONDUCTIVITY_LIMIT:
            self.raise_fault(FILTER_CLOGGED, 0.4 * self.cycle_time)

        # Freeze after the alarms, so an alarm raised on this tick also stops
        # the process on this tick.
        if self.faulted:
            dt_run = 0.0

        left = self.phases.advance(dt_run)
        phase = self.phases.state

        # Fresh water cools the bath, the heater brings it back.
        tau = max(1.0, 0.25 * self.phases.duration)
        target = BATH_SETPOINT - 12.0 if phase == State.FILLING else BATH_SETPOINT
        self.temperature += (target - self.temperature) * min(1.0, dt / tau)
        self.temperature += self.rng.uniform(-0.2, 0.2)

        good = scrap = 0
        # Book as the basket comes out, publishing the number of the basket that
        # produced it. See the note in oven.py.
        self._booked_number = None
        if State.DRYING in left:
            scrap = self.scrap_of(BASKET_SIZE)
            good = BASKET_SIZE - scrap
            self._booked_number = self.basket_number
            self.basket_number += 1
            self.baskets_since_bath_change += 1

        warning = DETERGENT_LOW if self.detergent_level < DETERGENT_WARN else 0

        return StepResult(
            phase=phase,
            temperature=self.temperature,
            speed=int(PUMP_RPM + self.rng.uniform(-25, 25)) if pumping else 0,
            stopping_code=self.stopping_code,
            warning_code=warning,
            good_delta=good,
            scrap_delta=scrap,
            values={
                "pump_pressure": self.pump_pressure,
                "conductivity": self.conductivity,
                "detergent_level": self.detergent_level,
                "flow_rate": self.flow_rate,
                "basket_number": (
                    self.basket_number
                    if self._booked_number is None
                    else self._booked_number
                ),
                "phase_remaining_s": self.phases.remaining,
            },
        )
