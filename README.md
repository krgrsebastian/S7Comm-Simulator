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

## Data models

`datamodels.yaml` holds a UMH data model per machine type — `generic`, `cnc`,
`oven`, `washing`, all at **v4** — so the data contracts are `_generic_v4`,
`_cnc_v4` and so on. Paste **both** top-level blocks (`payloadShapes:` and
`dataModels:`) into a umh-core `config.yaml`. Field counts match the address counts exactly: 10 / 17 / 16 / 16.

**A flat core, then grouped process values.** These eight fields are flat and
identical in every model, so one query spans every asset whatever the machine
type:

```
mode  state  fault_active  warning_active  fault_code
good_count  scrap_count  part_count
```

Everything machine-specific is grouped, and a group becomes the tag's
**virtual_path**:

| machine | groups |
|---|---|
| `cnc` | `spindle`, `feed`, `tool`, `coolant`, `measurement` |
| `oven` | `chamber`, `atmosphere`, `batch` |
| `washing` | `bath`, `pump`, `basket` |
| `generic` | none — all flat |

So `spindle: { speed_rpm: … }` lands as virtual_path `spindle`, tag_name
`speed_rpm`, topic suffix `…/spindle/speed_rpm`.

**Never edit a version in place — always bump it.** umh-core's registry
reconcile (`pkg/service/redpanda/schema_registry.go`, compare phase) matches on
the subject *name* only and never compares schema content, so a subject that
already exists is left exactly as it was. Editing `v2` after it has been
deployed leaves the old schema registered and the `uns` output keeps rejecting
messages with `datatype mismatch … want timeseries-string, got
timeseries-number`. Bumping the version makes the old subjects unexpected, and
the same reconcile deletes them for you.

Version history: `v1` flat with string enums; `v2` grouped with string enums
(unusable from a plain CSV bridge); `v3` grouped and numeric, but with the two
bit addresses wrongly declared as numbers; `v4` with those as
`timeseries-boolean`.

**The S7 type dictates the payload shape — it is not a modelling choice.**
benthos-umh's converter (`s7comm_plugin/type_conversions.go`,
`determineConversion`) decides what arrives on the wire:

| S7 type | Go type | payload shape |
|---|---|---|
| `X` (bit) | `bool` | `timeseries-boolean` |
| `I`, `DI`, `W`, `DW`, `B` | int / uint | `timeseries-number` |
| `R` | `float32` | `timeseries-number` |
| `C`, `S` | `string` | `timeseries-string` |

