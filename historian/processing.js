// ====================================================================
// BEFORE DEPLOYING, replace every MUST_CHANGE_* placeholder. Each is a distinct
// token, so a find-and-replace of one never corrupts another.
//
//   MUST_CHANGE_contract         the UNS data contract to archive, bare name only
//       (e.g. "pump"): no leading "_", no "_vN" suffix. It becomes part of the
//       table names umh.value_<name> / umh.attribute_<name>, so use lowercase letters,
//       digits and underscores only.
//   Metadata is ALWAYS archived into umh.attribute_<name>; there is no on/off toggle.
//   METADATA_KEYS_ALL            defaults to "true" = store every metadata key EXCEPT a
//       built-in blacklist (structural keys already stored as columns, plus high-churn
//       transport/runtime keys that change almost every message and would bloat
//       umh.attribute_<name> while defeating metadata de-dup). Set it to "false" to switch to
//       allowlist mode: only the keys in METADATA_KEYS are stored and the blacklist is NOT
//       applied (allowlist wins). Allowlisting a known high-churn key logs a one-time WARN.
//   MUST_CHANGE_metadata_key_list  in METADATA_KEYS below: the metadata keys to save
//       when METADATA_KEYS_ALL=false, e.g. ["serialNumber"]; use [] for none.
//   MUST_CHANGE_owner_password   the umh_owner role password. It appears in the
//       output dsn(s) below; set it to the SAME value you use when creating the
//       umh_owner role (see DATABASE SETUP below).
// ====================================================================
// DATABASE SETUP: create the umh_owner role once, before deploying. This is the
// only manual database-side step: the bridge logs in AS umh_owner, so it cannot
// bootstrap this role itself. Run as a Postgres superuser against the target
// database, using the SAME password you put in the dsn(s) below:
//
//     CREATE ROLE umh_owner WITH LOGIN PASSWORD 'MUST_CHANGE_owner_password';
//     GRANT CREATE, CONNECT ON DATABASE umh TO umh_owner;
//
// All historian objects live in a dedicated "umh" schema: on first deploy the bridge
// runs CREATE SCHEMA IF NOT EXISTS umh and creates every table, type and function
// there (umh.location, umh.tag, umh.topic, umh.value_<name>, umh.attribute_<name>,
// umh.get_topic_id(), ...). The CREATE-on-DATABASE grant above is what lets umh_owner
// create and own that schema, so NO grant on the public schema is required anymore.
// (The ltree extension still installs into public, the conventional shared location.)
// umh_owner is a dedicated non-superuser that owns the umh schema and holds the DDL
// rights this bridge needs. Never reuse the umh_owner dsn for queries (Grafana /
// SQL editors); add a read-only umh_reader before any human or Grafana access.
//
// PG13+ REQUIRED: a non-superuser (umh_owner) can only run CREATE EXTENSION ltree
// on PostgreSQL 13+, where ltree is a trusted extension.
// On an external PG <= 12, create the ltree extension once as a
// superuser before deploying.
// ====================================================================
// MIGRATING AN EXISTING public-SCHEMA DEPLOYMENT (skip this on a fresh install):
// earlier versions of this template created every object in the public schema;
// they now live in a dedicated umh schema. If you already ran the old version,
// MOVE your existing tables into umh ONCE, as a superuser, BEFORE redeploying --
// otherwise the bridge creates empty umh tables and your history stays orphaned
// in public. Replace <name> with your MUST_CHANGE_contract value:
//
//     CREATE SCHEMA IF NOT EXISTS umh;
//     ALTER TYPE  public.value_type        SET SCHEMA umh;
//     ALTER TABLE public.schema_migrations SET SCHEMA umh;
//     ALTER TABLE public.location          SET SCHEMA umh;
//     ALTER TABLE public.tag               SET SCHEMA umh;
//     ALTER TABLE public.topic             SET SCHEMA umh;
//     ALTER TABLE public.value_<name>      SET SCHEMA umh;
//     ALTER TABLE public.attribute_<name>  SET SCHEMA umh;
//
// Indexes, owned sequences, the datatype trigger and the TimescaleDB compression/
// retention policies move WITH their tables. The functions do NOT need moving:
// redeploying recreates get_topic_id(), to_ltree_path() and the guards inside umh.
// Once the bridge is healthy again you may drop the leftover public copies:
//     DROP FUNCTION IF EXISTS public.get_topic_id(text,text,text,text);
//     DROP FUNCTION IF EXISTS public.to_ltree_path(text);
//     DROP FUNCTION IF EXISTS public.tag_value_type_guard();
//     DROP FUNCTION IF EXISTS public.raise_pk_conflict(text, anyelement);
// ====================================================================
// DSN & PASSWORD: the output(s) below connect as umh_owner via:
//     postgres://umh_owner:MUST_CHANGE_owner_password@<host>:<port>/umh?sslmode=require
// Host and port come from the wizard's connection step and fill {{ .IP }} / {{ .PORT }}
// in the dsn. Replace MUST_CHANGE_owner_password in EVERY dsn with the umh_owner
// password above; the value writer and the metadata writer must use the same dsn.
//
// TLS / sslmode: the dsn defaults to sslmode=require, so the connection is encrypted.
// sslmode is ALWAYS set explicitly here, never left out: the postgres driver (lib/pq)
// does NOT enforce TLS on its own; an absent sslmode negotiates TLS only
// opportunistically and SILENTLY falls back to plaintext, which looks encrypted but
// is not. Choose the mode per target:
//   - External Postgres WITH TLS (the default): sslmode=require encrypts but does NOT
//       verify the server certificate (no protection against an active MITM). Use
//       sslmode=verify-full&sslrootcert=/certs/ca.crt to verify the server (add
//       sslcert=/sslkey= for mutual TLS). Cert paths are resolved INSIDE the
//       umh-core container, so the files must be mounted there.
//   - Bundled pgbouncer / TimescaleDB: it serves NO TLS, so sslmode=require is
//       REFUSED before any row is written. To use it, change sslmode=require to
//       sslmode=disable in BOTH dsns (value writer + metadata writer).
// ====================================================================
// NOTE: a throw here DOES fail the deploy; it is not just a warning. The processor
// drops the message without a nack and does not bump benthos's processor_error metric,
// but it logs at error level, and umh-core fails the bridge on any error-level log
// (IsBenthosLogsFine): the bridge never reaches active, so the deploy rolls back, and a
// running bridge flips to degraded. This works only because the source filter above is
// broad enough that this processor runs on UNS traffic; a contract-scoped filter would
// match nothing for an unset placeholder and the misconfig would deploy with no error.
//
// Validate the configured contract. The shipped placeholder "MUST_CHANGE_contract" has
// uppercase, so it fails the lowercase check and throws on the first message.
const CONTRACT = "cnc";
if (!/^[a-z0-9_]+$/.test(CONTRACT)) throw new Error('MUST_CHANGE_contract is unset or invalid: use a bare lowercase contract name (letters, digits, underscores), e.g. "pump".');
if (CONTRACT.startsWith("_")) throw new Error('MUST_CHANGE_contract must be set without the leading underscore ("pump", not "_pump")');
if (/_v\d+$/.test(CONTRACT)) throw new Error('MUST_CHANGE_contract must not carry a version suffix ("pump", not "pump_v1"); all versions of a contract share one table');
msg.meta._data_contract_name = (msg.meta.data_contract || "").replace(/_v\d+$/, "");
if (msg.meta._data_contract_name !== "_" + CONTRACT) return null;
if (!msg.meta.location_path || !msg.meta.tag_name) return null;
if (msg.meta.virtual_path && msg.meta.virtual_path.startsWith("Root.Objects.Server")) return null;
if (msg.payload == null || msg.payload.value == null || msg.payload.timestamp_ms == null) return null;

