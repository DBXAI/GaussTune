#!/usr/bin/env bash
set -euo pipefail

mode="warm"
duration_s="60"
workload="tp200"
db_name="postgres"
pg_host="127.0.0.1"
pg_port="5432"
pg_user="tpuser"
pg_password="openGauss@123"
data_dir="/opt/openGauss/data"
disk_method="auto"
walltime_s="0"
label=""

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) mode="$2"; shift 2;;
    --duration) duration_s="$2"; shift 2;;
    --workload) workload="$2"; shift 2;;
    --db) db_name="$2"; shift 2;;
    --host) pg_host="$2"; shift 2;;
    --port) pg_port="$2"; shift 2;;
    --user) pg_user="$2"; shift 2;;
    --password) pg_password="$2"; shift 2;;
    --data-dir) data_dir="$2"; shift 2;;
    --disk-method) disk_method="$2"; shift 2;;
    --walltime) walltime_s="$2"; shift 2;;
    --label) label="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

export LD_LIBRARY_PATH=/tmp/og_libpq_510/lib:${LD_LIBRARY_PATH:-}
export OG_CONNINFO="host=${pg_host} port=${pg_port} dbname=${db_name} user=${pg_user} password=${pg_password} connect_timeout=3"

now_ts="$(date +%Y%m%d_%H%M%S)"
out_dir="/root/Huawei2/results_${now_ts}"
mkdir -p "${out_dir}"

log() {
  local msg="$*"
  local ts
  ts="$(date -Is)"
  printf "%s %s\n" "${ts}" "${msg}" >&2
  printf "%s %s\n" "${ts}" "${msg}" >> "${out_dir}/progress.log"
}

sql_scalar() {
  local query="$1"
  local out rc
  set +e
  out="$(/tmp/ogsql "${query}" 2>/dev/null)"
  rc="$?"
  set -e
  if [ "${rc}" -ne 0 ]; then
    printf "%s" ""
    return 0
  fi
  printf "%s" "${out}" | tail -n 1 | tr -d '\r'
}

as_int() {
  local v="${1:-}"
  if [[ "${v}" =~ ^-?[0-9]+$ ]]; then
    printf "%s" "${v}"
    return 0
  fi
  printf "%s" "0"
}

run_with_deadline() {
  local limit_s="$1"
  shift
  if [ "${limit_s}" -le 0 ]; then
    "$@"
    return
  fi

  local start_ts
  start_ts="$(date +%s)"
  "$@" &
  local pid="$!"
  local last_beat="${start_ts}"
  while kill -0 "${pid}" 2>/dev/null; do
    local now
    now="$(date +%s)"
    if [ "$((now-last_beat))" -ge 30 ]; then
      log "running workload... elapsed=$((now-start_ts))s (limit=${limit_s}s) out_dir=${out_dir}"
      last_beat="${now}"
    fi
    if [ "$((now-start_ts))" -ge "${limit_s}" ]; then
      kill "${pid}" 2>/dev/null || true
      sleep 2
      kill -9 "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
      return 124
    fi
    sleep 1
  done
  wait "${pid}"
}

workload_cmd=""
case "${workload}" in
  tp200)
    workload_cmd="sysbench --db-driver=pgsql --pgsql-host=${pg_host} --pgsql-port=${pg_port} --pgsql-user=${pg_user} --pgsql-password=${pg_password} --pgsql-db=${db_name} --tables=20 --table-size=8000000 --threads=200 --time=${duration_s} --report-interval=10 --db-ps-mode=disable --delete_inserts=0 oltp_read_write run"
    ;;
  tp200_ro)
    workload_cmd="sysbench --db-driver=pgsql --pgsql-host=${pg_host} --pgsql-port=${pg_port} --pgsql-user=${pg_user} --pgsql-password=${pg_password} --pgsql-db=${db_name} --tables=20 --table-size=8000000 --threads=200 --time=${duration_s} --report-interval=10 --db-ps-mode=disable oltp_read_only run"
    ;;
  *)
    echo "unknown workload: ${workload}" >&2
    exit 2
    ;;
