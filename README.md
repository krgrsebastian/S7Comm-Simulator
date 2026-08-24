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

Two blocks in one dashboard. On top a **Plant Status** board in the shape of the
Webasto PULSE concept: one column per machine, a running timer, and below it one
colour-coded cell per status dimension. Underneath, the per-machine detail row
(counters, tool life, spindle load, SPC chart) for drilling in.

```
                  line_01              line_02              line_03
Operation · 4m    running
Operation · 52s                        FAULT
Operation · 30m                                             setup
Fault             none                 tool break           none
Quality           OUT OF TOLERANCE     in tolerance         in tolerance
Tool              ok                   ok                   worn out
Coolant           ok                   ok                   low
```

**Every cell states its own condition in words**, with colour as reinforcement,
not as the only carrier of meaning. A colour-only tile works for a binary row;
for a multi-valued state like `state` it does not — you cannot tell "setup"
from "stopped" from "no data" by shade. The value is encoded as
`dimension*100 + condition` and a value-mapping table gives each code its text
and colour, so a tile reads `Operation running`, `Operation FAULT`, `Tool worn out`.

Grey means exactly one thing — `stopped`, spelled out. Genuinely missing data
renders transparent and says `no data`. Those must never share a colour.

**How long the state has lasted rides in the Operation row itself**, not in a
tile of its own: `Operation · 52s` next to `FAULT` means down for 52 seconds. It
is the time since the last change of `state`, so the label says exactly what it
measures; falling back to the start of the dashboard window when the state has
not changed within it. Three earlier attempts failed and are worth not
repeating: a separate tile with a bare number (unlabelled), then
`ohne Störung seit` (outright false on a faulted machine), then `Status seit`
(vague enough that it had to be explained). A label that needs explaining is a
defect, not a documentation task.

The rows are the dimensions the CNC contract actually carries — no placeholder
cells:

| row | source | green | yellow | orange | red | grey |
|---|---|---|---|---|---|---|
| Operation | `state` | running | setup | tool change | FAULT | stopped |
| Fault | `fault_code` | none | coolant low | — | tool break, spindle overload | — |
| Quality | `measurement.diameter_mm` | in tolerance | — | — | out of tolerance | — |
| Tool | `tool.life_pct` | ok > 30 % | change soon 15–30 % | — | worn out < 15 % | — |
| Coolant | `coolant.level_pct` | ok > 20 % | low ≤ 20 % | — | — | — |

The dashboard is in English throughout.

A missing value renders transparent rather than green, so "no data" never reads
as "fine". The timer counts seconds since the last change of `fault_active`, so
it reads as time-since-recovery on a healthy machine and as fault duration on a
faulted one.

Two Grafana details worth knowing if you adapt it: the status column is a single
`stat` panel with `reduceOptions.values: true`, `textMode: "name"` and
`orientation: "horizontal"` — one tile per returned row, label shown, colour from
thresholds. `orientation` is the opposite of what it sounds like: `vertical`
puts the tiles side by side, `horizontal` stacks them as rows.


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

## Andon board — a whole area, one column per line

`grafana/andon-milling-center.json` (uid `andon-milling-center`) is the
wall-mounted view of the area: one column per production line, the line's three
stations stacked in process order, and the flow between them drawn as a chain.

```
line 01                          line 02                          line 03

MILL            hermle_c400      MILL            hermle_c400      MILL            hermle_c400
● running          (95%) OEE     ● FAULT            (48%) OEE     ● tool change      (87%) OEE
AVAIL 97% QUALITY 98% PERF 100%  AVAIL 50% QUALITY 97% PERF 100%  AVAIL 88% QUALITY 98% PERF 100%
 │                               ┊ NO FLOW                        ┊ NO FLOW
WASH      ecoclean_ecocwave      WASH      ecoclean_ecocwave      WASH      ecoclean_ecocwave
● washing          (96%) OEE     ● washing          (92%) OEE     ● setup            (59%) OEE
 │                                │                               ┊ NO FLOW
HARDEN                 oven      HARDEN                 oven      HARDEN                 oven
● heating          (98%) OEE     ● stopped          (51%) OEE     ● quenching        (91%) OEE
```

