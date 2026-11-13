#!/usr/bin/env python3
"""Package the actual running binary, clean source snapshot and JMT code.

Run ONLY after benchmark windows: copying/hashing/compression affects IO/CPU.
No data directory, production configuration, credentials, git config, historical
binaries, generated build output or private collector backups are included.
Does not stop, restart, install over, or modify the current database.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(2**20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def command(argv, cwd=None, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, check=True)
    return result.stdout


def command_bytes(argv, cwd=None):
    # Some tracked openGauss regression fixtures are GB18030, not UTF-8.
    # Patches must remain byte-exact rather than replacing undecodable bytes.
    return subprocess.run(argv, cwd=cwd, capture_output=True, check=True).stdout


def copy_entry(source, target, records, original_root):
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        link = os.readlink(str(source))
        # Runtime libraries must remain self-contained after relocation.
        if Path(link).is_absolute():
            raise ValueError("absolute symlink must be reviewed: " + str(source))
        resolved = source.resolve()
        if original_root.resolve() not in resolved.parents:
            raise ValueError("symlink escapes package source root: " + str(source))
        target.symlink_to(link)
        records.append({"path": str(target), "symlink": link})
        return
    if not source.is_file():
        raise ValueError("unexpected non-regular package input: " + str(source))
    shutil.copyfile(str(source), str(target))
    target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
    records.append({"path": str(target), "sha256": sha(target), "bytes": target.stat().st_size})


def runtime_files(home):
    # Top-level bin files only: old prototype directories and backup executables
    # are not required to run the current modified server.
    for entry in sorted((home / "bin").iterdir()):
        if entry.is_dir() or any(x in entry.name for x in (".before-", ".pre-", ".original", ".private")):
            continue
        yield entry
    for name in ("lib", "share", "include", "jre"):
        for entry in sorted((home / name).rglob("*")):
            if entry.is_dir() and not entry.is_symlink():
                continue
            if entry.suffix.lower() in (".key", ".pem", ".p12", ".pfx", ".keytab", ".cipher", ".rand"):
                raise ValueError("potential secret needs explicit review: " + str(entry))
            yield entry
    yield home / "version.cfg"


def create_release(args):
    check = subprocess.run(["pgrep", "-x", "sysbench"], capture_output=True, text=True)
    if check.returncode == 0:
        raise RuntimeError("refusing CPU/IO-heavy packaging while sysbench is running")
    measured = json.loads((args.mixed_result / "MIXED_TPS_RESULT.json").read_text())
    if not measured.get("restoration_verified"):
        raise RuntimeError("mixed benchmark has not finished and restored its configuration")
    home, source = args.gausshome.resolve(), args.source.resolve()
    pid = int((args.data_dir / "postmaster.pid").read_text().splitlines()[0])
    binary_hash = sha(Path("/proc/%d/exe" % pid))
    if binary_hash != sha(home / "bin/gaussdb"):
        raise RuntimeError("installed binary differs from the current postmaster executable")
    args.out.mkdir(parents=True, exist_ok=False)
    release = args.out / "jmt-modified-opengauss-5.1.0"
    release.mkdir()
    records = []
    for entry in runtime_files(home):
        copy_entry(entry, release / "opengauss" / entry.relative_to(home), records, home)
    tracked = command(["git", "ls-files", "-z"], cwd=source).split("\0")
    extra = ["src/include/storage/buf/jmt_buffer_trace.h"]
    missing = []
    for name in sorted(set(tracked + extra) - {""}):
        entry = source / name
        if not entry.exists() and not entry.is_symlink():
            missing.append(name)
            continue
        copy_entry(entry, release / "source" / name, records, source)
    provenance = release / "provenance"
    provenance.mkdir()
    baseline = command(["git", "rev-parse", "HEAD"], cwd=source).strip()
    upstream = command(["git", "rev-parse", "HEAD^"], cwd=source).strip()
    (provenance / "committed-online-resize.patch").write_bytes(
        command_bytes(["git", "diff", "--binary", upstream, baseline], cwd=source))
    (provenance / "uncommitted-tracked.patch").write_bytes(
        command_bytes(["git", "diff", "--binary", "HEAD"], cwd=source))
    (provenance / "git-status.txt").write_text(command(["git", "status", "--short"], cwd=source))
    # Capture exact configure options, but do not copy config.status/config.log,
    # which are generated, machine-bound and could include environment secrets.
    global_make = (source / "src/Makefile.global").read_text()
    (provenance / "configure-args.txt").write_text(
        next(line for line in global_make.splitlines() if line.startswith("configure_args")) + "\n")
    env = dict(os.environ, LD_LIBRARY_PATH=str(home / "lib"), GAUSSHOME=str(home))
    for binary in ("gaussdb", "gsql"):
        (provenance / (binary + "-version.txt")).write_text(
            command([str(home / "bin" / binary), "--version"], env=env))
        (provenance / (binary + "-ldd.txt")).write_text(
            command(["ldd", str(home / "bin" / binary)], env=env))
    (provenance / "os-release.txt").write_text(Path("/etc/os-release").read_text())
    (provenance / "uname.txt").write_text(command(["uname", "-a"]))
    write_json(provenance / "SOURCE_BINARY_IDENTITY.json", {
        "created_at": datetime.now().astimezone().isoformat(),
        "running_gaussdb_pid_at_collection": pid,
        "running_and_installed_binary_sha256": binary_hash,
        "source_HEAD": baseline, "source_upstream_parent": upstream,
        "source_build_output_sha256": sha(source / "src/gausskernel/gaussdb"),
        "source_build_output_matches_running_binary":
            sha(source / "src/gausskernel/gaussdb") == binary_hash,
        "rebuild_reproducibility_verified": False,
        "all_current_source_edits_proven_in_binary": False,
        "version_string_is_not_build_provenance": True,
        "missing_tracked_files_preserved_as_absent": missing,
        "extra_untracked_source_files_included": extra,
        "generated_build_files_and_git_directory_included": False})
    model = release / "model"
    for name in ("src", "vendor", "tools", "tests", "runtime"):
        for entry in sorted((ROOT / name).rglob("*")):
            if (entry.is_dir() or "__pycache__" in entry.parts or entry.suffix == ".pyc"):
                continue
            copy_entry(entry, model / entry.relative_to(ROOT), records, ROOT)
    for name in ("README.md", "replay_experiment1.py", "FIVE_STAGE_CURRENT_RECOMMENDATIONS.md",
                 "PREDICTION_ACCURACY_CURRENT.md", "CURRENT_FIVE_STAGE_RECOMMENDATIONS.json",
                 "MIXED_TPS_CURRENT.md", "CURRENT_MIXED_TPS.json", "CANDIDATE_STATUS.json",
                 "CODE_SHA256SUMS"):
        copy_entry(ROOT / name, model / name, records, ROOT)
    for line in (ROOT / "CODE_SHA256SUMS").read_text().splitlines():
        expected, name = line.split("  ", 1)
        entry, target = ROOT / name, model / name
        if ROOT.resolve() not in entry.resolve().parents or sha(entry) != expected:
            raise ValueError("code manifest path/digest mismatch: " + name)
        if not target.exists():
            copy_entry(entry, target, records, ROOT)
    recommendation_pointer = json.loads((ROOT / "CURRENT_FIVE_STAGE_RECOMMENDATIONS.json").read_text())
    recommendation_dir = ROOT / Path(recommendation_pointer["result"]).parent
    for entry in sorted(recommendation_dir.iterdir()):
        if entry.is_file():
            copy_entry(entry, model / entry.relative_to(ROOT), records, ROOT)
    # Bind frozen historical inputs by content hash, without relying on /root
    # existing on the destination. This is not a claim that retired raw traces
    # or all recursively referenced historical artifacts are in the package.
    frozen_document = json.loads((ROOT / recommendation_pointer["result"]).read_text())
    bindings = []
    for binding in frozen_document["source_artifacts"]:
        entry = Path(binding["path"])
        if entry.suffix not in (".json", ".sql") or sha(entry) != binding["sha256"]:
            raise ValueError("frozen input changed or needs security review: " + str(entry))
        target = release / "evidence/frozen-inputs" / (binding["sha256"] + entry.suffix)
        if not target.exists():
            copy_entry(entry, target, records, entry.parent)
        bindings.append({"original_path": str(entry), "sha256": binding["sha256"],
                         "package_relative_path": str(target.relative_to(release))})
    write_json(provenance / "FROZEN_INPUT_PATH_MAP.json", {
        "direct_frozen_inputs": bindings, "all_recursive_historical_raw_evidence_included": False})
    evidence = release / "evidence" / "mixed-tps"
    evidence.mkdir(parents=True)
    for entry in sorted(args.mixed_result.iterdir()):
        if entry.is_file() and entry.suffix in (".json", ".log", ".stderr") and ".private" not in entry.name:
            copy_entry(entry, evidence / entry.name, records, args.mixed_result)
    queries = release / "queries"
    queries.mkdir()
    for q in (2, 9, 13, 18, 21):
        copy_entry(args.sql_dir / ("q%d.sql" % q), queries / ("q%d.sql" % q),
                   records, args.sql_dir)
    # Retain this small tool independently of model paths for release testing.
    copy_entry(ROOT / "tools/smoke_test_packaged_opengauss.py",
               release / "scripts/smoke_test_packaged_opengauss.py", records, ROOT)
    templates = release / "templates"
    templates.mkdir()
    (templates / "postgresql-experiment.conf").write_text(
        "# TEMPLATE ONLY: review memory against the target machine before use.\n"
        "# No credentials, connection rules, production paths or machine secrets.\n"
        "shared_buffers = 8192MB\nshared_buffers_target = 8192MB\n"
        "shared_buffers_resize_granule = 256MB\nshared_buffers_resize_interval = 1000\n"
        "max_process_memory = 20GB\nmax_connections = 300\n"
        "jmt_buffer_trace_epoch = 0\njmt_buffer_trace_sample_ppm = 0\n")
    scripts = release / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "env.sh").write_text(
        '#!/usr/bin/env bash\n# Source this file; do not execute in a subshell.\n'
        'export GAUSSHOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/../opengauss" && pwd)"\n'
        'export PATH="$GAUSSHOME/bin:$PATH"\n'
        'export LD_LIBRARY_PATH="$GAUSSHOME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n')
    (scripts / "verify.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'cd "$(dirname "$0")/.."\nsha256sum -c FILE_SHA256SUMS\n')
    for script in scripts.iterdir():
        script.chmod(0o755)
    (release / "README_复现与边界.md").write_text("""# 当前魔改 openGauss + 四项创新代码

