"""The plain machine: v0.2's behaviour, unchanged.

Kept as the default so anything already pointing at this image sees exactly
what it saw before. It has no process phases and no alarms — the three real
machine profiles are where the interesting signals live.
"""

import math
import random

from .base import Profile, StepResult
from .core import State


class Generic(Profile):
    NAME = "generic"
    DESCRIPTION = "anonymous machine, one part per cycle (v0.2 behaviour)"
    DEFAULT_CYCLE_TIME = 60.0
    DATAPOINTS = ()
    ALARMS = {}

    def __init__(self, cycle_time, scrap_rate, rng):
        super().__init__(cycle_time, scrap_rate, rng)
        self.elapsed = 0.0
        self.run_seconds = 0.0

    def step(self, dt, dt_run):
        # The sines run on wall time, as in v0.2, so the curves keep moving
        # while the machine is stopped.
        self.elapsed += dt

        temperature = 50.0 + 30.0 * math.sin(self.elapsed / 20.0) + self.rng.uniform(-1.5, 1.5)
        running = dt_run > 0.0
        speed = (
            int(1500 + 200 * math.sin(self.elapsed / 7.0) + self.rng.uniform(-50, 50))
            if running
            else 0
        )

        good = scrap = 0
        self.run_seconds += dt_run
        while self.run_seconds >= self.cycle_time:
            self.run_seconds -= self.cycle_time
            if self.rng.random() < self.scrap_rate:
                scrap += 1
            else:
                good += 1

        return StepResult(
            phase=State.RUNNING,
            temperature=temperature,
            speed=speed,
            good_delta=good,
            scrap_delta=scrap,
        )
