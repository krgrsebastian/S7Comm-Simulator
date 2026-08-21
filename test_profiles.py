#!/usr/bin/env python3
"""Invariants every machine profile has to hold.

Runs in-process against the profile classes — no container, no PLC, no snap7 —
which is why `profiles/` is kept free of snap7 imports. Counter handling is
imported from `simulator` rather than reimplemented, so the tests exercise the
same wrap logic that runs in production.

    python -m unittest -v
"""

import random
import statistics
import unittest

import simulator
from profiles import PROFILES, Cnc, Generic, Oven, Washing
from profiles.cnc import (
    DIA_NOMINAL,
    DIA_TOL,
    PRESSURE_NOMINAL,
    PRESSURE_TOL,
    TOOL_LIFE,
    WEAR_DRIFT,
)
from profiles.core import (
    BOOL,
    CORE_DATAPOINTS,
    MODE_AUTO,
    MODE_MANUAL,
    MODE_SETUP,
    State,
    resolve_state,
)
from profiles.oven import BATCH_SIZE
from profiles.washing import BASKET_SIZE, CONDUCTIVITY_FRESH


class Tick(dict):
    """One published snapshot, addressable as attributes for readability."""

    __getattr__ = dict.__getitem__


def drive(profile_cls, ticks, dt=1.0, cycle_time=1.0, scrap_rate=0.05, seed=1, mode=MODE_AUTO):
    """Drive a profile the way simulator.py's main loop does.

    `mode` may be a constant or a callable taking the tick index, so an operator
    changing mode part-way through a cycle can be simulated.
    """
    rng = random.Random(seed)
    profile = profile_cls(cycle_time=cycle_time, scrap_rate=scrap_rate, rng=rng)
    modes = mode if callable(mode) else (lambda _i: mode)
    good = scrap = part_id = 0
    out = []
    for _i in range(ticks):
        mode = modes(_i)
        dt_run = dt if mode == MODE_AUTO else 0.0
        r = profile.step(dt, dt_run)
        good = simulator.bump(good, r.good_delta)
        scrap = simulator.bump(scrap, r.scrap_delta)
        part_id = simulator.bump(part_id, r.good_delta + r.scrap_delta)
        out.append(
            Tick(
                state=resolve_state(mode, r.phase, r.stopping_code != 0),
                phase=r.phase,
                fault_code=r.fault_code,
                error=r.stopping_code != 0,
                warning=r.stopping_code == 0 and r.warning_code != 0,
                good=good,
                scrap=scrap,
                part_id=part_id,
                booked=r.good_delta + r.scrap_delta,
                good_delta=r.good_delta,
                scrap_delta=r.scrap_delta,
                temperature=r.temperature,
                speed=r.speed,
                params=dict(r.values),
            )
        )
    return out


ALL = [Generic, Cnc, Oven, Washing]