## 包含什么
- `opengauss/`：实测实例正在运行的 gaussdb **原样二进制**、配套工具、库、share/include/JRE。
- `source/`：当前 Git 跟踪源码的工作树快照，以及实际使用的 JMT trace 头文件；
  排除了 `.git`、configure 临时文件、编译产物。当前缺失的 7 个回归测试期望文件未擅自补回。
- `provenance/`：源码 HEAD、上游基点、已提交 resize 补丁、未提交补丁、构建选项、ABI/依赖及二进制 SHA。
- `model/src/jmt/innovation_{1_cache,2_spill,3_search,4_performance}/`：四项创新。
- `evidence/mixed-tps/`：这次真实五阶段窗口结果、运行前冻结的推荐值、原始 TPS 日志。
- `evidence/frozen-inputs/`：旧推荐直接绑定的 77 份小型输入，按 SHA 保存；
  原路径映射在 provenance/FROZEN_INPUT_PATH_MAP.json，不声称包含已清理的全部历史原始链。
- `queries/`：原始 SF85 五条完整 SQL；`templates/`：不含密码的参数模板。

## 解压、校验与环境
在 x86_64 Linux 上解压，执行 `bash scripts/verify.sh`，再 `source scripts/env.sh`。
本机 ABI 是 Ubuntu 20.04 / glibc 2.31；系统仍需 libaio、libncurses6、
libtinfo6、libcrypt1、libkeyutils1 等，详见 provenance 中 ldd 清单。
例如 Ubuntu 上由管理员安装这些依赖；不要用包内库覆盖系统 glibc。
执行 `gaussdb --version` 和 `gsql --version` 检查动态链接。
**不同架构或不兼容 ABI 不保证可直接运行，不能称为已经跨机器验证。**

