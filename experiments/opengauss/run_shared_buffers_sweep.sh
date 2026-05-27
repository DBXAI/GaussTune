#!/usr/bin/env bash
set -euo pipefail

buffer_list="128MB,256MB,512MB,1GB,2GB"
mode="warm"
duration_s="60"
warmup_s="60"
workload="tp200"
db_name="postgres"
pg_host="127.0.0.1"
pg_port="5432"
pg_user="tpuser"
pg_password="openGauss@123"
data_dir="/opt/openGauss/data"
gauss_home="/opt/openGauss"
os_user="omm"
disk_method="auto"
per_step_walltime_s="0"
restart_cmd=""
out_base="/root/Huawei2"
hog_mb="0"
target_mem_available_kb="0"
min_mem_available_kb="10485760"
subset="all"
auto_min_mb="0"
auto_max_mb="0"
auto_count="0"
auto_scale="log"
auto_round_mb="16"
fill_method="memhog"
ap_max_workers="4"
ap_tables="20"
ap_timeout_s="300"

while [ $# -gt 0 ]; do
  case "$1" in
    --buffers) buffer_list="$2"; shift 2;;
    --mode) mode="$2"; shift 2;;
    --duration) duration_s="$2"; shift 2;;
    --warmup) warmup_s="$2"; shift 2;;
    --workload) workload="$2"; shift 2;;
    --db) db_name="$2"; shift 2;;
    --host) pg_host="$2"; shift 2;;
    --port) pg_port="$2"; shift 2;;
    --user) pg_user="$2"; shift 2;;
    --password) pg_password="$2"; shift 2;;
    --data-dir) data_dir="$2"; shift 2;;
    --gauss-home) gauss_home="$2"; shift 2;;
    --os-user) os_user="$2"; shift 2;;
    --disk-method) disk_method="$2"; shift 2;;
    --walltime) per_step_walltime_s="$2"; shift 2;;
    --restart-cmd) restart_cmd="$2"; shift 2;;
    --out-base) out_base="$2"; shift 2;;
    --hog-mb) hog_mb="$2"; shift 2;;
    --target-mem-available-kb) target_mem_available_kb="$2"; shift 2;;
    --min-mem-available-kb) min_mem_available_kb="$2"; shift 2;;
    --subset) subset="$2"; shift 2;;
    --fill-method) fill_method="$2"; shift 2;;
    --ap-max-workers) ap_max_workers="$2"; shift 2;;
    --ap-tables) ap_tables="$2"; shift 2;;
    --ap-timeout) ap_timeout_s="$2"; shift 2;;
    --auto-min-mb) auto_min_mb="$2"; shift 2;;
    --auto-max-mb) auto_max_mb="$2"; shift 2;;
    --auto-count) auto_count="$2"; shift 2;;
    --auto-scale) auto_scale="$2"; shift 2;;
    --auto-round-mb) auto_round_mb="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

export LD_LIBRARY_PATH=/tmp/og_libpq_510/lib:${LD_LIBRARY_PATH:-}
export OG_CONNINFO="host=${pg_host} port=${pg_port} dbname=${db_name} user=${pg_user} password=${pg_password} connect_timeout=3"

now_ts="$(date +%Y%m%d_%H%M%S)"
out_dir="${out_base}/shared_buffers_sweep_${now_ts}"
mkdir -p "${out_base}"
mkdir -p "${out_dir}"

lock_file="/tmp/huawei2_shared_buffers_sweep.lock"
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

get_mem_available_kb() {
  awk '/^MemAvailable:/ {print $2+0}' /proc/meminfo
}

kill_tree() {
  local pid="$1"
  local kids
  kids="$(ps -o pid= --ppid "${pid}" 2>/dev/null || true)"
  for k in ${kids}; do
    kill_tree "${k}"
  done
  kill "${pid}" 2>/dev/null || true
}

