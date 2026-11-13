"""Database SET domain, distinct from global budgets and operator grants."""
import math

from .memory_units import MAX_WORK_MEM_MIB

SCHEMA = "jmt.database-allowed-work-mem-domain/v1"
SETTINGS_SQL = """SELECT json_agg(t) FROM
    (SELECT name,setting,min_val,max_val,unit,context,source
     FROM pg_settings WHERE name IN
     ('work_mem','query_mem','query_max_mem','max_process_memory',
      'shared_buffers','shared_buffers_target','cstore_buffers',
      'enable_memory_limit','disable_memory_protect')
     ORDER BY name)t"""


def derive_work_mem_domain(settings):
    """Whole-MiB probes must stay within the actual engine's GUC range.

    query_mem/query_max_mem are recorded separately. They are not silently
    interpreted as work_mem's SQL validator, nor as the sum of AP grants.
    """
    rows = {r["name"]: r for r in settings}
    if len(rows) != len(settings) or "work_mem" not in rows:
        raise ValueError("unique database settings with work_mem required")
    wm = rows["work_mem"]
    if wm["unit"] != "kB":
        raise ValueError("unsupported database work_mem unit")
    try:
        low, high = int(wm["min_val"]), int(wm["max_val"])
    except (TypeError, ValueError):
        raise ValueError("integer database work_mem limits required")
    minimum, maximum = max(1, math.ceil(low / 1024)), min(
        MAX_WORK_MEM_MIB, high // 1024)
    if low < 0 or not 1 <= minimum <= maximum:
        raise ValueError("database work_mem limits have no supported whole-MiB value")
    return {
        "schema": SCHEMA, "raw_pg_settings": settings,
        "minimum_mib": minimum, "maximum_mib": maximum,
        "limit_source": "pg_settings.work_mem.min_val/max_val, intersect adapter integer domain",
        "query_memory_settings": {
            name: rows[name] for name in ("query_mem", "query_max_mem") if name in rows},
        "database_global_settings": {
            name: rows[name] for name in ("max_process_memory", "enable_memory_limit",
                                         "disable_memory_protect") if name in rows},
        "TP_reserve_or_native_dynamic_remainder_used_as_WM_GUC_limit": False,
        "SET_acceptance_is_not_memory_allocation_or_operator_grant": True,
        "large_SET_does_not_authorize_host_overcommit": True,
    }


def validate_candidate_work_mem(wm_mib, domain):
    if (isinstance(wm_mib, bool) or not isinstance(wm_mib, int)
            or not domain["minimum_mib"] <= wm_mib <= domain["maximum_mib"]):
        raise ValueError("WM candidate outside database-allowed work_mem SET range")


def collect_work_mem_domain(client, database):
    return derive_work_mem_domain(client.json(database, SETTINGS_SQL))
