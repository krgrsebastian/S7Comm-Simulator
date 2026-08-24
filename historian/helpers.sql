-- UMH historian helper functions.
--
-- The historian bridge's init_statement creates these, but a database that was
-- set up by an older bridge (or whose init aborted part-way) can be missing
-- them. Nothing in grafana/andon-cnc.json needs them any more — the panels join
-- location/tag/topic/value_<contract> directly — but they are convenient, and
-- umh.to_ltree_path() is the canonical path normaliser you want when comparing
-- a hand-written location path against a stored one.
--
-- Safe to run against a live historian: every statement is CREATE OR REPLACE,
-- no table is touched, and no data is read or written. Run it against the
-- historian database (\c umh), not against `postgres`.
--
--   docker exec -i standard-timescaledb-1 psql -U postgres -d umh < helpers.sql
--
-- Check afterwards with:  \df umh.*

CREATE OR REPLACE FUNCTION umh.tag_value_type_guard()
RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
  IF NEW.value_type IS DISTINCT FROM OLD.value_type THEN
    RAISE EXCEPTION 'tag datatype changed for virtual_path=% name=% contract=%: stored % but received % (one tag must always produce the same datatype)',
      OLD.virtual_path, OLD.name, OLD.data_contract_name, OLD.value_type, NEW.value_type;
  END IF;
  RETURN NEW;
END $guard$;

CREATE OR REPLACE FUNCTION umh.raise_pk_conflict(p_msg text, p_ret anyelement)
RETURNS anyelement LANGUAGE plpgsql AS $rpc$
BEGIN
  RAISE EXCEPTION '%', p_msg;
END $rpc$;

CREATE OR REPLACE FUNCTION umh.to_ltree_path(p_location_path text)
RETURNS ltree
LANGUAGE sql IMMUTABLE
AS $ltree$
  SELECT string_agg(q.lbl, '.' ORDER BY q.ord)::ltree
    FROM (SELECT left(regexp_replace(s.seg, '[^A-Za-z0-9_]', '_', 'g'), 255) AS lbl, s.ord
            FROM unnest(string_to_array(p_location_path, '.')) WITH ORDINALITY AS s(seg, ord)) q
   WHERE length(q.lbl) > 0;
$ltree$;

CREATE OR REPLACE FUNCTION umh.get_topic_id(
  p_location_path text,
  p_virtual_path  text,
  p_data_contract text,
  p_tag_name      text
)
RETURNS bigint
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = umh, public, pg_temp
AS $fn$
  SELECT t.topic_id
    FROM umh.location l
    JOIN umh.tag   g ON g.virtual_path = p_virtual_path
                AND g.name = p_tag_name
                AND g.data_contract_name =
                    '_' || regexp_replace(regexp_replace(p_data_contract, '_v\d+$', ''), '^_', '')
    JOIN umh.topic t ON t.location_id = l.location_id AND t.tag_id = g.tag_id
   WHERE l.path = umh.to_ltree_path(p_location_path);
$fn$;