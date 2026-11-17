"""Engine units, not experimental machine/pool quotas (64-bit openGauss)."""

MIB = 1024 ** 2
# pg_settings.max_val for work_mem is MAX_KILOBYTES = INT_MAX on this
# 64-bit build. Session SQL is expressed in whole MiB, never rounded past it.
MAX_WORK_MEM_MIB = (2 ** 31 - 1) // 1024


def valid_work_mem_mib(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= MAX_WORK_MEM_MIB)