Parts run mill → wash → harden, so a stop anywhere backs up the whole line.
That is what the layout encodes: each column is one **chain**. The accent down
the left of the cards continues into the link between them, so a line reads as a
single spine — and where the upstream station is not producing, the spine tears
into red dashes labelled `NO FLOW`. It is the one place the design spends any
boldness; everything else is hairlines and quiet type.

**Clicking a card opens a detail modal**: the machine's state, a mini state
timeline across the dashboard window, the six values that matter for that
machine type, and — for a CNC — buttons through to the drill-down and the SPC
board, which carry the machine and the time window with them. Oven and washing
cards say plainly that no detail board exists for them yet rather than linking
to a CNC board that would come up empty.

Three things about how it is built:

- **The modal is created on `document.body`, not inside the panel.** The panel's
  content lives in a shadow root inside a Grafana grid item, and that grid item
  carries a CSS transform, so `position: fixed` resolves against the panel
  instead of the viewport — a modal built inside the panel is trapped in it. It
  therefore also needs its own `<style>` element (id-guarded, so a remount does
  not stack copies) and its own copy of the theme variables, which it cannot
  inherit from the board.
- **One delegated click listener on the board root**, attached in `onInit`.
  `onRender` replaces only the root's `innerHTML`, so the root itself — and the
  listener — survives every refresh.
- **The mini timeline merges by state *tone*, not by state name.** An oven runs
  charging / heating / soaking / quenching / discharging and all five are
  producing, so naming them in the legend gives five identical green swatches:
  one colour standing for five things. The legend reads `producing / setup /
  not running / fault` and the exact phase stays in each segment's tooltip.

The panel runs three queries: the station summary, a state history bucketed into
48 buckets across the window, and the latest value of six tags per machine type.
The tag list is a `VALUES` table joined onto `tag`, so adding a value to a modal
is one row of SQL rather than another union leg.

**The card answers one question: what is this machine doing right now.** The
state is the largest thing on it, in its own colour, with a status dot ahead of
it; a machine in FAULT additionally gets a red-washed card and a heavier red
band, because "is anything broken" has to be answerable from across the room
without reading a word.

OEE sits at the far end of the same row, small, **monochrome**, and labelled —
something you read on the second look, with AVAIL / QUALITY / PERF underneath it.
Monochrome is the point: while the ring carried the usual green/amber/red
thresholds, a *running* machine with a poor shift showed a green state word next
to a red ring, so red meant two different things on one card. Colour on this
board belongs to the machine state and to nothing else.

Requires the **`gapit-htmlgraphics-panel`** plugin (tested with 2.2.3):

```bash
grafana-cli plugins install gapit-htmlgraphics-panel   # or GF_INSTALL_PLUGINS=gapit-htmlgraphics-panel
```

Its CSS is shadow-DOM scoped, so the panel's styles cannot leak into Grafana and
Grafana's cannot leak in.

Every colour, font, radius and spacing comes from **Grafana's own theme**, read
in `onInit` and written onto the board as CSS custom properties, so the board
follows the instance's light/dark setting instead of carrying a private palette.

Three things cost time here and are worth knowing:

- **The plugin hands the script the v1 theme** (`config.theme`), *not*
  `GrafanaTheme2`. So the paths are `colors.panelBg`, `colors.border1`,
  `colors.text`, `palette.greenBase`, `typography.fontFamily.sansSerif`,
  `border.radius.sm`, `spacing.md` — and `spacing` is an **object**, not the
  `theme.spacing(n)` function. GrafanaTheme2 paths do not throw, they just
  resolve to `undefined` and fall through to the fallbacks: the board then looks
  correct in the dark theme (where the fallbacks live) and stays stubbornly dark
  in the light one. If in doubt, dump the object from inside the panel rather
  than guessing the shape.
