#!/usr/bin/env python3
"""Validate that Linux can release retired shared-buffer pages on this host."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import resource
import time


IPC_PRIVATE = 0
IPC_CREAT = 0o1000
IPC_RMID = 0
MADV_REMOVE = 9


def resident_pages(libc: ctypes.CDLL, address: int, length: int, page_size: int) -> int:
    pages = (length + page_size - 1) // page_size
    vector = (ctypes.c_ubyte * pages)()
    if libc.mincore(ctypes.c_void_p(address), ctypes.c_size_t(length), vector) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return sum(1 for value in vector if value & 1)


def validate(size_mb: int) -> dict[str, int | bool]:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.shmget.restype = ctypes.c_int
    libc.shmat.restype = ctypes.c_void_p
    libc.madvise.restype = ctypes.c_int
    page_size = resource.getpagesize()
    length = size_mb * 1024 * 1024
    shmid = libc.shmget(IPC_PRIVATE, length, IPC_CREAT | 0o600)
    if shmid < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))

    address = libc.shmat(shmid, None, 0)
    if address == ctypes.c_void_p(-1).value:
        error = ctypes.get_errno()
        libc.shmctl(shmid, IPC_RMID, None)
        raise OSError(error, os.strerror(error))

    try:
        ctypes.memset(address, 0x5A, length)
        before = resident_pages(libc, address, length, page_size)
        if libc.madvise(ctypes.c_void_p(address), ctypes.c_size_t(length), MADV_REMOVE) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        time.sleep(0.05)
        after = resident_pages(libc, address, length, page_size)
        zero_after_remove = ctypes.c_ubyte.from_address(address).value == 0
        after_one_read = resident_pages(libc, address, length, page_size)
        return {
            "size_mb": size_mb,
            "pages": length // page_size,
            "resident_before": before,
            "resident_after_remove": after,
            "resident_after_one_read": after_one_read,
            "zero_after_remove": zero_after_remove,
            "release_supported": before == length // page_size and after == 0 and zero_after_remove,
        }
    finally:
        libc.shmdt(ctypes.c_void_p(address))
        libc.shmctl(shmid, IPC_RMID, None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=256)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = validate(args.size_mb)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="ascii") as stream:
            stream.write(payload)
    print(payload, end="")
    if not result["release_supported"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