class TestLayout(unittest.TestCase):
    def test_fits_one_benthos_read(self):
        """More than 20 addresses means benthos reads a profile in two
        round-trips that look like one atomic snapshot."""
        for cls in ALL:
            with self.subTest(cls.NAME):
                total = len(CORE_DATAPOINTS) + len(cls.DATAPOINTS)
                self.assertLessEqual(total, simulator.BENTHOS_MAX_ITEMS_PER_BATCH)

    def test_no_overlapping_or_duplicate_addresses(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                dps = CORE_DATAPOINTS + cls.DATAPOINTS
                addrs = [d.address() for d in dps]
                self.assertEqual(len(addrs), len(set(addrs)))
                # Whole-byte datapoints must not overlap each other; bools may
                # share a byte as long as they are on different bits.
                byte_owner = {}
                bit_owner = {}
                for d in dps:
                    if d.kind == BOOL:
                        key = (d.offset, d.bit)
                        self.assertNotIn(
                            key, bit_owner, "%s overlaps %s" % (d.name, bit_owner.get(key))
                        )
                        bit_owner[key] = d.name
                    else:
                        for off in range(d.offset, d.offset + _width(d)):
                            self.assertNotIn(
                                off,
                                byte_owner,
                                "%s overlaps %s at byte %d"
                                % (d.name, byte_owner.get(off), off),
                            )
                            byte_owner[off] = d.name
                # a bool must not sit inside a byte owned by a wider type
                for (off, _bit), name in bit_owner.items():
                    self.assertNotIn(
                        off, byte_owner, "%s overlaps %s" % (name, byte_owner.get(off))
                    )

    def test_block_fits_db(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                self.assertLessEqual(cls.block_end(), simulator.DB_SIZE)

    def test_every_profile_registered(self):
        self.assertEqual(set(PROFILES), {c.NAME for c in ALL})


def _width(dp):
    from profiles.core import WIDTH

    return WIDTH[dp.kind]


class TestCoreInvariants(unittest.TestCase):
    def test_counters_never_decrease(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                rows = drive(cls, 1200, cycle_time=1.0)
                for a, b in zip(rows, rows[1:]):
                    self.assertGreaterEqual(b.good, a.good)
                    self.assertGreaterEqual(b.scrap, a.scrap)
                    self.assertGreaterEqual(b.part_id, a.part_id)

    def test_counters_never_negative(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                for row in drive(cls, 600, cycle_time=1.0):
                    self.assertGreaterEqual(row.good, 0)
                    self.assertGreaterEqual(row.scrap, 0)

    def test_part_id_equals_total_booked(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                rows = drive(cls, 600, cycle_time=1.0)
                self.assertEqual(rows[-1].part_id, rows[-1].good + rows[-1].scrap)

    def test_nothing_produced_outside_auto(self):
        for cls in ALL:
            for mode in (MODE_MANUAL, MODE_SETUP):
                with self.subTest(cls.NAME, mode=mode):
                    rows = drive(cls, 900, cycle_time=1.0, mode=mode)
                    self.assertEqual(rows[-1].good + rows[-1].scrap, 0)
                    expected = State.STOPPED if mode == MODE_MANUAL else State.SETUP
                    self.assertTrue(all(r.state == expected for r in rows))

    def test_nothing_produced_while_already_faulted(self):
        """A part may be booked on the tick a fault is raised — it was finished
        before the tool broke. Nothing may be booked once the fault is held."""
        for cls in ALL:
            with self.subTest(cls.NAME):
                rows = drive(cls, 2000, cycle_time=1.0)
                for a, b in zip(rows, rows[1:]):
                    if a.error and b.error:
                        self.assertEqual(b.booked, 0)

    def test_moving_parts_stop_when_not_running(self):
        """speed is the core word for motion, so it must read 0 whenever the
        machine is not producing — in every profile, not just the ones that
        happened to get it right."""
        for cls in ALL:
            for mode in (MODE_MANUAL, MODE_SETUP):
                with self.subTest(cls.NAME, mode=mode):
                    # Run in auto long enough to get well into the process,
                    # then have the operator switch. A constant non-auto mode
                    # would leave the phase machine parked on its first phase
                    # and never exercise the moving parts at all.
                    switch = 900
                    rows = drive(
                        cls,
                        switch + 300,
                        cycle_time=20.0,
                        mode=lambda i, m=mode: MODE_AUTO if i < switch else m,
                    )
                    before = rows[:switch]
                    after = rows[switch + 1 :]
                    self.assertTrue(
                        any(r.speed > 0 for r in before),
                        "%s never moved while in auto" % cls.NAME,
                    )
                    for r in after:
                        self.assertEqual(r.speed, 0, "%s still moving" % cls.NAME)
                        for name in ("pump_pressure", "flow_rate", "feed_rate"):
                            if name in r.params:
                                self.assertEqual(
                                    r.params[name], 0.0, "%s: %s" % (cls.NAME, name)
                                )

    def test_process_is_frozen_while_faulted(self):
        """A stopping fault must hold the process where it is, not just stop the
        counters. Asserted directly: checking only that nothing is booked misses
        it whenever an alarm never coincides with a booking boundary."""
        for cls in ALL:
            with self.subTest(cls.NAME):
                rows = drive(cls, 4000, cycle_time=20.0)
                pairs = [(a, b) for a, b in zip(rows, rows[1:]) if a.error and b.error]
                if not any(stopping for _n, stopping in cls.ALARMS.values()):
                    self.assertFalse(pairs, "%s has no stopping alarms" % cls.NAME)
                    continue
                self.assertTrue(pairs, "%s never held a stopping fault" % cls.NAME)
                for a, b in pairs:
                    self.assertEqual(a.phase, b.phase, "%s advanced while faulted" % cls.NAME)
                    if "phase_remaining_s" in a.params:
                        self.assertEqual(
                            a.params["phase_remaining_s"],
                            b.params["phase_remaining_s"],
                            "%s phase clock ran while faulted" % cls.NAME,
                        )

    def test_state_is_fault_whenever_error(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                for row in drive(cls, 2000, cycle_time=1.0):
                    self.assertEqual(row.error, row.state == State.FAULT)

    def test_error_and_warning_are_mutually_exclusive(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                for row in drive(cls, 2000, cycle_time=1.0):
                    self.assertFalse(row.error and row.warning)
                    if row.error or row.warning:
                        self.assertIn(row.fault_code, cls.ALARMS)
                        stopping = cls.ALARMS[row.fault_code][1]
                        self.assertEqual(row.error, stopping)

    def test_fault_code_zero_means_no_alarm(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                for row in drive(cls, 1200, cycle_time=1.0):
                    if not row.fault_code:
                        self.assertFalse(row.error or row.warning)

    def test_counter_wraps_to_zero_and_never_goes_negative(self):
        original = simulator.COUNTER_MAX
        try:
            simulator.COUNTER_MAX = 5
            seen = [simulator.bump(c, 1) for c in range(7)]
            self.assertEqual(seen, [1, 2, 3, 4, 5, 0, 0])
        finally:
            simulator.COUNTER_MAX = original


class TestPhases(unittest.TestCase):
    EXPECTED = {
        "generic": {State.RUNNING},
        "cnc": {State.RUNNING, State.TOOL_CHANGE},
        "oven": {
            State.CHARGING,
            State.HEATING,
            State.SOAKING,
            State.QUENCHING,
            State.DISCHARGING,
        },
        "washing": {
            State.FILLING,
            State.WASHING,
            State.RINSING,
            State.DRYING,
            State.DRAINING,
        },
    }

    def test_every_phase_is_visited(self):
        for cls in ALL:
            with self.subTest(cls.NAME):
                rows = drive(cls, 3000, cycle_time=20.0)
                seen = {r.phase for r in rows}
                self.assertTrue(
                    self.EXPECTED[cls.NAME].issubset(seen),
                    "%s never reached %s" % (cls.NAME, self.EXPECTED[cls.NAME] - seen),
                )


class TestFastForward(unittest.TestCase):
    """S7_CYCLE_TIME is the one knob users turn, so the profiles have to stay
    correct and observable when it is turned right down."""

    NUMBER_KEY = {"oven": "batch_number", "washing": "basket_number"}

    def test_every_phase_survives_a_fast_forward(self):
        """A phase shorter than the poll interval is skipped between reads, so
        it becomes invisible. Caught over the wire, not here, the first time:
        washing's DRAINING was 5% of the cycle and vanished at CYCLE_TIME=15."""
        for cls in ALL:
            with self.subTest(cls.NAME):
                rows = drive(cls, 2000, dt=1.0, cycle_time=15.0)
                seen = {r.phase for r in rows}
                missing = TestPhases.EXPECTED[cls.NAME] - seen
                self.assertFalse(
                    missing, "%s loses phases %s at cycle_time=15" % (cls.NAME, missing)
                )

    def test_batch_numbering_holds_when_phases_collapse(self):
        """At an extreme fast-forward several transitions land on one tick.
        Attribution must not depend on them being separate."""
        for cls in (Oven, Washing):
            with self.subTest(cls.NAME):
                key = self.NUMBER_KEY[cls.NAME]
                rows = drive(cls, 1500, dt=1.0, cycle_time=4.0)
                numbers = [r.params[key] for r in rows if r.booked]
                self.assertGreater(len(numbers), 10)
                self.assertEqual(numbers, list(range(1, len(numbers) + 1)))


class TestCncSpc(unittest.TestCase):
    def setUp(self):
        self.rows = drive(Cnc, 3000, cycle_time=1.0, seed=7)
        self.parts = [r for r in self.rows if r.booked]

    def test_enough_parts_for_a_chart(self):
        self.assertGreater(len(self.parts), 300)

    def test_measurement_is_held_between_parts(self):
        """One measurement per part_id, however often it is polled."""
        by_part = {}
        for r in self.rows:
            by_part.setdefault(r.part_id, set()).add(
                round(r.params["measured_diameter"], 9)
            )
        for part_id, values in by_part.items():
            self.assertEqual(len(values), 1, "part %s changed mid-part" % part_id)

    def test_scrap_is_exactly_the_out_of_tolerance_parts(self):
        """S7_SCRAP_RATE is ignored here, so this reconciles exactly."""
        for r in self.parts:
            out = abs(r.params["measured_diameter"] - DIA_NOMINAL) > DIA_TOL
            self.assertEqual(
                bool(r.scrap_delta), out, "dia=%r" % r.params["measured_diameter"]
            )

    def test_scrap_rate_is_ignored(self):
        """Same seed, wildly different rate, identical scrap count."""
        a = drive(Cnc, 2000, cycle_time=1.0, scrap_rate=0.0, seed=3)
        b = drive(Cnc, 2000, cycle_time=1.0, scrap_rate=0.9, seed=3)
        self.assertEqual(a[-1].scrap, b[-1].scrap)
        self.assertGreater(a[-1].scrap, 0)

    def test_diameter_drifts_up_then_resets_at_tool_change(self):
        by_tool = {}
        for r in self.parts:
            by_tool.setdefault(r.params["tool_number"], []).append(
                r.params["measured_diameter"]
            )
        full = [t for t, v in by_tool.items() if len(v) >= TOOL_LIFE - 2]
        self.assertGreaterEqual(len(full), 2, "need at least two complete tool lives")
        # Assert the size of the drift, not an absolute band: the mean of a
        # third of a tool life is a random variable, and a threshold tight
        # enough to be meaningful is also tight enough to clip a 3-sigma draw.
        for tool in full:
            v = by_tool[tool]
            first_third = statistics.mean(v[: len(v) // 3])
            last_third = statistics.mean(v[-len(v) // 3 :])
            self.assertGreater(
                last_third - first_third,
                WEAR_DRIFT * 0.2,
                "tool %s barely drifted (%.5f -> %.5f)" % (tool, first_third, last_third),
            )

    def test_first_part_of_a_tool_is_not_already_worn(self):
        """Guards the attribution of the change: if tool_number advanced when the
        change *started* rather than when it finished, the last (worn) part of
        each tool would be filed under the next tool."""
        for r in self.parts:
            # A tool that has just produced a part cannot still be untouched.
            # In the buggy version the last, worn part of each tool was
            # published alongside the next tool at 100%.
            self.assertLess(
                r.params["tool_life_pct"],
                100,
                "a part was booked against a brand-new tool (dia=%.4f)"
                % r.params["measured_diameter"],
            )

    def test_tool_life_counts_down_and_resets(self):
        lives = [r.params["tool_life_pct"] for r in self.parts]
        self.assertEqual(min(lives), 0)
        self.assertGreaterEqual(max(lives), 95)
        # a reset means the value jumps back up at least once
        self.assertTrue(any(b > a for a, b in zip(lives, lives[1:])))

    def test_coolant_refills_at_tool_change(self):
        levels = [r.params["coolant_level"] for r in self.rows]
        self.assertLess(min(levels), 10.0)
        self.assertGreater(max(levels), 95.0)

    def test_all_three_alarms_occur(self):
        self.assertEqual(
            {r.fault_code for r in self.rows if r.fault_code}, set(Cnc.ALARMS)
        )

    def test_pressure_is_the_more_capable_process(self):
        dia = [r.params["measured_diameter"] for r in self.parts]
        press = [r.params["coolant_pressure"] for r in self.parts]
        cpk_dia = _cpk(dia, DIA_NOMINAL, DIA_TOL)
        cpk_press = _cpk(press, PRESSURE_NOMINAL, PRESSURE_TOL)
        self.assertGreater(cpk_press, cpk_dia * 1.5)
        for v in (cpk_dia, cpk_press):
            self.assertTrue(v == v and abs(v) != float("inf"))  # finite


def _cpk(values, nominal, tol):
    sigma = statistics.stdev(values)
    mean = statistics.mean(values)
    return min(nominal + tol - mean, mean - (nominal - tol)) / (3.0 * sigma)


class TestOvenBatches(unittest.TestCase):
    def setUp(self):
        self.rows = drive(Oven, 60 * 40, cycle_time=60.0, seed=3)
        self.bookings = [r for r in self.rows if r.booked]

    def test_counters_are_flat_between_discharges(self):
        self.assertGreater(len(self.bookings), 5)
        self.assertEqual(len(self.bookings), len({r.part_id for r in self.bookings}))

    def test_each_discharge_books_exactly_one_charge(self):
        for r in self.bookings:
            self.assertEqual(r.booked, BATCH_SIZE)

    def test_batch_number_names_the_charge_that_was_booked(self):
        """The jump must publish alongside the batch that produced it. If the
        number advanced in the same tick, every charge's output would be
        credited to the following one."""
        numbers = [r.params["batch_number"] for r in self.bookings]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(len(numbers), len(set(numbers)))
        # first booking is batch 1, and each is one more than the last
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_a_ruined_charge_is_entirely_scrap(self):
        ruined = [r for r in self.bookings if r.scrap_delta == BATCH_SIZE]
        self.assertTrue(ruined, "no charge was ever ruined")
        for r in ruined:
            self.assertEqual(r.good_delta, 0)

    def test_a_good_charge_is_mostly_good(self):
        normal = [r for r in self.bookings if r.scrap_delta < BATCH_SIZE]
        self.assertTrue(normal)
        for r in normal:
            self.assertEqual(r.good_delta + r.scrap_delta, BATCH_SIZE)
            self.assertGreater(r.good_delta, BATCH_SIZE * 0.7)

    def test_chamber_reaches_setpoint(self):
        from profiles.oven import SETPOINT

        soaking = [r.temperature for r in self.rows if r.phase == State.SOAKING]
        self.assertTrue(soaking)
        self.assertGreater(max(soaking), SETPOINT - 10)


class TestWashingBaskets(unittest.TestCase):
    def setUp(self):
        self.rows = drive(Washing, 30 * 40, cycle_time=30.0, seed=11)
        self.bookings = [r for r in self.rows if r.booked]

    def test_each_basket_books_its_full_load(self):
        self.assertGreater(len(self.bookings), 5)
        for r in self.bookings:
            self.assertEqual(r.booked, BASKET_SIZE)

    def test_basket_number_names_the_basket_that_was_booked(self):
        numbers = [r.params["basket_number"] for r in self.bookings]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_bath_change_resets_contamination(self):
        cond = [r.params["conductivity"] for r in self.rows]
        self.assertGreater(max(cond), 1200.0)
        drops = [b for a, b in zip(cond, cond[1:]) if b < a]
        self.assertTrue(drops, "the bath was never changed")
        self.assertAlmostEqual(min(drops), CONDUCTIVITY_FRESH, delta=60.0)

    def test_detergent_depletes_and_is_topped_up(self):
        levels = [r.params["detergent_level"] for r in self.rows]
        self.assertLess(min(levels), 15.0)
        self.assertGreater(max(levels), 90.0)

    def test_all_three_alarms_occur(self):
        self.assertEqual(
            {r.fault_code for r in self.rows if r.fault_code}, set(Washing.ALARMS)
        )


class TestGenericIsUnchanged(unittest.TestCase):
    def test_one_part_per_cycle_and_no_alarms(self):
        rows = drive(Generic, 500, cycle_time=1.0, seed=5)
        self.assertEqual(rows[-1].good + rows[-1].scrap, 500)
        self.assertEqual({r.fault_code for r in rows}, {0})
        self.assertTrue(all(r.phase == State.RUNNING for r in rows))

    def test_scrap_rate_is_respected(self):
        rows = drive(Generic, 4000, cycle_time=1.0, scrap_rate=0.25, seed=5)
        share = rows[-1].scrap / float(rows[-1].good + rows[-1].scrap)
        self.assertAlmostEqual(share, 0.25, delta=0.03)


if __name__ == "__main__":
    unittest.main()
