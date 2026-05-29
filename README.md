# S7Comm PLC Simulator

A Docker container that acts as a **real Siemens S7 PLC** over the S7comm
protocol (ISO-on-TCP / RFC1006, TCP port 102), so you can develop and test the
benthos-umh `s7comm` input without physical hardware.

It is built on [`python-snap7`](https://python-snap7.readthedocs.io) **1.3**,
which wraps the **native `libsnap7`** server — the de-facto reference S7 PLC
simulator, and the exact dialect `gos7` (and therefore benthos-umh) is tested
against. The bundled native lib works on both `amd64` and `arm64` (Apple
Silicon).

> ⚠️ Version pin matters: `python-snap7 >= 3.0` replaced the server with a
> pure-Python reimplementation whose ISO/COTP handshake is **not** compatible
> with gos7 (benthos fails with `ISO : Invalid PDU received`). This image
> deliberately pins `1.3`.

## Datapoints

All values live in **DB1** and are updated once per second. Layout is
big-endian (Siemens standard), matching exactly how the benthos plugin decodes
each type:

| benthos address | S7 type   | Datapoint     | Behaviour                                  |
|-----------------|-----------|---------------|--------------------------------------------|
| `DB1.I0`        | INT (16)  | `mode`        | 0 = auto, 1 = manual, 2 = setup            |
| `DB1.R4`        | REAL (32) | `temperature` | slow sine ~20–80 °C + noise                |
| `DB1.DI8`       | DINT (32) | `rpm`         | ~1500 ± noise in auto mode, else 0         |
| `DB1.X12.0`     | BOOL      | `error`       | ~3 % chance per tick to be set             |

To add/change points, edit the offsets and the simulation loop in
`simulator.py`, then keep the `addresses:` list in `benthos-test.yaml` in sync.

## Run from Docker Hub

A prebuilt multi-arch image (`linux/amd64` + `linux/arm64`) is published at
[`skumh/s7comm-simulator`](https://hub.docker.com/r/skumh/s7comm-simulator):

```bash
docker run --rm -p 1102:102 skumh/s7comm-simulator:0.1
```

This maps host **`1102` → container `102`** (so you don't need root for a low
port on the host); point clients at `127.0.0.1:1102`. To use the canonical S7
port instead, run with `-p 102:102`. Tags: `0.1` (pinned) and `latest`.

## Run from source

```bash
docker compose up -d --build
docker compose logs -f          # watch the simulated values
```

The compose file maps host **`1102` → container `102`** (so you don't need root
for a low port on the host). Point clients at `127.0.0.1:1102`.
To use the canonical S7 port instead, change the mapping to `"102:102"`.

## Read it with benthos-umh

```bash
benthos-umh run -c benthos-test.yaml
```

Key config points (see `benthos-test.yaml`):

- `tcpDevice: "127.0.0.1:1102"`, `rack: 0`, `slot: 1` — the gos7 defaults.
- `disableCPUInfo: true` — the pure-Python server doesn't answer the CPU-info
  (SZL) request, so disabling it avoids a harmless startup warning.
- Addresses are auto-batched by the plugin and read via `AGReadMulti`
  (verified working against this server).

In **UMH Core**, replace the `stdout` output with a `tag_processor` →
`uns: {}` pipeline as usual.

## Environment variables

| Variable              | Default | Meaning                              |
|-----------------------|---------|--------------------------------------|
| `S7_PORT`             | `102`   | Port the server listens on (in-container) |
| `S7_DB`               | `1`     | DB number to expose                  |
| `S7_UPDATE_INTERVAL`  | `1.0`   | Seconds between value updates        |

## Notes / limitations

- This is the native Snap7 server. It handles connect and single/multi-variable
  reads (`AGReadMulti`), which is all the benthos plugin needs. It does not
  implement every SZL/diagnostic function of a real CPU (hence
  `disableCPUInfo: true` — otherwise you get a harmless "Failed to get CPU
  information" warning).
- Verified end-to-end: a real `benthos-umh` build connects, negotiates PDU 480,
  and reads all four addresses via `AGReadMulti`.
- Rack/slot are not strictly validated; the gos7 defaults (rack 0, slot 1)
  connect fine.
