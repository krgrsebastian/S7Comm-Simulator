#!/usr/bin/env python3
"""Generate the bridge tag CSV for each machine profile.

The CSV is derived from datamodels.yaml, and every address is cross-checked
against `simulator.py --addresses` for that profile. If the model and the PLC
layout ever disagree, this fails loudly instead of emitting a CSV that imports
cleanly and then reads the wrong bytes.

    python tools/gen-bridge-csv.py            # writes bridge-<profile>.csv
    python tools/gen-bridge-csv.py --check    # verify only, write nothing

The `Data Contract` column is `_<model>_<version>`, with the version read from
the model file. Set S7_MODEL_VERSION to pick one when a model declares several.
"""

import os
import re
import subprocess
import sys
import pathlib

try:
    import yaml
except ImportError:
    sys.exit("this script needs PyYAML: pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODEL_FILE = ROOT / "datamodels.yaml"
VERSION_OVERRIDE = os.environ.get("S7_MODEL_VERSION")
HEADER = "Address,Location Path Suffix,Data Contract,Virtual Path,Tag Name"

# field name -> S7 address, taken from the trailing comment on each field line
ADDR_RE = re.compile(r"^\s+([a-z0-9_]+):\s+#\s*(DB\d+\.[A-Z]+[0-9.]*)")


def addresses_by_field(model_block: str) -> dict:
    out = {}
    for line in model_block.splitlines():
        m = ADDR_RE.match(line)
        if m:
            out.setdefault(m.group(1), m.group(2))
    return out


def model_blocks(text: str) -> dict:
    """Split the file into one text block per model, so identical leaf names in
    different models (temp_c, number, ...) can't be confused for each other."""
    parts = re.split(r"\n  - name: ", text)
    blocks = {}
    for part in parts[1:]:
        name = part.split("\n", 1)[0].strip()
        blocks[name] = part
    return blocks


def leaf_paths(structure, prefix=""):
    """Yield (virtual_path, tag_name) for every leaf, in declaration order."""
    for key, value in structure.items():
        if not isinstance(value, dict):
            continue
        if "_payloadshape" in value:
            yield prefix, key
        else:
            yield from leaf_paths(value, key)


def simulator_addresses(profile: str) -> set:
    env = dict(os.environ, S7_MACHINE_TYPE=profile)
    out = subprocess.run(
        [sys.executable, str(ROOT / "simulator.py"), "--addresses"],
        env=env, capture_output=True, text=True, check=True,
    ).stdout
    return set(re.findall(r'"(DB\d+\.[A-Z]+[0-9.]*)"', out))


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    text = MODEL_FILE.read_text()
    doc = yaml.safe_load(text)
    blocks = model_blocks(text)

    failures = []
    for model in doc["dataModels"]:
        name = model["name"]
        versions = model["version"]
        if VERSION_OVERRIDE:
            version = VERSION_OVERRIDE
        elif len(versions) == 1:
            version = next(iter(versions))
        else:
            failures.append(
                "%s declares %s — set S7_MODEL_VERSION to choose"
                % (name, sorted(versions))
            )
            continue
        if version not in versions:
            failures.append("%s has no version %s" % (name, version))
            continue
        structure = versions[version]["structure"]
        addr_of = addresses_by_field(blocks[name])

        rows, missing = [], []
        for vpath, tag in leaf_paths(structure):
            addr = addr_of.get(tag)
            if addr is None:
                missing.append(tag)
                continue
            rows.append("%s,,_%s_%s,%s,%s" % (addr, name, version, vpath, tag))

        if missing:
            failures.append("%s: no address comment for %s" % (name, missing))
            continue

        from_model = {r.split(",")[0] for r in rows}
        from_sim = simulator_addresses(name)
        if from_model != from_sim:
            failures.append(
                "%s: model and simulator disagree — only in model %s, only in simulator %s"
                % (name, sorted(from_model - from_sim), sorted(from_sim - from_model))
            )
            continue

        out = HEADER + "\n" + "\n".join(rows) + "\n"
        target = ROOT / ("bridge-%s.csv" % name)
        if check_only:
            existing = target.read_text() if target.exists() else ""
            state = "up to date" if existing == out else "STALE"
        else:
            target.write_text(out)
            state = "written"
        print("  %-8s %2d tags  %s  %s" % (name, len(rows), target.name, state))

    for f in failures:
        print("FAIL", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