start_apfill() {
  local target_avail_kb="$1"
  local min_avail_kb="$2"
  local max_workers="$3"
  local timeout_s="$4"
  local tables="$5"
  local step_tag="$6"
  local pids=()

  if [ "${target_avail_kb}" -le 0 ]; then
    printf "%s" ""
    return 0
  fi

  local start_ts now avail workers
  start_ts="$(date +%s)"
  workers=0
  while true; do
    avail="$(get_mem_available_kb)"
    if [ "${avail}" -le 0 ]; then
      break
    fi
    if [ "${avail}" -le "${target_avail_kb}" ]; then
      break
    fi
    if [ "${avail}" -lt "${min_avail_kb}" ]; then
      break
    fi
    if [ "${workers}" -ge "${max_workers}" ]; then
      break
    fi
    now="$(date +%s)"
    if [ "${timeout_s}" -gt 0 ] && [ "$((now-start_ts))" -ge "${timeout_s}" ]; then
      break
    fi
    workers="$((workers+1))"
    ap_log="${out_dir}/apfill_${step_tag}_w${workers}.log"
    bash -lc "
      set -euo pipefail
      while true; do
        for i in \$(seq 1 ${tables}); do
          /tmp/ogsql \"select count(*) from sbtest\${i};\" >/dev/null 2>&1 || true
        done
      done
    " >\"${ap_log}\" 2>&1 &
    pids+=("$!")
    sleep 2
  done
  printf "%s" "${pids[*]}"
}

stop_apfill() {
  local pids_str="$1"
  if [ -z "${pids_str}" ]; then
    return 0
  fi
  for pid in ${pids_str}; do
    kill_tree "${pid}"
  done
  for pid in ${pids_str}; do
    wait "${pid}" 2>/dev/null || true
  done
}

start_memhog() {
  local mb="$1"
  local target_avail_kb="$2"
  local min_avail_kb="$3"
  if [ "${mb}" -le 0 ] && [ "${target_avail_kb}" -le 0 ]; then
    echo "0"
    return
  fi
  local hog_log="${out_dir}/memhog_step_${idx}_${buf}.log"
  nice -n 19 python3 - <<PY > "${hog_log}" 2>&1 &
import time
chunks=[]
target_mb=int("${mb}")
target_avail_kb=int("${target_avail_kb}")
min_avail_kb=int("${min_avail_kb}")
def mem_available_kb():
    try:
        with open("/proc/meminfo","r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except Exception:
        return -1
    return -1

print(f"target_mb={target_mb} target_mem_available_kb={target_avail_kb} min_mem_available_kb={min_avail_kb}", flush=True)

allocated=0
if target_avail_kb > 0:
    while True:
        avail=mem_available_kb()
        if avail > 0 and avail <= target_avail_kb:
            break
        if avail > 0 and avail < min_avail_kb:
            print(f"stop_allocating: mem_available_kb={avail} < min_mem_available_kb={min_avail_kb}", flush=True)
            break
        if target_mb > 0 and allocated >= target_mb:
            break
        chunks.append(bytearray(1024*1024))
        allocated += 1
        if allocated % 128 == 0:
            avail=mem_available_kb()
            print(f"allocated_mb={allocated} mem_available_kb={avail}", flush=True)
else:
    for i in range(target_mb):
        chunks.append(bytearray(1024*1024))
        allocated += 1
        if allocated % 128 == 0:
            avail=mem_available_kb()
            print(f"allocated_mb={allocated} mem_available_kb={avail}", flush=True)
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

set_shared_buffers_conf() {
  local value="$1"
  python3 - "${value}" "${data_dir}" <<'PY'
from pathlib import Path
import sys

value = sys.argv[1]
data_dir = sys.argv[2]

p = Path(data_dir) / "postgresql.conf"
s=p.read_text(errors="ignore").splitlines()
out=[]
found=False
for line in s:
    if line.strip().startswith("shared_buffers"):
        out.append(f"shared_buffers = {value}")
        found=True
    else:
        out.append(line)
if not found:
    out.append(f"shared_buffers = {value}")
p.write_text("\n".join(out) + "\n")
PY
}

restart_db() {
  local log_file="$1"
  if [ -n "${restart_cmd}" ]; then
    run_with_deadline "${per_step_walltime_s}" bash -lc "set -o pipefail; { ${restart_cmd}; } >>'${log_file}' 2>&1"
    return
  fi
  run_with_deadline "${per_step_walltime_s}" bash -lc "su - '${os_user}' -c \"export GAUSSHOME='${gauss_home}'; export LD_LIBRARY_PATH='${gauss_home}/lib'; '${gauss_home}/bin/gs_ctl' restart -D '${data_dir}' -m fast\" >>'${log_file}' 2>&1"
}

if [ "${per_step_walltime_s}" -le 0 ]; then
  per_step_walltime_s="$((warmup_s + duration_s + 600))"
fi

preflight

current_hog_pid="0"
current_ap_pids=""
summary_closed="0"
cleanup() {
  stop_memhog "${current_hog_pid}"
  stop_apfill "${current_ap_pids}"
  if [ "${summary_closed}" != "1" ] && [ -f "${out_dir}/summary.json" ]; then
    printf "%s\n" "]" >> "${out_dir}/summary.json" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [ "${auto_count}" -gt 0 ]; then
  buffer_list="$(
    python3 - "${auto_min_mb}" "${auto_max_mb}" "${auto_count}" "${auto_scale}" "${auto_round_mb}" <<'PY'
import math
import sys

min_mb=int(float(sys.argv[1]))
max_mb=int(float(sys.argv[2]))
count=int(float(sys.argv[3]))
scale=str(sys.argv[4]).strip().lower()
round_mb=int(float(sys.argv[5]))

if min_mb <= 0 or max_mb <= 0 or max_mb < min_mb:
    raise SystemExit("invalid min/max")
if count < 2:
    raise SystemExit("count must be >=2")
if scale not in ("log","linear"):
    raise SystemExit("scale must be log|linear")
if round_mb <= 0:
    raise SystemExit("round_mb must be >0")

vals=[]
for i in range(count):
    t=i/(count-1)
    if scale=="linear":
        v=min_mb + (max_mb-min_mb)*t
    else:
        v=min_mb * ((max_mb/min_mb)**t)
    v=int(round(v/round_mb)*round_mb)
    v=max(min_mb, min(max_mb, v))
    vals.append(v)

vals=sorted(set(vals))
if len(vals) < 2:
    raise SystemExit("generated too few unique values")

print(",".join(f"{v}MB" for v in vals))
PY
  )"
fi

case "${subset}" in
  all|train|test) ;;
  *) echo "invalid --subset (use: all|train|test)" >&2; exit 2;;
