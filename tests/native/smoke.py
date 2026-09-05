#!/usr/bin/env python3
"""Exercise the patched Android binary without loading BPF programs."""

import subprocess
import sys

qemu, options, memleak = sys.argv[1:]


def check(binary, args, expected=0, contains=""):
    result = subprocess.run(
        [qemu, binary, *args], capture_output=True, text=True, timeout=15
    )
    if (result.returncode == 0) != (
        expected == 0
    ) or contains not in result.stdout + result.stderr:
        raise AssertionError((args, result.returncode, result.stdout, result.stderr))


path = "/apex/com.android.runtime/lib64/bionic/hwasan/libc.so"
check(options, [], contains="default=/system/lib64/libc.so")
check(options, ["-O", path], contains="object=" + path + "\n")
check(options, ["-O", "a" * 4095], contains="object=" + "a" * 4095 + "\n")
check(options, ["-O", "a" * 4096], expected=1, contains="path is too long")
check(options, ["-o", "60000"], contains="age_ns=60000000000\n")
check(options, ["-o", "9223372036854"], contains="age_ns=9223372036854000000\n")
check(options, ["-o", "9223372036855"], expected=1, contains="invalid AGE_MS")
check(options, ["-o", "-1"], expected=1)
check(options, ["-S", "je_"], contains="prefix=je_\n")
check(options, ["--stack-storage-size", "65536", "--perf-max-stack-depth", "127"])
check(memleak, ["--help"], contains="stack-storage-size")
check(memleak, ["-h"], contains="Trace outstanding memory allocations")
check(memleak, ["--no-such-option"], expected=1)
print("Android/QEMU smoke tests: 13 passed (no BPF syscalls)")