## 新实例（不要覆盖已有数据库）
用独立非 root 数据库用户创建**全新** DATADIR；
执行 `gs_initdb --help`，用 `--pwfile` 提供本机新生成的强密码文件。
不要把密码写进脚本、命令历史或本包。
从 initdb 生成的配置出发，按机器资源审核后追加 templates 中参数；
由目标机器自行设置 pg_hba.conf、socket、端口与访问规则。
`gs_ctl start -D "$DATADIR" -M primary -l "$DATADIR/start.log"`。
不提供携带本机身份信息的生产配置，也不自动写系统服务。

## 在线 SB 与 work_mem
`shared_buffers` 是启动上限，实际在线活动容量由 `shared_buffers_target` 控制。
例如 `gs_guc reload -D "$DATADIR" -c "shared_buffers_target=2048MB"`。
必须等待**本次调整之后新增的** `shared buffer resize committed: active buffers ...`
日志，不能只看到 SHOW 改变就认定 resize 完成。
`work_mem` 是每算子/会话限制，不等于全查询总 grant 或物理峰值预算。
恢复目标最大值也无需重启；本次验证会恢复原配置。

## 模型与实验复现
本机使用 Python 3.8、NumPy 1.24.4，运行时把 `model/src`、`model/vendor` 加入 PYTHONPATH；
离线测试：`cd model; PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:vendor python3 -m unittest discover -s tests`。
核心回归与采样兼容层测试源码和固定 JSON 夹具一并附带，不依赖原机其他 JMT 目录。
`model/runtime/` 是轻量直接采样的历史 pickle/导入兼容层，核心预测仍按四项创新组织。
实时采集额外需要 sysbench + PostgreSQL driver、psycopg2、BCC Python 模块、匹配的
内核头文件、root 的 BPF 探针权限及独立非 root 数据库用户。
包不携带本机 venv、操作系统包、数据库数据或登录密码。
目前实时 collector 的默认部署布局是 `/opt/openGauss`，数据在其 `data/`，
Unix socket `/tmp:5432`，本地 OS/数据库用户 omm，数据库 h5_tpcc/h5_tpch，
块设备 `/dev/nvme0n1`。换机器需核对/修改这些实验绑定，**不能直接沿用本机校准**；
模型核心各模块和 JSON 输入本身独立于这些采集绑定。