- **The OEE donut is a `conic-gradient` with a `::after` cut-out**, and that
  pseudo-element comes after the value span in DOM order — so with both
  positioned and no `z-index`, the cut-out paints over the number and the ring
  renders empty. `.ring-val` needs `z-index: 1`.
- **Set the theme variables in `onInit`, not `onRender`.** `onRender` runs on
  every data update, and mutating the root element's inline style there is
  layout work on every refresh for values that never change.

The OEE ring is the one number that carries the label with it (`OEE` under the
ring), because the components beside it — AVAIL, QUALITY, PERF — are already
labelled and an unlabelled ring in the middle would be the odd one out.
Performance is fixed at 100 %: there is no takt time in the contract to measure
against, so OEE here is availability × quality, and `PERF 100%` is greyed to say
so rather than implying it was measured.

`grafana/overview-milling-center.json` is the same data as three plain tables,
if you prefer sortable columns over a wall display.

## Shopfloor map — the same area as a plan

`grafana/shopfloor-milling-center.json` (uid `shopfloor-milling-center`) is the
second overview: the hall seen from above instead of as columns. Material enters
at `goods in` on the left, each line runs left to right through mill → wash →
harden, finished parts leave at `goods out`. Every machine is drawn as a
schematic of the real thing — a mill with column, spindle and table; a washer
with its drum and spray bar; a furnace with door and heating coils — so the plan
reads as a shopfloor rather than a grid of identical boxes. The machine's state
colours its outline, its status LED and its state word, and the conveyor between
two machines tears into red dashes when the upstream station is not producing.

**Clicking a machine opens the same detail modal as the Andon board.** The
modal implementation and the three queries are *lifted out of
`andon-milling-center.json` when this dashboard is generated*, so the two
overviews cannot drift apart in how a click behaves — only the drawing differs.
Editing the modal means editing the Andon board and regenerating this one.

The machines are SVG `<g class="station" data-path="…">` groups, which is why
the Andon board's delegated click handler works here unchanged: `closest()`
walks up out of a nested `<path>` or `<rect>` to the group exactly as it does in
HTML.

Two notes if you change the drawing:

- The svg is positioned `absolute` inside a `position: relative` wrapper. As a
  flex item with an auto height, `height: 100%` on the svg has nothing to
  resolve against, so it falls back to its aspect-ratio height and pushes the
  legend out of the panel.
- SVG text does not clip or ellipsise, so a long machine name simply draws over
  the box border. The boxes are sized for the names in use and `clip()` is the
  fallback; the full name stays in the group's tooltip either way.

## Navigation

Links go one way. Both overviews reach the two CNC boards, and those two reach
each other as peers:

```
andon-milling-center ─┐                  ┌─> cnc-detail <──> cnc-spc
                      ├─> CNC drill-down ┤
shopfloor-…-center  ──┘        SPC       └─
```

No board carries a "back to overview" link. With two overviews there is no
single right answer to which one a back button should return to, and two back
buttons side by side is worse than none — the browser's own back button does
the job. The modal's buttons and the header links both pass the machine and the
time window on.

## CNC drill-down

`grafana/cnc-detail.json` (uid `cnc-detail`) is the level below the Andon board:
**one** CNC in detail, picked with the `Machine` dropdown at the top.

The two boards are linked both ways through Grafana **dashboard links**
(Dashboard settings → Links), so they are navigation, not panel data links:

- the Andon board carries `CNC drill-down` → `/d/cnc-detail/cnc-drill-down`
- the drill-down carries `Back to Andon board` → `/d/andon-milling-center/andon`

Both set `keepTime`, so the window you were looking at survives the jump. They
deliberately do **not** set `includeVars`: the Andon board has no `machine`
variable to pass, so the drill-down opens on its first CNC and you pick the one
you want. (Making the Andon cards themselves clickable is a different job —
Grafana's data links do not work inside the HTML panel, it would need a
`window.open` handler in `onRender`.)

What is on it:

