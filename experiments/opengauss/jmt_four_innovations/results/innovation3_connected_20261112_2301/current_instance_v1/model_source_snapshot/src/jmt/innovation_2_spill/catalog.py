"""Bounded metadata-only openGauss catalog snapshot; no fact-table SELECT."""
import hashlib
import json
from datetime import datetime, timezone


CATALOG_SQL = """
SELECT row_to_json(s) FROM (
 SELECT version() AS version,current_database() AS database,
 current_setting('sql_compatibility') AS sql_compatibility,
 (SELECT coalesce(json_agg(t),'[]'::json) FROM (
   SELECT c.oid,c.relname,c.reltuples,c.relpages,c.relkind FROM pg_class c
   JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='public' AND c.relkind IN ('r','i') ORDER BY c.relname
 ) t) AS relations,
 (SELECT coalesce(json_agg(t),'[]'::json) FROM (
   SELECT s.schemaname,s.tablename,s.attname,s.null_frac,s.avg_width,s.n_distinct,
          s.most_common_freqs AS mcv_freqs,a.attnotnull,
          ty.typname,ty.typlen,ty.typbyval,ty.typalign
   FROM pg_stats s JOIN pg_namespace n ON n.nspname=s.schemaname
   JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=s.tablename
   JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname=s.attname
   JOIN pg_type ty ON ty.oid=a.atttypid
   WHERE s.schemaname='public' AND c.relkind='r' AND a.attnum>0
   ORDER BY s.tablename,s.attname
 ) t) AS columns,
 (SELECT coalesce(json_agg(t),'[]'::json) FROM (
   SELECT n.nspname AS schema,c.relname AS relation,co.conname AS constraint_name,
          array_agg(a.attname ORDER BY a.attnum) AS columns
   FROM pg_constraint co JOIN pg_class c ON c.oid=co.conrelid
   JOIN pg_namespace n ON n.oid=c.relnamespace
   JOIN pg_attribute a ON a.attrelid=co.conrelid AND a.attnum=ANY(co.conkey)
   WHERE n.nspname='public' AND co.contype IN ('p','u')
   GROUP BY n.nspname,c.relname,co.conname HAVING bool_and(a.attnotnull)
 ) t) AS nonnull_unique_keys,
 (SELECT json_agg(t) FROM (
   SELECT name,setting,unit FROM pg_settings
   WHERE name IN ('shared_buffers','work_mem','enable_vector_engine','query_dop',
                 'enable_hashagg','sql_compatibility','enable_hashjoin')
   ORDER BY name
 ) t) AS settings
) s
"""


def collect_catalog(client, database):
    result = client.json(database, CATALOG_SQL)
    for row in result["columns"]:
        freqs = row.get("mcv_freqs")
        if isinstance(freqs, str):
            freqs = [x for x in freqs.strip("{}").split(",") if x]
        if freqs is not None:
            # openGauss JSON encodes real[] elements as strings on this build.
            # Normalise arrays as well as the legacy PostgreSQL array string.
            freqs = [float(x) for x in freqs] if len(freqs) <= 256 else None
        row["most_common_freqs"] = freqs
        row["mcv_freqs"] = freqs
    for row in result["nonnull_unique_keys"]:
        if isinstance(row["columns"], str):
            row["columns"] = row["columns"].strip("{}").split(",")
    result["collected_at"] = datetime.now(timezone.utc).isoformat()
    result["catalog_snapshot_fingerprint"] = hashlib.sha256(
        json.dumps({k: v for k, v in result.items() if k != "collected_at"},
                   sort_keys=True).encode()
    ).hexdigest()
    result["qualification"] = (
        "Public-schema catalog-only snapshot; no values/samples/fact scans. "
        "Statistics are planner estimates, not measured target cardinalities."
    )
    return result