esac

IFS=',' read -r -a bufs <<< "${buffer_list}"

case "${fill_method}" in
  memhog|ap) ;;
  *) echo "invalid --fill-method (use: memhog|ap)" >&2; exit 2;;
esac

log "start sweep: buffers=${buffer_list} mode=${mode} warmup_s=${warmup_s} duration_s=${duration_s} workload=${workload} fill_method=${fill_method} hog_mb=${hog_mb} target_mem_available_kb=${target_mem_available_kb} min_mem_available_kb=${min_mem_available_kb} ap_max_workers=${ap_max_workers} ap_tables=${ap_tables} ap_timeout_s=${ap_timeout_s} per_step_walltime_s=${per_step_walltime_s} (out_dir=${out_dir})"
if [ -n "${restart_cmd}" ]; then
  log "restart_cmd=${restart_cmd}"
fi

printf "%s\n" "[" > "${out_dir}/summary.json"
first=1
idx=0
total="${#bufs[@]}"
for raw_buf in "${bufs[@]}"; do
  idx="$((idx+1))"
  split="train"
  if [ "$((idx%2))" -eq 0 ]; then
    split="test"
  fi
  if [ "${subset}" != "all" ] && [ "${split}" != "${subset}" ]; then
    continue
  fi
  buf="$(echo "${raw_buf}" | tr -d ' ')"
  log "step ${idx}/${total}: set shared_buffers=${buf} then restart (split=${split})"
  set_shared_buffers_conf "${buf}"
  restart_log="${out_dir}/restart_${idx}_${buf}.log"
  set +e
  restart_db "${restart_log}"
  restart_rc="$?"
  set -e
  if [ "${restart_rc}" -ne 0 ]; then
    log "step ${idx}/${total}: restart failed rc=${restart_rc} shared_buffers=${buf}"
    if [ "${first}" -ne 1 ]; then
      printf "%s\n" "," >> "${out_dir}/summary.json"
    fi
    first=0
    printf "%s" "$(printf '{\"shared_buffers\":\"%s\",\"error\":\"restart_failed_or_timeout\",\"rc\":%s}' "${buf}" "${restart_rc}")" >> "${out_dir}/summary.json"
    continue
  fi
  sleep 2

  current_hog_pid="0"
  current_ap_pids=""
  if [ "${fill_method}" = "memhog" ]; then
    current_hog_pid="$(start_memhog "${hog_mb}" "${target_mem_available_kb}" "${min_mem_available_kb}")"
    if [ "${current_hog_pid}" != "0" ]; then
      log "step ${idx}/${total}: memhog pid=${current_hog_pid} (tail -f ${out_dir}/memhog_step_${idx}_${buf}.log)"
    fi
    sleep 2
  else
    step_tag="step_${idx}_${buf}"
    current_ap_pids="$(start_apfill "${target_mem_available_kb}" "${min_mem_available_kb}" "${ap_max_workers}" "${ap_timeout_s}" "${ap_tables}" "${step_tag}")"
    if [ -n "${current_ap_pids}" ]; then
      log "step ${idx}/${total}: apfill pids=${current_ap_pids} (logs: ${out_dir}/apfill_${step_tag}_w*.log)"
    else
      log "step ${idx}/${total}: apfill skipped (target_mem_available_kb=${target_mem_available_kb})"
    fi
    sleep 2
    stop_apfill "${current_ap_pids}"
    current_ap_pids=""
  fi

  avail_kb="0"
  avail_kb="$(get_mem_available_kb)"
  log "step ${idx}/${total}: MemAvailable=${avail_kb}KB"
  if [ "${avail_kb}" -gt 0 ] && [ "${avail_kb}" -lt "${min_mem_available_kb}" ]; then
    log "step ${idx}/${total}: skip (MemAvailable=${avail_kb}KB < ${min_mem_available_kb}KB)"
    if [ "${first}" -ne 1 ]; then
      printf "%s\n" "," >> "${out_dir}/summary.json"
    fi
    first=0
    printf "%s" "$(printf '{\"shared_buffers\":\"%s\",\"error\":\"skip_low_memavailable\",\"mem_available_kb\":%s}' "${buf}" "${avail_kb}")" >> "${out_dir}/summary.json"
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
    --label "phase=warmup,shared_buffers=${buf},split=${split},hog_mb=${hog_mb},target_mem_available_kb=${target_mem_available_kb},step=${idx}/${total}" >/dev/null
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
      --label "phase=measure,shared_buffers=${buf},split=${split},hog_mb=${hog_mb},target_mem_available_kb=${target_mem_available_kb},step=${idx}/${total}"
  )"
  rc="$?"
  set -e
  if [ -z "${run_json}" ]; then
    rc=1
    run_json="$(printf '{\"shared_buffers\":\"%s\",\"error\":\"empty_run_json\",\"rc\":%s}' "${buf}" "${rc}")"
  fi
  if [ "${rc}" -ne 0 ]; then
    run_json="$(printf '{\"shared_buffers\":\"%s\",\"error\":\"timeout_or_failed\",\"rc\":%s}' "${buf}" "${rc}")"
  fi

  memhog_actual_mb="0"
  if [ "${current_hog_pid}" != "0" ] && [ -f "${out_dir}/memhog_step_${idx}_${buf}.log" ]; then
    memhog_actual_mb="$(awk -F= '/^final_allocated_mb=/{v=$2} END{print v+0}' "${out_dir}/memhog_step_${idx}_${buf}.log" 2>/dev/null || echo 0)"
  fi
  run_json="$(
    printf "%s" "${run_json}" | python3 - "${hog_mb}" "${memhog_actual_mb}" "${target_mem_available_kb}" "${min_mem_available_kb}" "${fill_method}" "${ap_max_workers}" "${ap_tables}" "${ap_timeout_s}" <<'PY'