// Turn location_path into a valid ltree label string: split on ".", replace any
// character outside [A-Za-z0-9_] with "_", cap each label at 255 chars, drop empties.
// This MUST produce the same string as the SQL to_ltree_path() function below for the
// same input: get_topic_id() finds a row by comparing the stored path against
// to_ltree_path(query_path), so if the two differ the row is never matched and the
// query returns nothing even though the data is there.
// Keep the /u flag: an emoji or rare-CJK character is one code point but two UTF-16
// units, so without /u this regex replaces it with two "_" where Postgres uses one,
// and the strings no longer match. After replacement every character is ASCII, so JS
// .slice(0,255) and SQL left(...,255) cut at the same position.
msg.meta._ltree = msg.meta.location_path
  .split(".")
  .map(s => s.replace(/[^A-Za-z0-9_]/gu, "_").slice(0, 255))
  .filter(s => s.length > 0)
  .join(".");
// A non-empty location_path whose every segment normalizes away (e.g. "..." or "/")
// yields "" here but NULL from the SQL to_ltree_path(); drop it so the two sides agree
// and so physically distinct sources are not all merged into one empty-path row.
if (msg.meta._ltree === "") return null;

// Pick value_num vs value_text per row and record the tag's datatype in _value_type
// (numeric or text); the table's XOR CHECK enforces exactly one column is set, and
// the DB pins each tag to one value_type (see tag_value_type_guard). Non-finite
// numbers (NaN, +/-Infinity) are DROPPED rather than coerced to text: they are a bad
// numeric reading, NOT a datatype change, and would also corrupt DOUBLE aggregates.
const v = msg.payload.value;
const t = typeof v;
if (t === "boolean") {
  msg.meta._value_type = "numeric";
  msg.payload._value_num = v ? 1 : 0;
  msg.payload._value_text = null;
} else if (t === "number") {
  if (!Number.isFinite(v)) return null;
  msg.meta._value_type = "numeric";
  msg.payload._value_num = v;
  msg.payload._value_text = null;
} else {
  msg.meta._value_type = "text";
  msg.payload._value_num = null;
  msg.payload._value_text = t === "string" ? v : JSON.stringify(v);
}

