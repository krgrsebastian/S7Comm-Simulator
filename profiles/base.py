"""The Profile contract.

A profile owns its process: phases, alarms, and the decision of when and how
many parts were made. It does **not** own the counters — `simulator.py` applies
the deltas, the wrap and the auto-mode gating, so that logic exists once and
keeps the behaviour verified in v0.2.
"""

from dataclasses import dataclass, field

from .core import PROFILE_BLOCK_START, WIDTH, State


@dataclass
class StepResult:
    """What a profile reports for one update tick.

    `stopping_code` and `warning_code` are kept apart so the caller can derive
    `error`, `warning` and `fault_code` without re-deciding which alarm wins.
    """

    phase: int
    temperature: float
    speed: int
    stopping_code: int = 0
    warning_code: int = 0
    good_delta: int = 0
    scrap_delta: int = 0
    values: dict = field(default_factory=dict)

    @property
    def fault_code(self) -> int:
        """The published code: a stopping fault outranks a warning."""
        return self.stopping_code or self.warning_code


class Profile:
    NAME = "?"
    DESCRIPTION = ""
    DEFAULT_CYCLE_TIME = 60.0
    # Datapoints of this profile's own block, from byte PROFILE_BLOCK_START.
    DATAPOINTS = ()
    # code -> (name, stopping). Stopping alarms freeze production; warnings
    # leave the machine running with the lamp on.
    ALARMS = {}
    # Parts produced per machine cycle: 1 for a part-at-a-time machine, the
    # batch size for a batch machine.
    PARTS_PER_CYCLE = 1

    def __init__(self, cycle_time: float, scrap_rate: float, rng):
        self.cycle_time = cycle_time
        self.scrap_rate = scrap_rate
        self.rng = rng
        self.stopping_code = 0
        self.warning_code = 0
        self._fault_remaining = 0.0

    # -- alarms -----------------------------------------------------------
    def raise_fault(self, code: str, seconds: float) -> None:
        """Latch a stopping fault for `seconds` of wall time."""
        if self.stopping_code:
            return
        self.stopping_code = code
        self._fault_remaining = seconds

    def _tick_fault(self, dt: float) -> None:
        if not self.stopping_code:
            return
        self._fault_remaining -= dt
        if self._fault_remaining <= 0.0:
            self.stopping_code = 0
            self._fault_remaining = 0.0
            self.on_fault_cleared()

    def on_fault_cleared(self) -> None:
        """Hook for whatever the machine does after being reset."""

    @property
    def faulted(self) -> bool:
        return self.stopping_code != 0

    # -- the tick ---------------------------------------------------------
    def step(self, dt: float, dt_run: float) -> StepResult:
        """Advance by `dt` seconds of wall time, `dt_run` of production time.

        `dt_run` is already 0 when the operator is not in auto. A profile must
        additionally not advance its process while `self.faulted`, so that a
        stopping alarm freezes the phase clock as well as the counters.
        """
        raise NotImplementedError

    # -- introspection ----------------------------------------------------
    @classmethod
    def block_end(cls) -> int:
        return max(
            (dp.offset + WIDTH[dp.kind] for dp in cls.DATAPOINTS),
            default=PROFILE_BLOCK_START,
        )

    def scrap_of(self, parts: int) -> int:
        """How many of `parts` are scrap at the baseline rate."""
        return sum(1 for _ in range(parts) if self.rng.random() < self.scrap_rate)


class PhaseMachine:
    """A cyclic sequence of timed phases, as used by the oven and the washer.

    Phase lengths are fractions of the machine's cycle time, so the single
    S7_CYCLE_TIME knob stretches or compresses the whole process.
    """

    def __init__(self, phases, cycle_time: float):
        # phases: sequence of (state_code, fraction_of_cycle)
        self.phases = tuple(phases)
        self.cycle_time = cycle_time
        self.index = 0
        self.elapsed = 0.0
        self.completed = 0

    @property
    def state(self) -> int:
        return self.phases[self.index][0]

    @property
    def duration(self) -> float:
        return self.phases[self.index][1] * self.cycle_time

    @property
    def remaining(self) -> int:
        return max(0, int(round(self.duration - self.elapsed)))

    def advance(self, dt_run: float):
        """Advance the clock; yields each phase code as it is *left*.

        A full pass through the sequence increments `completed`, which is the
        signal a batch machine books its parts on.
        """
        left = []
        self.elapsed += dt_run
        while self.elapsed >= self.duration:
            self.elapsed -= self.duration
            left.append(self.phases[self.index][0])
            self.index += 1
            if self.index >= len(self.phases):
                self.index = 0
                self.completed += 1
        return left
