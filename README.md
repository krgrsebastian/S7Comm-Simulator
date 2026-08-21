# S7Comm PLC Simulator

A Docker container that acts as a **real Siemens S7 PLC** over the S7comm
protocol (ISO-on-TCP / RFC1006, TCP port 102), so you can develop and test the
benthos-umh `s7comm` input without physical hardware.

Pick a machine with one environment variable:

```bash
docker run --rm -p 1103:102 -e S7_MACHINE_TYPE=cnc skumh/s7comm-simulator:0.3
```

| `S7_MACHINE_TYPE` | machine | what it gives you |
|---|---|---|
| `generic` (default) | anonymous machine | one part per cycle, no alarms — the v0.2 behaviour |
| `cnc` | milling centre | a measurement per part you can run SPC on, tool wear, tool breaks |
| `oven` | hardening furnace | a batch counter that jumps by 200, and charges that get ruined |
| `washing` | basket washer | a bath that contaminates and clogs, ~30 parts per basket |

Each profile carries its own process constants, so there is nothing else to
configure. It is built on [`python-snap7`](https://python-snap7.readthedocs.io)
**1.3**, which wraps the **native `libsnap7`** server — the de-facto reference S7
PLC simulator, and the exact dialect `gos7` (and therefore benthos-umh) is tested
against. The bundled native lib works on both `amd64` and `arm64` (Apple
Silicon).

> ⚠️ Version pin matters: `python-snap7 >= 3.0` replaced the server with a
> pure-Python reimplementation whose ISO/COTP handshake is **not** compatible
> with gos7 (benthos fails with `ISO : Invalid PDU received`). This image
> deliberately pins `1.3`.

## The core block

Ten words at the same offsets on **every** profile, so one benthos config reads
any machine and a dashboard built against one works against all of them. Layout
is big-endian (Siemens standard).

| benthos address | S7 type | name | meaning |
|-----------------|---------|------|---------|
| `DB1.I0` | INT | `mode` | 0 = auto, 1 = manual, 2 = setup |
| `DB1.I2` | INT | `state` | what the machine is doing — see below |
| `DB1.R4` | REAL | `temperature` | the machine's primary temperature, °C |
| `DB1.DI8` | DINT | `speed` | the machine's primary speed, rpm |
| `DB1.X12.0` | BOOL | `error` | a **stopping** fault is active |
| `DB1.X12.1` | BOOL | `warning` | a **non-stopping** alarm is active |
| `DB1.DI16` | DINT | `good_parts` | counts up, wraps at `S7_COUNTER_MAX` |
| `DB1.DI20` | DINT | `scrap_parts` | counts up |
| `DB1.I24` | INT | `fault_code` | 0 = none, otherwise a per-profile code |
| `DB1.DI28` | DINT | `part_id` | total parts; changes **only** when a part completes |

Each profile adds its own process parameters from byte 32 up.

### State codes

`state` is derived, so it is enough on its own to render a machine-state
timeline — you never have to combine it with `mode` and `error` to work out what
a machine was doing:

```
a stopping fault is active  ->  30 FAULT
mode == setup               ->  20 SETUP
mode == manual              ->  40 STOPPED
otherwise                   ->  the machine's current process phase
```

| code | phase | profile |
|---|---|---|
| `0` / `10` | idle / running | all |
| `20` / `30` / `40` | setup / fault / stopped | all |
| `50` `51` `52` `53` `54` | charging, heating, soaking, quenching, discharging | oven |
| `60` `61` `62` `63` `64` | filling, washing, rinsing, drying, draining | washing |
| `70` | tool change | cnc |

A stopping fault outranks everything, so an alarm is never hidden by the machine
having been left in setup.

### Alarms

One rule: **every alarm has a cause you can see in the data.** There is no random
fault generator. A *stopping* fault sets `error`, forces `state = FAULT` and
freezes the counters *and* the process — an interrupted oven charge resumes where
it left off. A *warning* sets `warning` only and the machine keeps producing,
which is the case that quietly breaks naive downtime maths.

## Profiles

### `generic` — 10 addresses

The plain machine, unchanged from v0.2: one part per cycle, `S7_SCRAP_RATE` of
them scrap, temperature on a slow sine, speed only in auto. No process phases and
no alarms.

### `cnc` — 17 addresses

A 3-axis milling centre. Core `temperature` is the spindle, `speed` the spindle
rpm. One part per cycle.

| address | type | name | behaviour |
|---|---|---|---|
| `DB1.R32` | REAL | `measured_diameter` | mm — resampled once per part, then **held** |
| `DB1.R36` | REAL | `coolant_pressure` | bar — per part, and a far more capable process |
| `DB1.R40` | REAL | `coolant_level` | % — depletes over a tool life, refilled at the change |
| `DB1.I44` | INT | `tool_number` | +1 per tool change |
| `DB1.I46` | INT | `tool_life_pct` | 100 → 0 across 40 parts |
| `DB1.R48` | REAL | `spindle_load` | % — rises as the tool wears |
| `DB1.R52` | REAL | `feed_rate` | mm/min |

**The SPC bit.** The diameter is nominal **25.000 mm ± 0.050**, with a short-term
sigma of 0.008 mm and a further **+0.045 mm of drift** across one tool life. So it
creeps toward the upper limit as the tool wears and steps back to nominal at the
tool change: an X-bar/R chart shows a real trend with a real reset, and Cp/Cpk is
worth computing. `coolant_pressure` sits next to it as a deliberately stable
process, so the two have visibly different capability.

**Scrap comes from one place only:** a part that measures outside tolerance.
`S7_SCRAP_RATE` is ignored on this profile (the simulator says so at startup), so
`scrap_parts` reconciles *exactly* against the measurements — every scrapped part
is one you can point at.

Alarms: `101` tool break (stopping — every 4th tool breaks near the end of its
life instead of finishing), `102` coolant low (**warning**), `103` spindle
overload (stopping — the load crosses its limit late in the life of a tool that
doesn't break). Each tool life therefore ends one of three ways.

### `oven` — 16 addresses

A hardening furnace. Core `temperature` is the chamber ramping toward setpoint,
`speed` the circulation fan.

| address | type | name |
|---|---|---|
| `DB1.R32` | REAL | `setpoint_temperature` — 860 °C |
| `DB1.R36` | REAL | `heater_power` — % |
| `DB1.R40` | REAL | `carbon_potential` — %C |
| `DB1.DI44` | DINT | `batch_number` |
| `DB1.I48` | INT | `batch_size` — 200 |
| `DB1.I50` | INT | `phase_remaining_s` |

CHARGING → HEATING → SOAKING → QUENCHING → DISCHARGING, then round again. The
counters stay **flat for the whole charge** and then jump by the entire batch on
discharge:

```
state=52 soaking      good=1400  batch=8
state=53 quenching    good=1400  batch=8
state=54 discharging  good=1592  batch=8   <- +192 good and +8 scrap, in one step
state=54 discharging  good=1592  batch=9   <- the number moves on to the next charge
state=50 charging     good=1592  batch=9
```

The jump is published while `batch_number` still names the charge that produced
it, so a counter delta joins to the right batch.

That is a different signal shape from the CNC's `+1`, and the one that breaks
rate calculations written against a smooth counter.

Scrap is `S7_SCRAP_RATE` per part at discharge — **plus** the failure mode a
furnace actually has. A stopping fault freezes the phase clock, and if the
interruption during heating or soaking runs long enough, the metallurgy is wrong
and the **whole charge** is booked as scrap (`good +0, scrap +200`).

Alarms: `201` over-temperature (stopping — an overshoot at the top of the ramp,
which stalls long enough to ruin that charge), `202` door interlock (stopping —
while loading, so the charge is cold and survives), `203` atmosphere out of band
(**warning** — `carbon_potential` drifting during the soak).

### `washing` — 16 addresses

A basket washer. Core `temperature` is the bath, `speed` the pump.

| address | type | name |
|---|---|---|
| `DB1.R32` | REAL | `pump_pressure` — bar |
| `DB1.R36` | REAL | `conductivity` — µS/cm, contamination |
| `DB1.R40` | REAL | `detergent_level` — % |
| `DB1.R44` | REAL | `flow_rate` — l/min |
| `DB1.DI48` | DINT | `basket_number` |
| `DB1.I52` | INT | `phase_remaining_s` |

FILLING → WASHING → RINSING → DRYING → DRAINING, ~30 parts booked per basket —
a third counter magnitude between the CNC's `+1` and the furnace's `+200`.

Its alarms are the bath itself: detergent depletes basket by basket until `301`
detergent low (**warning**), while contamination climbs until `302` filter
clogged (stopping) forces a bath change that resets both. `303` pump pressure low
(stopping) trips when the pump sags below its limit. Every one of them has a
number you can watch climbing toward it.

## Reading it: dedup on `part_id`

A benthos poll is fixed at one second, but a CNC cycle is 45 s and a furnace
charge is minutes — and the measurement registers **hold their last value**,
exactly as a real PLC does. So the same measurement is read over and over. A
chart that plots every poll will show hundreds of identical points and compute a
badly wrong Cpk.

`part_id` is the key: it changes only when a part completes, so it identifies
the part a measurement belongs to.

This only applies to the *per-part* values — `measured_diameter` and
`coolant_pressure` on the cnc. `temperature`, `speed` and `spindle_load` are
genuinely live and should be kept at full rate.

The simplest place to do it is where you query, not where you ingest — store
every poll and collapse on read:

```sql
-- one row per part, whatever the poll rate was
SELECT DISTINCT ON (part_id) part_id, ts, measured_diameter
FROM   cnc_samples
WHERE  ts > now() - interval '8 hours'
ORDER  BY part_id, ts;
```

Doing it in benthos instead means holding the previous `part_id` in a `cache`
resource and dropping the measurement messages when it has not moved. That is
worth it only if the write volume actually hurts: at one poll a second a single
machine produces a few hundred thousand rows a day, which Postgres will not
notice.

## Environment variables

Seven, and only the first is new in v0.3. Everything about a machine's process
is a constant in its profile, documented above.

| Variable | Default | Meaning |
|-----------------------|---------|--------------------------------------|
| `S7_MACHINE_TYPE` | `generic` | `generic` \| `cnc` \| `oven` \| `washing`. `MACHINE_TYPE` works too |
| `S7_CYCLE_TIME` | per profile | Seconds per **machine cycle** — one part, or one charge/basket. Defaults: generic 60, cnc 45, oven 300, washing 120. `CYCLE_TIME` works too |
| `S7_UPDATE_INTERVAL` | `1.0` | Seconds between value updates |
| `S7_SCRAP_RATE` | `0.05` | Baseline share of parts booked as scrap. **Not used by `cnc`** |
| `S7_COUNTER_MAX` | `2147483647` | Counter wrap point (DINT max); lower it to test rollover |
| `S7_PORT` | `102` | Port the server listens on (in-container) |
| `S7_DB` | `1` | DB number to expose |

`S7_CYCLE_TIME` is the single time knob: alarm frequency and duration are
expressed in cycles, so turning it down fast-forwards the whole simulation with
nothing else to keep in sync. `S7_CYCLE_TIME=2` on the oven gives you a complete
charge, including a ruined one, in under a minute.

## Dumping a profile's addresses

The `addresses:` list for the selected profile, ready to paste:

```bash
docker run --rm -e S7_MACHINE_TYPE=oven skumh/s7comm-simulator:0.3 \
  python simulator.py --addresses
```

It needs no PLC and no native library, so it also works from a plain checkout:
`S7_MACHINE_TYPE=cnc python simulator.py --addresses`.

## Run from Docker Hub

A prebuilt multi-arch image (`linux/amd64` + `linux/arm64`) is published at
[`skumh/s7comm-simulator`](https://hub.docker.com/r/skumh/s7comm-simulator):

```bash
docker run --rm -p 1102:102 skumh/s7comm-simulator:0.3
```

This maps host **`1102` → container `102`** (so you don't need root for a low
port on the host); point clients at `127.0.0.1:1102`. To use the canonical S7
port instead, run with `-p 102:102`. Tags: `0.3`, `0.2`, `0.1` (pinned) and
`latest`.

## Run from source

```bash
docker compose up -d --build
docker compose logs -f          # watch the simulated values
```

Or bring up a whole line — four machines on four ports:

```bash
docker compose -f docker-compose.line.yml up -d --build
docker compose -f docker-compose.line.yml logs -f s7-oven
```

On a machine without the source, `docker-compose.hub.yml` runs cnc, oven and
washing straight from Docker Hub — no build step:

```bash
curl -O https://raw.githubusercontent.com/krgrsebastian/S7Comm-Simulator/main/docker-compose.hub.yml
docker compose -f docker-compose.hub.yml up -d
```

| machine | port |
|---|---|
| generic | 1102 |
| cnc | 1103 |
| oven | 1104 |
| washing | 1105 |

## Read it with benthos-umh

```bash
benthos-umh run -c benthos-test.yaml
```

`benthos-test.yaml` reads the **core block only**, which is why it works against
any profile — point `tcpDevice` at whichever port you want. Append a profile's
own block with `--addresses` when you want its process parameters.

Key config points:

- `tcpDevice: "127.0.0.1:1102"`, `rack: 0`, `slot: 1` — the gos7 defaults.
- `disableCPUInfo: true` — the server doesn't answer the CPU-info (SZL) request,
  so disabling it avoids a harmless startup warning.
- Addresses are auto-batched by the plugin and read via `AGReadMulti`.

In **UMH Core**, replace the `stdout` output with a `tag_processor` → `uns: {}`
pipeline as usual.

## Tests

The process models live in `profiles/`, which deliberately never imports
`snap7`, so they can be driven directly — no container, no PLC:

```bash
python -m unittest -v
```

41 tests covering the invariants: counters only ever increase, nothing is
produced outside auto mode or while a fault is held, the process is frozen (not
merely the counters) during a stopping fault, every phase is reachable, the CNC's
measurement is held for exactly one `part_id`, its scrap reconciles exactly
against tolerance, a batch books its whole charge at once, and every profile fits
in a single benthos read — plus that no phase becomes invisible and no batch
is misattributed when `S7_CYCLE_TIME` is turned down far enough that several
phase transitions land on one poll.

## Notes / limitations

- **A profile must stay under 20 addresses.** benthos-umh reads at most 20 items
  per `AGReadMulti` (`s7comm_plugin/s7comm.go`, `maxItemsPerBatch`) — the binding
  limit, not the PDU. Beyond it a profile is fetched in two round-trips that
  arrive as one `service.MessageBatch`, so a measurement can be paired with a
  counter from a few milliseconds later while looking perfectly atomic. The
  profiles here use 10–17 addresses and the simulator warns at startup if a
  future one crosses the line.
- **One behaviour change from v0.2**: the `error` bit at `DB1.X12.0` used to
  flicker at random on the generic machine. It now means something specific —
  production is stopped — and the generic profile has no alarms, so it stays
  false. Every v0.2 *address* keeps its offset, type and meaning.
- This is the native Snap7 server. It handles connect and single/multi-variable
  reads (`AGReadMulti`), which is all the benthos plugin needs. It does not
  implement every SZL/diagnostic function of a real CPU (hence
  `disableCPUInfo: true`).
- Rack/slot are not strictly validated; the gos7 defaults (rack 0, slot 1)
  connect fine.
- Counters reset on restart and wrap at `S7_COUNTER_MAX`; nothing else resets
  them.