| Row | Panels |
|---|---|
| Right now | state, OEE, availability, quality (as gauges, markers at 60% and 85%), active fault, parts in window |
| State and faults | state timeline, fault log |
| Process parameters | the three percentages, spindle speed + feed rate, spindle temperature + coolant pressure |
| Why the machine was not producing | downtime Pareto by duration, the same by occurrence, and the arithmetic as a table |
| Output | the raw counters |

The diameter and its capability used to live here; they moved to their own SPC
board (below), because capability is a different question asked over a longer
window than "what is this machine doing".

**The downtime Pareto** names a FAULT episode by the code that caused it, so
`tool break` and `spindle overload` are separate reasons rather than one red
bucket, and the two rankings disagree on purpose: many short stops is a
different problem from one long one.

Its bars take the **state timeline's colours** — grey for a manual stop, yellow
for setup, orange for a tool change, red for any fault code — through
`options.colorByField: "reason"` plus value mappings on the `reason` field, with
a regex mapping so an unmapped code still reads as a fault instead of falling
back to grey. The same mappings colour the reason column of the table below, so
a bar's colour is decodable without a legend. Red on the Pareto is the same red
as a FAULT on the timeline.

Two things about how the episodes are built:

- State and `fault_code` are bucketed together before the reason is resolved,
  because the `s7comm` input carries no source timestamp — sibling tags land
  close together but not on the identical `ts`.
- Islands are numbered over *every* reason including `running`. Number them
  only over the downtime rows and two faults separated by production merge into
  one episode, which quietly halves the occurrence count.
- An episode shorter than the poll interval can be missed entirely. On a 1 s
  poll that is sub-second stops; it is worth knowing before treating the
  occurrence count as exact.

Two things worth knowing if you edit it:

- **The state timeline resolves the state name in SQL**, not through value
  mappings. Value mappings on a *numeric* field silently miss here — Grafana
  falls back to the threshold label and renders every state as one grey `-∞+`
  bar. So the query returns `'running'`/`'setup'`/`'FAULT'` as text and the
  mappings only assign colours.
- **mm/min is not a Grafana unit.** `velocitymms` renders literally as
  "900 velocitymms" on the axis; the feed rate uses `suffix:mm/min` instead.

`grafana/andon-cnc.json` predates this and overlaps with it: it is the
PULSE-style plant-status matrix for *several* CNCs at once, with a multi-select
`machine` variable. Keep it if you want the matrix; `cnc-detail` is the one to
open from the Andon board for a single machine.

## CNC SPC

`grafana/cnc-spc.json` (uid `cnc-spc`) is statistical process control on the
milled diameter, and the third corner of the link triangle: the Andon board and
the drill-down both reach it, and it links back to both. The drill-down passes
its selected machine through (`includeVars`), so stepping from one to the other
keeps the machine you were looking at.

| Row | Panels |
|---|---|
| Capability | Cp, Cpk, mean, sigma, parts measured, out of spec |
| Individuals and moving range | I-chart with control *and* spec limits, machine state on the same time axis, mR chart, tool life against diameter |
| What the diameter correlates with | deviation from nominal per band of tool life, spindle load, coolant pressure |
| Detail | out-of-spec parts with the conditions that made them, and a per-tool summary |

**One part is one sample.** The SPC unit is a finished part, keyed on
`part_count` — not on the sample rate. The measurement is *held* on the wire
until the next part, so sampling by time would count the same part a dozen
times and collapse sigma. The query buckets the tags, pivots them, then takes
the first row of each `part_count`.

**Sigma is the moving-range estimate, mR/1.128**, not the standard deviation of
the column. That is the standard estimate for an individuals chart, and here it
matters more than usual: the profile's true short-term sigma is 0.008 mm, and
mR/1.128 recovers 0.0084 while the plain standard deviation of the same parts
reads 0.0138 because it swallows the tool-wear drift. Cp near 2.0 with Cpk near
1.1 is the honest summary of this process: the spread is fine, the mean is
walking.

