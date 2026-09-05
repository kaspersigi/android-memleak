# android-memleak

从上游 [BCC release](https://github.com/iovisor/bcc/releases) 构建 Android arm64
全静态 `memleak`。不需要 AOSP、Soong、`lunch`，也不复用 AOSP 的 `.a` / `.o`。

项目由原 BCC/AOSP fork 收敛而来，专注 `libbpf-tools/memleak`，不是 Python
`tools/memleak.py`，也不是完整 BCC 工具集。项目统一使用 `android-memleak`
名称，本地检出目录可以继续叫 `bcc`。所有路径均相对于脚本自身解析，
本地目录后续改名也不影响构建入口。

## 一键构建

构建主机：Linux x86_64，Python 3.12+、Git、CMake 3.22+、Ninja、主机版
`bpftool` 和 Android NDK。已验证 NDK r27d、Android API 35；不自动下载 NDK。

```sh
# Ubuntu 主机依赖（只需安装一次）
sudo apt install python3 git cmake ninja-build bpftool

# 本地默认 NDK /mnt/develop/android-ndk-r27d，并行数为 nproc。
make release

# 直接运行脚本使用同样的默认值。
python3 scripts/build-memleak.py

# GitHub Actions 等环境显式指定 NDK 和并行数。
python3 scripts/build-memleak.py --ndk /path/to/android-ndk-r27d --jobs 4
```

NDK 选择顺序为 `--ndk` → `ANDROID_NDK_HOME` → `ANDROID_NDK_ROOT` →
`/mnt/develop/android-ndk-r27d`。没有参数或环境变量时即可使用本机默认安装；
路径无效会在下载前报错，不会静默切换到其他 NDK。
脚本未传 `--jobs` 时执行 `nproc`；`make release` 同样默认使用 `nproc`，
也可以通过 `make release JOBS=4` 覆盖。构建开始时会打印实际 NDK 和并行数。

默认读取顶层 [sources.lock](sources.lock) 的 `bcc: "latest"`，
每次在线构建查询 GitHub 最新**正式 release**，解析 tag 到 commit，再下载、
应用补丁并编译。主机只运行 `bpftool gen skeleton`；用户态和 BPF 都由 NDK
Clang 编译。libbpf、elfutils/libelf、zstd 从锁定源码构建；Bionic libc、
libc++、zlib 来自同一 NDK。libelf 保留 zlib/zstd 压缩 ELF 支持。

输出：

- `dist/memleak`：已 strip 的全静态 ARM64 可执行程序，16 KiB 对齐。
- `dist/build-info.json`：实际 release、commit、源码/补丁 SHA256、工具链及产物信息。
- `dist/SHA256SUMS`：产物校验值。

`make verify` 检查 ELF 架构、静态链接、16 KiB 对齐及校验值。BPF object 已
嵌入程序；设备上不需要 NDK、Python、编译工具或独立的 `.bpf.o`。

## 更新、固定版本和离线构建

```sh
# 固定一个上游正式 release（也可直接修改 sources.lock 的 bcc 字段）
python3 scripts/build-memleak.py --version v0.37.0

# 恢复自动选择最新正式 release
python3 scripts/build-memleak.py --version latest

# 只下载、校验并应用补丁，不需要 NDK
make prepare

# 首次在线成功后，同一配置使用缓存重建，完全不访问网络
python3 scripts/build-memleak.py --offline

# 离线重建指定版本，需要此前已在线准备过同样的 --version 配置
python3 scripts/build-memleak.py --version v0.37.0 --offline
```

GitHub 公共 API 有频率限制，必要时设置 `GH_TOKEN` 或 `GITHUB_TOKEN`。
下载代码不依赖 `gh` 登录，也不读取本机 `gh` 凭据。源码包通过 HTTPS 从官方
源下载；elfutils 使用配置内的 SHA256，其余使用固定 commit URL并记录下载
SHA256，缓存复用前再次校验。实际版本信息在 `build-info.json` 中留档。

`sources.lock` 使用 JSON 数据格式，不是需要 `source` 执行的 shell 脚本。
libbpf/elfutils/zstd 的输入保持锁定；`bcc: "latest"` 则有意保留自动跟随
正式 release 的行为，并非完全固定的构建锁。每次解析后的确切版本写入
`build/source-lock.json`，成功产物的来源写入 `dist/build-info.json`。

**未来上游版本不保证免维护。** 脚本可以自动跟随 release；如果上游修改了
Android 补丁涉及的代码、libbpf API 或 BPF 结构，补丁/构建会明确失败，
不会静默跳过、反向应用补丁，也不会以旧源码冒充新版本。
下载、打补丁、编译、ELF 检查失败时保留上次成功的产物。

## 项目结构

```text
sources.lock           上游版本、依赖锁定、Android API（JSON）
CMakeLists.txt         独立 NDK 静态构建入口
Makefile               release / prepare / verify / test 入口
cmake/                 skeleton 生成、libelf 配置模板
compat/                Android argp、libintl 兼容层
patches/               BCC/libbpf 补丁与有序 series
scripts/               build-memleak.py：下载、打补丁、构建、验证
tests/                 构建脚本回归测试；native/ 为 QEMU 和真机工作负载
docs/                  迁移说明、设备用法及实测记录
.cache/                下载缓存（不提交）
sources/               按内容指纹隔离的已打补丁上游源码（不提交）
build/                 source-lock.json、编译目录及测试日志（不提交）
dist/                  成功产物 memleak、build-info.json、SHA256SUMS（不提交）
```

采用与 Platform-Tools 独立构建项目相同的职责分层：`sources/` 只放上游
输入，`build/android-arm64/<指纹>/` 放中间产物，`dist/` 放可交付文件。
项目只面向 Android arm64，因此 `dist/` 不再重复嵌套 `android-arm64/`。

修改补丁请编辑 `patches/`，不要直接修改缓存源码。源码、补丁、NDK
或构建规则变化会生成独立构建目录，避免旧 object 混入新版本。
旧版本目录保留用于排查；脚本不会清理用户指定的外部路径。

## Android 适配和验证边界

- 默认 allocator 为 `/system/lib64/libc.so`。
- `-O` 支持 `PATH_MAX` 长度的完整 APEX/HWASan 路径，超长明确失败。
- 保留 `-S` allocator 前缀、stack map/depth 配置、combined map 优化和无效
  stack id 处理：这些已经在上游 BCC 内，不重复维护一份旧实现。
- 使用本地维护的 Android argp 兼容层；用户态符号解析使用同一 BCC release
  的 `memleak.c` 和 helpers，不再混用旧 AOSP helper API。
- 年龄转换使用整数防溢出；libbpf 的目录 `open()` 调用适配 Bionic 检查。
- 默认不启用 Rust/blazesym，和原 Android arm64 路线一致。

```sh
adb push dist/memleak /data/local/tmp/memleak
adb shell chmod 0755 /data/local/tmp/memleak
# 在有 root 权限的设备 shell 中执行：
/data/local/tmp/memleak -p <PID> \
  -O /apex/com.android.runtime/lib64/bionic/hwasan/libc.so \
  --stack-storage-size 65536 -T 20 1
```

应根据目标进程 `/proc/PID/maps` 选择 allocator，不能假设所有进程都用
普通 Bionic libc。运行依赖设备 root/BPF/BTF/tracefs 能力及 SELinux 策略。
静态编译及 QEMU 参数测试通过不等于真机 verifier/uprobes 已验证，也不保证
上游 memleak 的所有边界行为（如失败的 realloc、部分 munmap）已经修复。

2026-09-05 的 NDK 产物已在 Android 16 / arm64 / Linux 6.6 真机上验证：
普通 Bionic 与 HWASan 的 malloc/free、`-C` 聚合、mmap/mremap/munmap 均捕获
到与受控分配/释放一致的数据，调用栈可解析。Camera Provider 仅验证了短时
挂载，未执行拍照负载。环境、产物校验值、命令和边界见
[本次真机验证记录](docs/device-validation-2026-09-05.md)。

详见 [设备使用说明与历史记录](docs/android-usage.md) 和
[迁移与补丁维护](docs/migration.md)。

## 测试

```sh
make test
make verify

# 可选：运行实际 Android 参数解析回归，不加载 BPF、不需要设备或 root
# 主机需安装 qemu-user
python3 scripts/build-memleak.py --offline --self-test
```

仓库构建/兼容代码使用 [Apache-2.0](LICENSE)；下载的第三方源码保留各自的
许可证（包括 BCC/libbpf、elfutils、zstd 及 NDK notices），不能仅按本仓库
顶层 LICENSE 理解所有静态链接组件。