Declare anything else and the `uns` output rejects every message for that field
with `datatype mismatch … want X, got Y`. That is why `mode` and `state` are
numbers — the PLC serves INT codes, and a plain address-to-tag bridge has
nowhere to translate them (render the names in Grafana with value mappings; the
code tables are in the model file's header). And it is why `fault_active` and
`warning_active` are `timeseries-boolean`: they are bit addresses.

`timeseries-boolean` is declared explicitly in the `payloadShapes:` block.
Some umh-core versions inject only `timeseries-number` and `timeseries-string`
as defaults (`pkg/datamodel/validator.go`, `ensureDefaultPayloadShapes`), and
the injector never overrides a shape that is already defined — so declaring it
works on every version.

`tools/gen-bridge-csv.py` enforces the whole table above, so a field can no
longer be declared with a shape its address cannot produce.

## Historian

`historian/processing.js` and `historian/destination.yaml` are the two blocks of a
`uns_to_postgres` bridge, tailored to the `cnc` contract — paste them into the
protocol converter's **Processing** and **Destination** fields. They were cloned
from a working bridge and adjusted only as intended: `const CONTRACT = "cnc"`,
every table renamed to `umh.value_cnc` / `umh.attribute_cnc` in both writers, and
both `dsn:` lines set to `${HISTORIAN_DSN}`.

**One bridge per contract, not per machine.** It reads the whole UNS and filters
on the contract, so every CNC lands in the same two tables.

`data_contract_name` is stored **without** the version: rows for `_cnc_v4` appear
as `_cnc`, and all versions of a contract share one table. That string is what
dashboards filter on.

Location paths are normalised the ltree way — every character outside
`[A-Za-z0-9_]` becomes `_`, so `umh-factory` is stored as `umh_factory`.
`umh.get_topic_id()` normalises its argument the same way, so a panel may pass
either spelling.

The boolean tags land in `value_num` as 0/1 with `value_type = 'numeric'`, which
is what the historian does with every boolean.

### On a fresh database, run the two init blocks once, by hand

Verified the hard way. The value writer's `init_statement` opens with
`BEGIN; GRANT USAGE ON SCHEMA umh …` — *before* its own `CREATE SCHEMA`. On an
empty database that GRANT fails, which aborts the transaction, so the block never
reaches `CREATE TABLE umh.value_cnc`. Worse, the aborted transaction stays open on
that pooled connection, so every insert afterwards fails with
`25P02 current transaction is aborted` and never recovers on its own. The two
`fan_out` writers also race each other on the shared DDL (`deadlock detected`).

So on a new database, extract both `init_statement` blocks and run them once —
**the metadata writer first**, since it is the one that bootstraps the schema,
then the value writer — and only then start the bridge. (Running them the other
way round reproduces the failure: the value writer's GRANT hits a schema that
does not exist yet.) After that they are idempotent no-ops. An
existing historian database already has the schema, so this only bites on a first
deploy.

### Missing helper functions

`umh.get_topic_id()`, `umh.to_ltree_path()` and the two write-side guards are
created by the bridge's `init_statement`, so a database set up by an older
bridge — or one whose init aborted part-way — can be missing them. Check with
`\df umh.*` **while connected to the historian database**; run it against
`postgres` and you get an empty list no matter what.

`historian/helpers.sql` installs all four. Every statement is `CREATE OR
REPLACE`, no table is touched, and it is safe to re-run against a live
historian:

```bash
docker exec -i <timescaledb-container> psql -U postgres -d umh < historian/helpers.sql
```

The dashboard does not need them — the panels join the four tables directly —
but `to_ltree_path()` is the canonical normaliser for comparing a hand-written
location path against a stored one.

### Watch out for a tag pinned to the wrong datatype

`umh.tag.value_type` is pinned on a tag's first write and a trigger rejects any
later change. If a tag ever arrived as text — e.g. while a bit was still coming
through as the string `"true"` — every later numeric write for it is refused.
Delete that `umh.tag` row (and its `umh.topic` rows) and let the bridge recreate
it.

## Andon dashboard

`grafana/andon-cnc.json` — import it, choose your historian when the import
dialog asks for **TimescaleDB**, then pick machines in **Maschine**; the board
grows a tile and a detail row per selection.

The file is in Grafana's **export-for-sharing** format: it declares the
datasource as an `__inputs` entry (`DS_TIMESCALEDB`) and every panel, target and
variable references `${DS_TIMESCALEDB}`. On import Grafana prompts for the
datasource once and substitutes the real uid throughout, so nothing is left
pointing at a foreign uid. Two earlier shapes did not survive the import dialog:

- A hardcoded uid per panel *and* per target. A target's datasource overrides its
  panel's, so changing it in the panel header left the query bound to the old uid
  and it silently ran elsewhere.
- A `${DS}` datasource *template variable* with an empty `current` — the import
  dialog does not prompt for it, so it stays unselected.

The `machine` variable's `query` is a plain **string**. Written as the object
form (`{rawSql: …}`) it survives an API POST but the import path stringifies it,
and the query shows up as `[object Object]`.

**Never wrap a multi-value variable in your own quotes.** The panels select the
machine with

```sql
WHERE l.path = ANY(ARRAY[${machine:sqlstring}]::ltree[])
```

not `l.path = '$machine'::ltree`. For a multi-value variable Grafana's SQL
formatter supplies the quotes itself (`'a','b','c'`), so wrapping it again yields
`''a','b','c''` — Postgres reads the leading `''` as an empty string and then
trips over the next token, reporting `syntax error at or near "umh"` for a path
beginning with `umh`. `:sqlstring` plus `= ANY(ARRAY[...])` is correct for one
value and for many, so it works in a repeated panel (one value per instance) and
in the panel editor alike, where repeats are not applied and the variable still
holds every selected value.

No query contains a newline or an SQL comment, deliberately: some Grafana
versions collapse a rawSql into one line, which turns a leading `--` comment into
a comment over the whole statement.

The variable lists whatever writes into the contract, so a new CNC appears on its
own:

```sql
SELECT DISTINCT
       subltree(l.path, nlevel(l.path)-1, nlevel(l.path))::text AS __text,
       l.path::text AS __value
FROM umh.location l
JOIN umh.topic t ON t.location_id = l.location_id
JOIN umh.tag   g ON g.tag_id      = t.tag_id
WHERE g.data_contract_name = '_cnc'
ORDER BY 1
```

`__text` is the last ltree label, so the dropdown shows `hermle_c400` while the
panels get the full path. Layout: an **Andon** row of status tiles (one per
machine, repeated horizontally), then a repeated detail row per machine with
status, active alarm, good/scrap counts, tool life, spindle load, and the SPC
chart.

Panels resolve a tag by joining `value_cnc → topic → tag → location` and
filtering on `l.path = '$machine'::ltree` plus `data_contract_name`,
`virtual_path` and `name`. There is a helper, `umh.get_topic_id()`, that does the
same as a point lookup with no joins — but the panels deliberately do not use it.
A historian database is created by whichever bridges have run against it, so its
function set is not guaranteed; the joins only need the four tables, which every
bridge creates.

Colour appears only where it carries meaning: green for running, orange for a
warning or a tool change, red for a stopping fault. Idle, setup and stopped are
mapped to `transparent`, not to a colour — a pale filled tile reads as "something
is happening here" from across the shop floor, which is exactly wrong for a quiet
state. The state name is always written out, so the board still reads correctly
with all colour removed.

Verified by rendering, not by reading the JSON: the dashboard was imported into
Grafana 13.0.2 against a live TimescaleDB seeded by two simulators through the
real historian bridge, then rendered as PNG with one and with two machines
selected. Both repeats, the `${DS}` variable, the `machine` variable and all
eight panels resolve and show data. Two defects only visible in the render were
fixed that way: `Werkzeugwechsel` was clipped in a 4-unit-wide stat, and the
no-fault and quiet-state tiles were rendering as filled pale blocks.

**The SPC panel dedups.** `measurement.diameter_mm` is held between parts, so a
raw query returns the same value once per poll — 168 rows for 34 parts in
testing. The panel keeps only rows where the value changed, which yields exactly
one point per part (34 points against a part counter that advanced 33) without
assuming the diameter and `part_count` rows share a timestamp.

Grants: the historian writes into schema `umh`, so `grafana_reader` needs
`USAGE` on that schema — the bridge's init does this, but a reader role created
later needs it granted again, otherwise every panel returns permission denied.

## Bridge tag CSVs

`bridge-<machine>.csv` is the tag list in Management Console import format:

```
Address,Location Path Suffix,Data Contract,Virtual Path,Tag Name
DB1.DI8,,_cnc_v4,spindle,speed_rpm
DB1.R32,,_cnc_v4,measurement,diameter_mm
DB1.X12.0,,_cnc_v4,,fault_active
```

They are generated, not hand-written:

```bash
python tools/gen-bridge-csv.py          # regenerate all four
python tools/gen-bridge-csv.py --check  # verify, write nothing
```

The generator derives every row from `datamodels.yaml` and checks two things,
both of which have bitten this repo already:

1. Every address exists in `simulator.py --addresses` for that profile, so the
   model cannot drift from the PLC layout.
2. Every field's `_payloadshape` matches the shape its S7 type actually
   produces.

Either mismatch fails loudly instead of producing a CSV that imports cleanly
and then breaks at runtime.

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
