"""Hardening furnace: a batch process.

Unlike the CNC, nothing is booked while the furnace works. A charge is loaded,
heated, soaked, quenched and only counted on discharge — so the counters sit
flat for a whole cycle and then jump by the charge. That is the signal shape
that breaks naive rate maths, and the reason this profile exists.

It also has the failure mode a furnace really has: if the charge does not hold
temperature for its soak, the metallurgy is wrong and the whole charge is scrap,
not one part of it.
"""

from .base import PhaseMachine, Profile, StepResult
from .core import DP, DINT, INT, REAL, State

# --- baked process constants -------------------------------------------------
SETPOINT = 860.0        # deg C
AMBIENT = 40.0          # deg C, chamber at rest
BATCH_SIZE = 200        # parts per charge
FAN_RPM = 1450
CARBON_NOMINAL = 0.85   # %C
CARBON_BAND = 0.10      # %C, +/- before the atmosphere alarm
# Share of the cycle each phase takes.
# Shares of the cycle. Nothing below 0.10: a phase shorter than the update
# interval is skipped between polls, so it becomes unobservable as soon as
# someone turns S7_CYCLE_TIME down to fast-forward.
PHASES = (
    (State.CHARGING, 0.10),
    (State.HEATING, 0.25),
    (State.SOAKING, 0.35),
    (State.QUENCHING, 0.20),
    (State.DISCHARGING, 0.10),
)
# Interruption during heating/soaking beyond this share of the cycle ruins it.
RUIN_SHARE = 0.15

OVER_TEMPERATURE = 201
DOOR_INTERLOCK = 202
ATMOSPHERE = 203


class Oven(Profile):
    NAME = "oven"
    DESCRIPTION = "hardening furnace, whole charge booked on discharge"
    DEFAULT_CYCLE_TIME = 300.0
    PARTS_PER_CYCLE = BATCH_SIZE
    DATAPOINTS = (
        DP(32, REAL, "setpoint_temperature"),
        DP(36, REAL, "heater_power"),
        DP(40, REAL, "carbon_potential"),
        DP(44, DINT, "batch_number"),
        DP(48, INT, "batch_size"),
        DP(50, INT, "phase_remaining_s"),
    )
    ALARMS = {
        OVER_TEMPERATURE: ("over-temperature", True),
        DOOR_INTERLOCK: ("door interlock", True),
        ATMOSPHERE: ("atmosphere out of band", False),
    }

    def __init__(self, cycle_time, scrap_rate, rng):
        super().__init__(cycle_time, scrap_rate, rng)
        self.phases = PhaseMachine(PHASES, cycle_time)
        self.batch_number = 1
        self.temperature = AMBIENT
        self.heater_power = 0.0
        self.carbon_potential = CARBON_NOMINAL
        self._booked_number = None  # names the charge booked on this tick
        self._stalled = 0.0        # interruption accumulated in heating/soaking
        self._alarmed_phase = None  # so each scripted alarm fires once per charge

    def step(self, dt, dt_run):
        self._tick_fault(dt)

        phase = self.phases.state

        # --- scripted, cause-visible alarms ------------------------------
        # Every 5th charge overshoots at the top of the ramp, and stalls long
        # enough to ruin itself.
        if (
            phase == State.HEATING
            and self.batch_number % 5 == 0
            and self._alarmed_phase != State.HEATING
            and self.temperature > SETPOINT - 15.0
        ):
            self._alarmed_phase = State.HEATING
            self.raise_fault(OVER_TEMPERATURE, (RUIN_SHARE + 0.10) * self.cycle_time)
        # Every 3rd charge sticks a door interlock while loading — annoying, but
        # the charge is cold, so nothing is spoiled.
        elif (
            phase == State.CHARGING
            and self.batch_number % 3 == 0
            and self._alarmed_phase != State.CHARGING
        ):
            self._alarmed_phase = State.CHARGING
            self.raise_fault(DOOR_INTERLOCK, 0.05 * self.cycle_time)

        # A stopping fault holds the process where it is — including one raised
        # on this very tick, hence the check after the alarm block. Time stalled
        # in the hot phases is what ruins a charge.
        if self.faulted:
            if phase in (State.HEATING, State.SOAKING):
                self._stalled += dt
            dt_run = 0.0

        left = self.phases.advance(dt_run)
        phase = self.phases.state
        if left:
            self._alarmed_phase = None

        # --- process values ----------------------------------------------
        # Time constants are a share of the *current phase*, not a fixed number
        # of seconds: the ramp has to finish inside the heating phase whatever
        # S7_CYCLE_TIME is set to, otherwise the chamber never reaches setpoint
        # and the over-temperature alarm can never fire.
        tau = max(1.0, 0.22 * self.phases.duration)
        if phase == State.HEATING:
            self.temperature += (SETPOINT + 8.0 - self.temperature) * min(1.0, dt / tau)
            self.heater_power = 100.0
        elif phase == State.SOAKING:
            self.temperature += (SETPOINT - self.temperature) * min(1.0, dt / tau)
            self.heater_power = 22.0 + self.rng.uniform(-3, 3)
        elif phase == State.QUENCHING:
            self.temperature += (AMBIENT - self.temperature) * min(1.0, dt / tau)
            self.heater_power = 0.0
        else:
            self.temperature += (AMBIENT - self.temperature) * min(1.0, dt / tau)
            self.heater_power = 0.0
        self.temperature += self.rng.uniform(-1.0, 1.0)

        # Carbon potential drifts per charge; some charges wander out of band.
        drift = CARBON_BAND * 1.6 * ((self.batch_number * 37 % 11) / 10.0 - 0.5)
        self.carbon_potential = CARBON_NOMINAL + drift + self.rng.uniform(-0.005, 0.005)
        warning = 0
        if phase in (State.HEATING, State.SOAKING) and abs(
            self.carbon_potential - CARBON_NOMINAL
        ) > CARBON_BAND:
            warning = ATMOSPHERE

        # --- booking ------------------------------------------------------
        good = scrap = 0
        # Book as the charge comes out, and publish the number of the charge
        # that produced it. Deriving that from the phase would break whenever a
        # short phase is skipped between polls and both transitions land on the
        # same tick.
        self._booked_number = None
        if State.QUENCHING in left:
            ruined = self._stalled > RUIN_SHARE * self.cycle_time
            if ruined:
                scrap = BATCH_SIZE
            else:
                scrap = self.scrap_of(BATCH_SIZE)
                good = BATCH_SIZE - scrap
            self._stalled = 0.0
            self._booked_number = self.batch_number
            self.batch_number += 1

        # The fan only turns while the machine is actually running. The heater
        # is left alone on purpose: a furnace's temperature controller keeps
        # holding setpoint whatever mode the operator selected.
        heating = phase in (State.HEATING, State.SOAKING) and dt_run > 0.0
        return StepResult(
            phase=phase,
            temperature=self.temperature,
            speed=int(FAN_RPM + self.rng.uniform(-20, 20)) if heating else 0,
            stopping_code=self.stopping_code,
            warning_code=warning,
            good_delta=good,
            scrap_delta=scrap,
            values={
                "setpoint_temperature": SETPOINT,
                "heater_power": self.heater_power,
                "carbon_potential": self.carbon_potential,
                "batch_number": (
                    self.batch_number
                    if self._booked_number is None
                    else self._booked_number
                ),
                "batch_size": BATCH_SIZE,
                "phase_remaining_s": self.phases.remaining,
            },
        )
