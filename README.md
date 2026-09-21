# android-memleak

Build a fully static Android ARM64 `memleak` from an upstream
[BCC release](https://github.com/iovisor/bcc/releases). The build requires no
AOSP checkout, Soong, or `lunch`, and does not reuse AOSP `.a` or `.o` files.

This project grew out of a BCC/AOSP fork and now focuses on
`libbpf-tools/memleak`. It does not build Python `tools/memleak.py` or the full
BCC tool suite. The project name is `android-memleak`, but an existing local
checkout can still be named `bcc`. Scripts resolve paths relative to themselves,
so renaming the checkout does not affect the build entry points.

## Quick build

Build host: Linux x86_64 with Python 3.12+, Git, CMake 3.30+, Ninja, a host
`bpftool`, and the Android NDK. Project-owned C++ code uses C++26, which CMake
requires the compiler to support. NDK r30, Android API 35, and CMake 4.2.3 have
been tested. The build does not download the NDK automatically.

```sh
# Install Ubuntu host dependencies once.
sudo apt install python3 git cmake ninja-build bpftool

# Defaults: NDK at /mnt/develop/android-ndk-r30, parallelism from nproc.
make release

# Calling the script directly uses the same defaults.
python3 scripts/build-memleak.py

# Set the NDK and parallelism explicitly in environments such as GitHub Actions.
python3 scripts/build-memleak.py --ndk /path/to/android-ndk-r30 --jobs 4
```

NDK selection order: `--ndk` → `ANDROID_NDK_HOME` → `ANDROID_NDK_ROOT` →
`/mnt/develop/android-ndk-r30`. Without an argument or environment override, the
local default is used. An invalid path fails before any downloads; the script
does not silently switch to another NDK. Without `--jobs`, it uses `nproc`.
`make release` also defaults to `nproc`; override it with `make release JOBS=4`.
The selected NDK and job count are printed before the build starts.

The top-level [sources.lock](sources.lock) defaults to `bcc: "latest"`.
Each online build queries GitHub for the latest **published, non-prerelease BCC
release**, resolves its tag to a commit, downloads the sources, applies patches,
and compiles them. The host runs `bpftool gen skeleton`; NDK Clang compiles both
userspace and BPF code. libbpf, elfutils/libelf, and zstd are built from pinned
sources. Bionic libc, libc++, and zlib come from the same NDK. libelf retains
support for zlib/zstd-compressed ELF files.

Outputs:

- `dist/memleak`: stripped, fully static ARM64 executable with 16 KiB alignment.
- `dist/build-info.json`: resolved release and commit, source/patch SHA-256
  values, toolchain details, and output metadata.
- `dist/SHA256SUMS`: output checksums.

`make verify` checks ELF architecture, static linkage, 16 KiB alignment, and
checksums. The BPF object is embedded in the executable, so the device needs no
NDK, Python, build tools, or separate `.bpf.o` file.

## Updates, version pinning, and offline builds

```sh
# Pin an upstream release, or edit the bcc field in sources.lock.
python3 scripts/build-memleak.py --version v0.37.0

# Resume tracking the latest published, non-prerelease release.
python3 scripts/build-memleak.py --version latest

# Download, verify, and patch sources only; no NDK required.
make prepare

# Rebuild the same configuration from an existing cache without network access.
python3 scripts/build-memleak.py --offline

# Requires a previous online preparation with the same --version setting.
python3 scripts/build-memleak.py --version v0.37.0 --offline
```

GitHub's public API is rate-limited. Set `GH_TOKEN` or `GITHUB_TOKEN` if needed.
The downloader does not require a `gh` login or read local `gh` credentials.
Source archives are downloaded over HTTPS from their official sources.
elfutils uses the configured SHA-256; other downloads use pinned commit URLs
and record their downloaded SHA-256 values. Cached content is verified again
before reuse. Resolved versions are recorded in `build-info.json`.

`sources.lock` is JSON, not a shell script to execute with `source`.
libbpf/elfutils/zstd inputs remain pinned, while `bcc: "latest"` intentionally
tracks published releases. This is therefore not a completely immutable build
lock. Each resolved version is written to `build/source-lock.json`; provenance
for successful outputs is recorded in `dist/build-info.json`.

**Future upstream releases may require maintenance.** If upstream changes code
covered by Android patches, libbpf APIs, or BPF structures, patching or building
fails explicitly. Patches are not silently skipped or reversed, and old sources
are not presented as a new version. Download, patch, compilation, or ELF
validation failures leave the last successful outputs intact.

## Project layout

```text
sources.lock           Upstream version, pinned dependencies, Android API (JSON)
CMakeLists.txt         Standalone static NDK build
Makefile               release / prepare / verify / test entry points
cmake/                 Skeleton generation and libelf configuration templates
compat/                Android argp and libintl compatibility layers
patches/               BCC/libbpf patches and ordered series
scripts/               build-memleak.py: download, patch, build, and verify
tests/                 Build regression tests; native/ has QEMU/device workloads
docs/                  Migration notes, device usage, and validation records
.cache/                Download cache (ignored)
sources/               Patched upstream sources isolated by content hash (ignored)
build/                 source-lock.json, build directories, test logs (ignored)
dist/                  Successful memleak, build-info.json, SHA256SUMS (ignored)
```

As in the standalone Platform-Tools project, `sources/` contains upstream
inputs, `build/android-arm64/<fingerprint>/` holds intermediate files, and
`dist/` contains deliverables. Since Android ARM64 is the only target, `dist/`
does not have another `android-arm64/` subdirectory.

BCC's `trace_helpers.c` uses `strtok_r` to pass NDK r30's deprecated API checks.
Edit `patches/` rather than cached sources. Changes to sources, patches, the NDK,
or build rules create a separate build directory, preventing stale objects
from entering a new build. Old directories remain available for diagnosis;
the script does not clean user-specified external paths.

## Android adaptations and validation scope

- The default allocator is `/system/lib64/libc.so`.
- `-O` accepts full APEX/HWASan paths up to the `PATH_MAX` limit; longer paths
  fail explicitly.
- The `-S` allocator prefix, stack map/depth settings, combined-map optimization,
  and invalid stack ID handling come from upstream BCC rather than a separate
  copy of older implementations.
- A locally maintained Android argp layer provides compatibility. Userspace
  symbol resolution uses `memleak.c` and helpers from the same BCC release,
  rather than mixing in older AOSP helper APIs.
- Allocation age conversion guards against integer overflow; libbpf directory
  `open()` calls are adapted to Bionic checks.
- Rust/blazesym is disabled by default, matching the original Android ARM64 path.

```sh
adb push dist/memleak /data/local/tmp/memleak
adb shell chmod 0755 /data/local/tmp/memleak
# Run in a root shell on the device:
/data/local/tmp/memleak -p <PID> \
  -O /apex/com.android.runtime/lib64/bionic/hwasan/libc.so \
  --stack-storage-size 65536 -T 20 1
```

Select the allocator using the target process's `/proc/PID/maps`; not every
process uses ordinary Bionic libc. Runtime support depends on device root
access, BPF/BTF/tracefs capabilities, and SELinux policy. Static compilation and
QEMU argument tests do not establish device verifier/uprobes support or prove
that all upstream memleak edge cases, such as failed realloc or partial munmap,
have been fixed.

The NDK output tested on 2026-09-05 was validated on an Android 16 / ARM64 /
Linux 6.6 device. Ordinary Bionic and HWASan malloc/free, `-C` aggregation, and
mmap/mremap/munmap produced data consistent with controlled allocations and
frees, with resolvable stacks. Camera Provider testing covered only a short
attachment, without a photo capture workload. See the
[device validation record](docs/device-validation-2026-09-05.md) for the
environment, checksums, commands, and limitations.

See also [device usage and historical notes](docs/android-usage.md) and
[migration and patch maintenance](docs/migration.md).

## GitHub Actions and releases

[release.yml](.github/workflows/release.yml) runs only when a `v*` tag, such as
`v1.0.0`, is pushed. Branch commits and pull requests do not trigger it, and
there is no manual dispatch entry point. The tag must reference a commit that
contains the workflow. The workflow does not create tags or change the source
version configuration.

- Runner: `ubuntu-26.04`, building Android ARM64 on an x86_64 host.
- NDK: pinned r30 (`30.0.16248370`), prepared through the runner's SDK Manager.
- Build: passes the runner's NDK path and `--jobs 4 --self-test` explicitly.
  The script uses `CMAKE_BUILD_TYPE=Release`, without relying on a local
  `/mnt/develop` path.
- Validation: Python tests, 13 QEMU argument tests, fully static ARM64 ELF and
  16 KiB alignment checks, and SHA-256 verification. QEMU does not load BPF and
  cannot replace device validation.
- Publication: the GitHub Release uploads **only the raw `memleak` executable**,
  with no extension or archive wrapper. `build-info.json` and `SHA256SUMS` are
  internal verification files carried in a temporary Actions artifact. After
  publication and asset digest verification, that artifact is deleted; its
  one-day retention remains a fallback if publication or cleanup fails.

The build job has read-only repository access. The publish job has
`contents: write` for releases and `actions: write` for artifact cleanup.
Both use GitHub's automatically provided `GITHUB_TOKEN`; no extra PAT, signing
key, or repository secret is required. The workflow creates a draft, uploads
`memleak`, downloads it again, and checks byte equality before publishing.
Failed drafts can be recovered by rerunning the workflow while its artifacts
remain available. After cleanup, rebuild to rerun. Published releases are not
overwritten; use a new tag for changes.

The BCC entry in `sources.lock` still selects `latest` or a pinned release.
Project tags are independent of BCC versions. To pin upstream inputs, edit
`sources.lock` before tagging. After downloading a Release executable to a
device, run `chmod 0755 memleak`.

GitHub's [Ubuntu 26.04 image documentation](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Readme.md)
lists the installed Java, Android SDK, and NDK versions. As of September 2026,
the image is in public preview.

## Tests

```sh
make test
make verify

# Optional: exercise Android argument parsing without loading BPF.
# Requires qemu-user on the host; no device or root access needed.
python3 scripts/build-memleak.py --offline --self-test
```

Repository build and compatibility code uses [Apache-2.0](LICENSE). Downloaded
third-party sources retain their own licenses, including BCC/libbpf, elfutils,
zstd, and NDK notices. The top-level license alone does not describe the
licensing of every statically linked component.