数据集为 TP 16 张 sbtest 表 × 每表 100 万行，约 5.1GB；AP 为 TPC-H SF85。
它不是 PPT 的 20GB TP 数据；包内 SQL 不能替代数据集生成和导入。
直接采样统计有水位、TTL 和适用范围，换数据必须重采；
缓存/CPU/设备/AP 时间模型也必须重新采集校准，不能复制指纹冒充当前证据。
本次五阶段结果是**连续轨迹中的短重叠窗口**，重复窗口不是独立完整实验重复。
AP 在测量后按计划取消，不提供完整 AP 时延、完整 spill、物理峰值安全或最优性证明。
配置是旧模型预先冻结的推荐；有数值实测不代表推荐已合格。

## 从源码重建
构建输入在 source/；历史选项见 provenance/configure-args.txt。
它引用的第三方 binarylibs/gcc7.3 **没有混入运行包**，需在目标机准备版本匹配的
openGauss 第三方库与工具链，按 source/build.sh --help 和源码说明构建。
未完成可重复重建，不宣称“当前所有源码改动都已被证明编入当前二进制”；
SOURCE_BINARY_IDENTITY.json 明确分别记录是否与现存构建产物字节相同及其局限。

## 明确未打包
189GB 数据目录、WAL、日志、原 postgresql.conf/pg_hba.conf、账号密钥、私有备份、
旧 gaussdb/原型目录、机器 venv、第三方构建工具链、历史实验垃圾。
依赖所附上游许可/版权声明；本包不额外改变任何组件许可。
""")
    if sha(Path("/proc/%d/exe" % pid)) != binary_hash or sha(home / "bin/gaussdb") != binary_hash:
        raise RuntimeError("running binary changed while packaging")
    # Paths in this provenance manifest are relative, not source-machine roots.
    for row in records:
        row["path"] = str(Path(row["path"]).relative_to(release))
    write_json(provenance / "COPIED_INPUTS.json", records)
    regenerate_manifest(release)
    return release


def regenerate_manifest(release):
    entries = []
    for path in sorted(release.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "FILE_SHA256SUMS":
            entries.append("%s  %s\n" % (sha(path), path.relative_to(release)))
    (release / "FILE_SHA256SUMS").write_text("".join(entries))


def archive_release(release, out):
    archive = out / (release.name + ".tar.gz")
    def normalize(info):
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        if info.isdev() or info.isfifo():
            raise ValueError("unexpected device/FIFO in release")
        return info
    with tarfile.open(str(archive), "w:gz", compresslevel=2) as tar:
        tar.add(str(release), arcname=release.name, filter=normalize)
    (out / "ARCHIVE_SHA256SUMS").write_text("%s  %s\n" % (sha(archive), archive.name))
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gausshome", type=Path, default=Path("/opt/openGauss"))
    parser.add_argument("--data-dir", type=Path, default=Path("/opt/openGauss/data"))
    parser.add_argument("--source", type=Path, default=Path("/root/openGauss-server-5.1.0"))
    parser.add_argument("--mixed-result", type=Path, required=True)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true",
                        help="Prepare tree for isolated smoke test before final archive")
    args = parser.parse_args()
    release = create_release(args)
    print(str(release), flush=True)
    if not args.prepare_only:
        print(str(archive_release(release, args.out)), flush=True)


if __name__ == "__main__":
    main()