import json
import sys
hog_mb=int(sys.argv[1])
actual=int(sys.argv[2])
target_avail=int(sys.argv[3])
min_avail=int(sys.argv[4])
fill_method=str(sys.argv[5])
ap_max_workers=int(sys.argv[6])
ap_tables=int(sys.argv[7])
ap_timeout_s=int(sys.argv[8])
raw=sys.stdin.read().strip()
try:
    obj=json.loads(raw) if raw else {}
except Exception:
    obj={"error":"invalid_run_json"}
obj["memhog_target_mb"]=hog_mb
obj["memhog_actual_mb"]=actual
obj["target_mem_available_kb"]=target_avail
obj["min_mem_available_kb"]=min_avail
obj["fill_method"]=fill_method
obj["ap_max_workers"]=ap_max_workers
obj["ap_tables"]=ap_tables
obj["ap_timeout_s"]=ap_timeout_s
print(json.dumps(obj, ensure_ascii=False))
PY
  )"

  printf "%s" "${run_json}" >> "${out_dir}/summary.json"
  log "step ${idx}/${total}: done rc=${rc} shared_buffers=${buf}"
  stop_memhog "${current_hog_pid}"
  current_hog_pid="0"
  stop_apfill "${current_ap_pids}"
  current_ap_pids=""
  sleep 1
done
printf "%s\n" "]" >> "${out_dir}/summary.json"
summary_closed="1"

log "sweep finished (summary -> ${out_dir}/summary.json)"
echo "${out_dir}"