if (msg.payload._value_text !== null && msg.payload._value_text.length > 8192) {
  msg.payload._value_text = msg.payload._value_text.slice(0, 8192);
  msg.meta._value_truncated = "true";
}

// A non-numeric / NaN / out-of-range timestamp_ms makes toISOString() throw. An uncaught
// throw does NOT nack or stall the stream: the nodered_js processor drops the message and
// bumps messages_errored (see benthos-umh nodered_js_plugin.go). We drop explicitly so the
// loss is intentional and uniform with the guards above, not dependent on a throw whose
// only signal is an error-log line plus the errored metric.
const tms = Number(msg.payload.timestamp_ms);
if (!Number.isFinite(tms)) return null;
const ts = new Date(tms);
if (isNaN(ts.getTime())) return null;
msg.meta._ts = ts.toISOString();

// Metadata is always archived. METADATA_KEYS_ALL defaults to "true": store every key
// EXCEPT the two built-in blacklists below. Set it to "false" for allowlist mode (store
// only METADATA_KEYS; the blacklists are NOT applied, so allowlist wins over blacklist).
const METADATA_KEYS_ALL = "true".toLowerCase() === "true";
const METADATA_KEYS     = ["MUST_CHANGE_metadata_key_list"];

// Structural keys: already stored as table columns / dimensions (location, tag, contract)
// or are transport routing, so never duplicate them into the attribute JSON.
const SKIP_STRUCTURAL = {
  location_path:1, data_contract:1, virtual_path:1, tag_name:1,
  data_contract_name:1, data_contract_version:1,
  data_contract_bypassed:1, data_contract_bypass_reason:1,
  umh_topic:1, bridged_by:1, origin:1, origin_id:1,
  kafka_topic:1, kafka_key:1, kafka_msg_key:1, kafka_partition:1, kafka_offset:1,
  kafka_timestamp_unix:1, kafka_lag:1, kafka_tombstone_message:1,
};
// High-churn keys: inherently per-message / transport-level values that change on nearly
// every message. Storing them turns attribute_<contract> into a per-message log and makes
// the metadata de-dup cache miss every time, so they are excluded by default. If you
// allowlist one anyway (METADATA_KEYS_ALL=false), it is stored and you get a one-time WARN.
// NOTE: modbus_tag_slaveid is deliberately NOT listed: it is fixed for a given stream, so
// it is stable per tag and safe to store -- do not add it here.
const HIGH_CHURN = {
  kafka_timestamp_ms:1,
  opcua_source_timestamp:1, opcua_server_timestamp:1, opcua_attr_statuscode:1,
  opcua_heartbeat_message:1,
  spb_sequence:1, spb_bdseq:1, spb_timestamp:1, spb_metric_index:1,
  spb_metrics_in_payload:1, spb_message_type:1, spb_state:1,
  event_type:1, umh_conversion_status:1, umh_conversion_error:1,
};

const keys = METADATA_KEYS_ALL
  ? Object.keys(msg.meta).filter(k => !k.startsWith("_") && !SKIP_STRUCTURAL[k] && !HIGH_CHURN[k])
  : METADATA_KEYS;

const md = {};
for (const k of keys) { if (msg.meta[k] != null) md[k] = msg.meta[k]; }

// One-time high-churn warning. If any key actually being stored is a known high-churn key
// (only reachable in allowlist mode, where the blacklist is intentionally bypassed), warn
// ONCE per process. WARN, never error: an error-level log fails the bridge health check
// (IsBenthosLogsFine) and rolls back the deploy. The cache flag is per-process (cleared on
// restart), so the warning re-emits once after each restart -- effectively "on startup".
// Placed before the de-dup short-circuit below so an unchanged fingerprint can't mask it.
const churn = Object.keys(md).filter(k => HIGH_CHURN[k]);
if (churn.length > 0 && !cache.exists("_churn_warned")) {
  cache.set("_churn_warned", "1");
  console.warn("TimescaleDB historian: archiving high-churn metadata key(s) [" + churn.join(", ") +
    "] into umh.attribute_MUST_CHANGE_contract. These change on nearly every message, so the attribute " +
    "table will grow per-message and metadata de-duplication will not help. Remove them from " +
    "METADATA_KEYS unless you specifically need them.");
}

const fp = JSON.stringify(md);
const cacheKey = "md:" + msg.meta._data_contract_name + ":" + msg.meta.location_path + ":" +
                 (msg.meta.virtual_path || "") + ":" + msg.meta.tag_name;
if (cache.exists(cacheKey) && cache.get(cacheKey) === fp) {
  msg.meta._emit_meta = "false";
} else {
  cache.set(cacheKey, fp);
  msg.meta._metadata_json = fp;
  msg.meta._emit_meta = "true";
}
return msg;