**The correlations are bars, not a scatter.** `xychart` never finishes loading
in Grafana 13.2 — the panel sits on "Loading plugin panel..." forever — so each
relationship is shown as the mean deviation from nominal per band of the other
value. Two reasons that turned out better than a scatter anyway: absolute
diameter on a zero-based bar axis hides everything (the whole signal is in the
fourth decimal, so every bar looks 25 mm tall), and in micrometres against the
50 µm tolerance line the tool-wear drift reads straight off the chart —
`90-100 %` tool life sits at nominal, `0-10 %` at +34 µm.

Spindle load correlates too, *through* tool wear rather than by causing
anything, and coolant pressure is the control case: a deliberately capable
process, so its bars stay flat. Nothing on this board is a causal claim.

### Checking a dashboard by eye

Rendering a dashboard headless is the only way to catch layout and legibility
defects, but the `grafana-image-renderer` container is a poor witness on a
loaded machine: its Chromium tab gets SIGKILLed (`Inspector.targetCrashed`,
`errorCode: 9`) and the symptom is a 60-second `rendering.serverTimeout` or a
blank PNG — which reads exactly like a broken panel. Raising the timeout does
not help, and neither does `--shm-size`; the tab is out of memory, not slow.
Confirm it by watching the container: 2 % CPU and ~8 MiB during a "render" means
no browser is running at all.

Faster and far more reliable: give the *test* instance anonymous access and
screenshot it with a browser on the host, which has the whole machine's memory.

```bash
docker exec -u 0 <grafana> sh -c \
  'printf "\n[auth.anonymous]\nenabled = true\norg_role = Admin\n" >> /etc/grafana/grafana.ini'
docker restart <grafana>

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --hide-scrollbars --user-data-dir=/tmp/prof \
  --window-size=1400,560 --virtual-time-budget=20000 --screenshot=board.png \
  "http://localhost:3000/d-solo/<uid>/x?orgId=1&panelId=1&from=now-2h&to=now&theme=light"
```

Render both `theme=light` and `theme=dark` — a panel that reads its own theme is
exactly the kind that looks fine in one and unreadable in the other. Only ever
enable anonymous access on a throwaway rig, and tear the rig down afterwards.

## Overview dashboard — a whole area

`grafana/overview-milling-center.json` (uid `milling-center`) is the level above
the Andon board: every asset in the `milling_center` area on one screen, three
values each and nothing more.

```
CNC — click a machine to drill down
  line_01   hermle_c400          running    100.0%   97.6%
  line_02   hermle_c400          FAULT       91.8%   97.6%
  line_03   hermle_c400          setup       94.6%   97.6%
Oven
  line_01   oven                 heating    100.0%   97.6%
  line_03   oven                 FAULT       89.0%   97.6%
Washing
  line_01   ecoclean_ecocwave    washing    100.0%   97.6%
  line_02   ecoclean_ecocwave    stopped     83.3%   97.6%
```

One table per machine type, because the three live in different tables
(`umh.value_cnc`, `umh.value_oven`, `umh.value_washing`) and only the flat core
of the contracts is shared. The queries use only `state`, `good_count` and
`scrap_count`, which every one of the three contracts carries.

**Availability is not "state = running".** An oven in `soaking` and a washer in
`rinsing` are producing — neither is ever `state = 10`. It counts the share of
samples in a productive state: `state = 10` or any process phase (`>= 50`).
Setup, stopped, idle and fault do not count.

**Quality** is the window delta, `Δgood / (Δgood + Δscrap)`, not the lifetime
ratio, so it reflects the selected time range.

The **Machine** column of the CNC table links to the Andon board for that
machine:

```
/d/andon-cnc/andon-cnc?var-machine=${__data.fields.path}&from=${__from}&to=${__to}
```

The full ltree path travels in a hidden `path` column, which is what the Andon's
`machine` variable uses as its value. The link sits only on the CNC table — the
oven and washer have no detail board yet, and a link into the CNC Andon with an
oven path would land on empty panels.

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