esac

get_mem_available_kb() {
  awk '/^MemAvailable:/ {print $2+0}' /proc/meminfo
}

get_gaussdb_pids() {
  ps -eo pid,cmd | awk 'index($0,"/opt/openGauss/bin/gaussdb")>0 {print $1}' | tr '\n' ' '
}

read_proc_read_bytes() {
  local pids="$1"
  local total=0
  local ok=1
  for pid in ${pids}; do
    local v
    v="$(awk '/^read_bytes:/ {printf("%.0f\n", $2)}' "/proc/${pid}/io" 2>/dev/null || true)"
    if [ -z "${v}" ]; then
      ok=0
      continue
    fi
    total="$((total+v))"
  done
  printf "%s %s\n" "${ok}" "${total}"
}

disk_source="$(findmnt -no SOURCE -T "${data_dir}" 2>/dev/null || true)"
if [ -z "${disk_source}" ]; then
  disk_source="$(df -P "${data_dir}" | awk 'NR==2 {print $1}')"
fi
disk_dev="${disk_source##*/}"

get_disk_read_sectors() {
  awk -v d="${disk_dev}" '$3==d {print $6+0}' /proc/diskstats
}

if ! awk -v d="${disk_dev}" '$3==d {found=1} END{exit found?0:1}' /proc/diskstats; then
  disk_dev=""
fi

if [ "${mode}" = "cold" ]; then
  sync
  echo 3 > /proc/sys/vm/drop_caches
fi

if [ "${walltime_s}" -le 0 ]; then
  walltime_s="$((duration_s + 180))"
fi

log "start run: mode=${mode} duration_s=${duration_s} walltime_s=${walltime_s} workload=${workload} db=${db_name} host=${pg_host}:${pg_port} disk_method=${disk_method}"
if [ -n "${label}" ]; then
  log "label=${label}"
fi

sb_conf="$(sql_scalar "show shared_buffers;")"

gaussdb_pids="$(get_gaussdb_pids)"

hit1="$(as_int "$(sql_scalar "select blks_hit from pg_stat_database where datname='${db_name}';")")"
read1="$(as_int "$(sql_scalar "select blks_read from pg_stat_database where datname='${db_name}';")")"
mem_avail1_kb="$(get_mem_available_kb)"

proc_ok1="0"
proc_read_bytes1="0"
if [ "${disk_method}" = "auto" ] || [ "${disk_method}" = "proc" ]; then
  read -r proc_ok1 proc_read_bytes1 < <(read_proc_read_bytes "${gaussdb_pids}")
fi

disk_sec1="0"
if [ -n "${disk_dev}" ]; then
  disk_sec1="$(get_disk_read_sectors)"
fi

start_epoch="$(date +%s)"
start_iso="$(date -Is)"

set +e
{
  echo "start_iso=${start_iso}"
  echo "duration_s=${duration_s}"
  echo "walltime_s=${walltime_s}"
  echo "workload=${workload}"
  echo "label=${label}"
  echo "cmd=${workload_cmd}"
} > "${out_dir}/meta.log"

log "workload started (sysbench output -> ${out_dir}/sysbench.log)"
run_with_deadline "${walltime_s}" bash -lc "set -o pipefail; { ${workload_cmd}; } 2>&1 | tee -a '${out_dir}/sysbench.log' >&2"
workload_rc="$?"
set -e

end_epoch="$(date +%s)"
end_iso="$(date -Is)"
elapsed_s="$((end_epoch-start_epoch))"
{
  echo "end_iso=${end_iso}"
  echo "elapsed_s=${elapsed_s}"
  echo "workload_rc=${workload_rc}"
} >> "${out_dir}/meta.log"
log "workload finished rc=${workload_rc} elapsed_s=${elapsed_s}"

hit2="$(as_int "$(sql_scalar "select blks_hit from pg_stat_database where datname='${db_name}';")")"
read2="$(as_int "$(sql_scalar "select blks_read from pg_stat_database where datname='${db_name}';")")"
mem_avail2_kb="$(get_mem_available_kb)"

