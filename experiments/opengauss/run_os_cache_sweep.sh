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
hog_list_mb="0,5000,10000,15000,20000"
per_step_walltime_s="0"
warmup_s="60"
min_mem_available_kb="10485760"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) mode="$2"; shift 2;;
    --duration) duration_s="$2"; shift 2;;
    --warmup) warmup_s="$2"; shift 2;;
    --min-mem-available-kb) min_mem_available_kb="$2"; shift 2;;
    --workload) workload="$2"; shift 2;;
    --db) db_name="$2"; shift 2;;
    --host) pg_host="$2"; shift 2;;
    --port) pg_port="$2"; shift 2;;
    --user) pg_user="$2"; shift 2;;
    --password) pg_password="$2"; shift 2;;
    --data-dir) data_dir="$2"; shift 2;;
    --disk-method) disk_method="$2"; shift 2;;
    --hog-mb) hog_list_mb="$2"; shift 2;;
    --walltime) per_step_walltime_s="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

export LD_LIBRARY_PATH=/tmp/og_libpq_510/lib:${LD_LIBRARY_PATH:-}
export OG_CONNINFO="host=${pg_host} port=${pg_port} dbname=${db_name} user=${pg_user} password=${pg_password} connect_timeout=3"

now_ts="$(date +%Y%m%d_%H%M%S)"
out_dir="/root/Huawei2/os_cache_sweep_${now_ts}"
mkdir -p "${out_dir}"

lock_file="/tmp/huawei2_os_cache_sweep.lock"
exec 9>"${lock_file}"
if ! flock -n 9; then
  echo "another sweep is running (lock: ${lock_file})" >&2
  exit 1
fi

log() {
  local msg="$*"
  local ts
  ts="$(date -Is)"
  printf "%s %s\n" "${ts}" "${msg}" >&2
  printf "%s %s\n" "${ts}" "${msg}" >> "${out_dir}/progress.log"
}

