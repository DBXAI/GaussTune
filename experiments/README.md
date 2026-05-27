# PostgreSQL Experiments

This directory contains selected PostgreSQL experiment assets.

## Layout

- `postgresql/`: experiments copied from `/root/Huawei`.

## Selection policy

The repository keeps files that are useful for reproducing or reviewing the
experiments:

- experiment scripts, SQL files, and bpftrace programs;
- analysis scripts;
- README/report Markdown files;
- generated result figures.

Large or machine-specific artifacts are intentionally excluded:

- raw benchmark traces and raw SQL output;
- transient logs;
- Python bytecode/cache directories;
- presentation binaries.