proc_ok2="0"
proc_read_bytes2="0"
if [ "${disk_method}" = "auto" ] || [ "${disk_method}" = "proc" ]; then
  read -r proc_ok2 proc_read_bytes2 < <(read_proc_read_bytes "${gaussdb_pids}")
fi

disk_sec2="0"
if [ -n "${disk_dev}" ]; then
  disk_sec2="$(get_disk_read_sectors)"
fi

delta_hit="$((hit2-hit1))"
delta_read="$((read2-read1))"
denom="$((delta_hit+delta_read))"
sb_hit_ratio="0"
if [ "${denom}" -gt 0 ]; then
  sb_hit_ratio="$(awk -v h="${delta_hit}" -v d="${denom}" 'BEGIN{printf("%.6f", h/d)}')"
fi

logical_read_bytes="$(awk -v r="${delta_read}" 'BEGIN{printf("%.0f", r*8192)}')"
disk_read_bytes="0"
disk_read_source="none"
proc_disk_ok="0"
if [ "${disk_method}" = "proc" ] || [ "${disk_method}" = "auto" ]; then
  if [ "${proc_ok1}" = "1" ] && [ "${proc_ok2}" = "1" ]; then
    if [ "${proc_read_bytes2}" -ge "${proc_read_bytes1}" ]; then
      disk_read_bytes="$((proc_read_bytes2-proc_read_bytes1))"
      disk_read_source="proc_io"
      proc_disk_ok="1"
    fi
  fi
fi
if [ "${disk_read_source}" = "none" ]; then
  if [ "${disk_method}" != "proc" ] && [ -n "${disk_dev}" ]; then
    disk_read_bytes="$(awk -v a="${disk_sec1}" -v b="${disk_sec2}" 'BEGIN{printf("%.0f", (b-a)*512)}')"
    disk_read_source="diskstats"
  fi
fi

os_hit_ratio="0"
if [ "${logical_read_bytes}" -gt 0 ] && [ "${disk_read_source}" != "none" ]; then
  os_hit_ratio="$(awk -v l="${logical_read_bytes}" -v d="${disk_read_bytes}" 'BEGIN{v=1-(d/l); if(v<0)v=0; if(v>1)v=1; printf("%.6f", v)}')"
fi

tp_summary="$(awk '/transactions:/{print; exit}' "${out_dir}/sysbench.log" | tr -d '\r')"

printf "%s\n" "{" \
  "\"timestamp\":\"${now_ts}\"," \
  "\"mode\":\"${mode}\"," \
  "\"workload\":\"${workload}\"," \
  "\"db\":\"${db_name}\"," \
  "\"shared_buffers\":\"${sb_conf}\"," \
  "\"data_dir\":\"${data_dir}\"," \
  "\"gaussdb_pids\":\"${gaussdb_pids}\"," \
  "\"label\":\"${label//\"/\\\"}\"," \
  "\"delta_blks_hit\":${delta_hit}," \
  "\"delta_blks_read\":${delta_read}," \
  "\"sb_hit_ratio\":${sb_hit_ratio}," \
  "\"mem_available_kb_start\":${mem_avail1_kb}," \
  "\"mem_available_kb_end\":${mem_avail2_kb}," \
  "\"disk_dev\":\"${disk_dev}\"," \
  "\"disk_read_source\":\"${disk_read_source}\"," \
  "\"proc_io_available\":${proc_disk_ok}," \
  "\"disk_read_bytes\":${disk_read_bytes}," \
  "\"logical_read_bytes\":${logical_read_bytes}," \
  "\"os_cache_hit_ratio\":${os_hit_ratio}," \
  "\"start_iso\":\"${start_iso}\"," \
  "\"end_iso\":\"${end_iso}\"," \
  "\"elapsed_s\":${elapsed_s}," \
  "\"sysbench_transactions_line\":\"${tp_summary//\"/\\\"}\"," \
  "\"workload_rc\":${workload_rc}" \
  "}" > "${out_dir}/run.json"

log "done (results -> ${out_dir}/run.json)"
cat "${out_dir}/run.json"