preflight() {
  if [ ! -x /tmp/ogsql ]; then
    echo "/tmp/ogsql not found or not executable" >&2
    exit 1
  fi
  if ! /tmp/ogsql "select 1;" >/dev/null 2>&1; then
    echo "/tmp/ogsql preflight failed (check LD_LIBRARY_PATH and og_libpq contents)" >&2
    exit 1
  fi

  local memtotal_kb
  memtotal_kb="$(awk '/^MemTotal:/ {print $2+0}' /proc/meminfo)"
  if [ "${memtotal_kb}" -gt 0 ] && [ "${min_mem_available_kb}" -gt "${memtotal_kb}" ]; then
    echo "min_mem_available_kb=${min_mem_available_kb} > MemTotal=${memtotal_kb}KB" >&2
    exit 1
  fi
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
      log "step running... elapsed=$((now-start_ts))s (limit=${limit_s}s)"
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

start_memhog() {
  local mb="$1"
  if [ "${mb}" -le 0 ]; then
    echo "0"
    return
  fi
  local hog_log="${out_dir}/memhog_${mb}MB.log"
  nice -n 19 python3 - <<PY > "${hog_log}" 2>&1 &
import time
chunks=[]
target_mb=int("${mb}")
min_avail_kb=int("${min_mem_available_kb}")
def mem_available_kb():
    try:
        with open("/proc/meminfo","r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except Exception:
        return -1
    return -1

print(f"target_mb={target_mb} min_mem_available_kb={min_avail_kb}", flush=True)
for i in range(target_mb):
    chunks.append(bytearray(1024*1024))
    if (i+1) % 128 == 0:
        avail=mem_available_kb()
        print(f"allocated_mb={i+1} mem_available_kb={avail}", flush=True)
        if avail > 0 and avail < min_avail_kb:
            print(f"stop_allocating: mem_available_kb={avail} < min_mem_available_kb={min_avail_kb}", flush=True)
            break
print(f"final_allocated_mb={len(chunks)}", flush=True)
time.sleep(3600)
PY
  echo "$!"
}

stop_memhog() {
  local pid="$1"
  if [ "${pid}" = "0" ]; then
    return
  fi
  kill "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
}

if [ "${per_step_walltime_s}" -le 0 ]; then
  per_step_walltime_s="$((warmup_s + duration_s + 600))"
fi

IFS=',' read -r -a hogs <<< "${hog_list_mb}"

current_hog_pid="0"
summary_closed="0"
cleanup() {
  stop_memhog "${current_hog_pid}"
  if [ "${summary_closed}" != "1" ] && [ -f "${out_dir}/summary.json" ]; then
    printf "%s\n" "]" >> "${out_dir}/summary.json" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

preflight

log "start sweep: mode=${mode} duration_s=${duration_s} workload=${workload} hog_list_mb=${hog_list_mb} per_step_walltime_s=${per_step_walltime_s} (out_dir=${out_dir})"
log "warmup_s=${warmup_s} (each step runs warmup then measure; only measure is written into summary.json)"
log "min_mem_available_kb=${min_mem_available_kb} (skip step if MemAvailable below this threshold after memhog start)"

printf "%s\n" "[" > "${out_dir}/summary.json"
first=1
idx=0
total="${#hogs[@]}"
for hog_mb in "${hogs[@]}"; do
  idx="$((idx+1))"
  hog_mb="$(echo "${hog_mb}" | tr -d ' ')"
  log "step ${idx}/${total}: start memhog hog_mb=${hog_mb}"
  current_hog_pid="$(start_memhog "${hog_mb}")"
  if [ "${current_hog_pid}" != "0" ]; then
    log "step ${idx}/${total}: memhog pid=${current_hog_pid} (tail -f ${out_dir}/memhog_${hog_mb}MB.log)"
  fi
  sleep 2
  avail_kb="0"
  if [ -r /proc/meminfo ]; then
    avail_kb="$(awk '/^MemAvailable:/ {print $2+0}' /proc/meminfo)"
    log "step ${idx}/${total}: MemAvailable=${avail_kb}KB"
  fi

  if [ "${avail_kb}" -gt 0 ] && [ "${avail_kb}" -lt "${min_mem_available_kb}" ]; then
    log "step ${idx}/${total}: skip (MemAvailable=${avail_kb}KB < ${min_mem_available_kb}KB); stop memhog pid=${current_hog_pid}"
    if [ "${first}" -ne 1 ]; then
      printf "%s\n" "," >> "${out_dir}/summary.json"
    fi
    first=0
    printf "%s" "$(printf '{\"hog_mb\":%s,\"error\":\"skip_low_memavailable\",\"mem_available_kb\":%s}' "${hog_mb}" "${avail_kb}")" >> "${out_dir}/summary.json"
    stop_memhog "${current_hog_pid}"
    current_hog_pid="0"
    sleep 1
    continue
  fi
  if [ "${first}" -ne 1 ]; then
    printf "%s\n" "," >> "${out_dir}/summary.json"
  fi
  first=0
  log "step ${idx}/${total}: warmup start warmup_s=${warmup_s}"
  set +e
  run_with_deadline "${per_step_walltime_s}" bash /root/Huawei2/run_hit_ratio_experiment.sh \
    --mode "${mode}" \
    --duration "${warmup_s}" \
    --workload "${workload}" \
    --db "${db_name}" \
    --host "${pg_host}" \
    --port "${pg_port}" \
    --user "${pg_user}" \
    --password "${pg_password}" \
    --data-dir "${data_dir}" \
    --disk-method "${disk_method}" \
    --walltime "${per_step_walltime_s}" \
    --label "phase=warmup,hog_mb=${hog_mb},step=${idx}/${total}" >/dev/null
  warmup_rc="$?"
  set -e
  log "step ${idx}/${total}: warmup done rc=${warmup_rc}"

  log "step ${idx}/${total}: measure start duration_s=${duration_s}"
  set +e
  run_json="$(
    run_with_deadline "${per_step_walltime_s}" bash /root/Huawei2/run_hit_ratio_experiment.sh \
      --mode "${mode}" \
      --duration "${duration_s}" \
      --workload "${workload}" \
      --db "${db_name}" \
      --host "${pg_host}" \
      --port "${pg_port}" \
      --user "${pg_user}" \
      --password "${pg_password}" \
      --data-dir "${data_dir}" \
      --disk-method "${disk_method}" \
      --walltime "${per_step_walltime_s}" \
      --label "phase=measure,hog_mb=${hog_mb},step=${idx}/${total}"
  )"
  rc="$?"
  set -e
  if [ -z "${run_json}" ]; then
    rc=1
    run_json="$(printf '{\"hog_mb\":%s,\"error\":\"empty_run_json\",\"rc\":%s}' "${hog_mb}" "${rc}")"
  fi
  if [ "${rc}" -ne 0 ]; then
    run_json="$(printf '{\"hog_mb\":%s,\"error\":\"timeout_or_failed\",\"rc\":%s}' "${hog_mb}" "${rc}")"
  fi
  memhog_actual_mb="0"
  if [ "${hog_mb}" != "0" ] && [ -f "${out_dir}/memhog_${hog_mb}MB.log" ]; then
    memhog_actual_mb="$(awk -F= '/^final_allocated_mb=/{v=$2} END{print v+0}' "${out_dir}/memhog_${hog_mb}MB.log" 2>/dev/null || echo 0)"
  fi
  run_json="$(
    printf "%s" "${run_json}" | python3 - "${hog_mb}" "${memhog_actual_mb}" <<'PY'
import json
import sys
hog_mb=int(sys.argv[1])
actual=int(sys.argv[2])
raw=sys.stdin.read().strip()
try:
    obj=json.loads(raw) if raw else {}
except Exception:
    obj={"error":"invalid_run_json"}
obj["memhog_target_mb"]=hog_mb
obj["memhog_actual_mb"]=actual
print(json.dumps(obj, ensure_ascii=False))
PY
  )"
  printf "%s" "${run_json}" >> "${out_dir}/summary.json"
  log "step ${idx}/${total}: workload done rc=${rc}; stop memhog pid=${current_hog_pid}"
  stop_memhog "${current_hog_pid}"
  current_hog_pid="0"
  sleep 1
done
printf "%s\n" "]" >> "${out_dir}/summary.json"
summary_closed="1"

log "sweep finished (summary -> ${out_dir}/summary.json)"
echo "${out_dir}"
