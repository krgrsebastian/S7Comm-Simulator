"""Machine profiles. Pick one with S7_MACHINE_TYPE."""

from .base import Profile, StepResult
from .cnc import Cnc
from .generic import Generic
from .oven import Oven
from .washing import Washing

PROFILES = {
    Generic.NAME: Generic,
    Cnc.NAME: Cnc,
    Oven.NAME: Oven,
    Washing.NAME: Washing,
}

__all__ = ["PROFILES", "Profile", "StepResult", "Generic", "Cnc", "Oven", "Washing"]
