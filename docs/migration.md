# 从 BCC/AOSP fork 到 android-memleak

## 结构迁移

旧源码基线为提交 `3482129`（移植实现 `f97ee4a`）。原 BCC 源码、Python
工具集、AOSP Android.bp 和旧打包文件从主工作树移除，仍在 Git 历史中。
查看旧实现可用 `git show 3482129:libbpf-tools/memleak.c`；不要再把新仓库
整体复制到 AOSP external/bcc。

项目统一使用 `android-memleak` 名称；本地检出目录可以继续叫 `bcc`，
GitHub 仓库重命名不要求本地目录同步改名。
更改目录名后，脚本会选择新的 CMake 构建目录；版本配置和补丁不含
`/mnt/develop`、AOSP 或某个 checkout 的硬编码路径。

随后按 Platform-Tools 独立构建项目对齐目录职责：

| 首次 NDK 重构路径 | 当前路径 |
| --- | --- |
| `android/versions.json` | `sources.lock`（仍为 JSON） |
| `android/CMakeLists.txt` | `CMakeLists.txt` |
| `android/skeleton.cmake`、libelf 配置模板 | `cmake/` |
| `android/compat/` 中的兼容源码 | `compat/` |
| `android/patches/` | `patches/` |
| `scripts/build_memleak.py` | `scripts/build-memleak.py` |
| `tests/android/test_build.py` | `tests/test_build.py` |
| `build/android-memleak/sources/` | `sources/` |
| `build/android-memleak/builds/` | `build/android-arm64/` |
| `out/android-arm64/` | `dist/` |

`make` 入口保持不变，NDK、目标 ABI/API、上游版本和补丁内容不因迁移而改变。
已下载的 `.cache/android-memleak/` 继续复用；旧 `build/android-memleak/`、
`out/` 保留供本机核对，新脚本不再读写它们，也不自动删除它们。
源码从校验后的缓存重新解包到 `sources/`，不会直接复用旧 CMake 构建树。

## 保留了什么

| 原有改动 | 新流程中的来源 |
| --- | --- |
| posix_memalign key 修正、MAP_FAILED 过滤、mremap、combined map 优化 | 上游 BCC v0.37.0 |
| 无效 stack id、map/depth 参数、allocator 符号前缀 | 同版本上游 memleak |
| uprobe_helpers 文件释放修正 | 同版本上游 helper，已含修正 |
| 默认 Bionic libc、PATH_MAX、长 HWASan 路径检查 | BCC 补丁 0001 |
| 年龄数值范围保护 | 上游保护 + 补丁 0002 的精确整数计算 |
| 非法参数不能返回成功 | BCC 补丁 0003 |
| Android argp 参数/帮助输出 | 本地 compat/argp，增加 GNU group 字段和 -h 支持 |
| ELF/用户态符号解析 | 同版本上游 trace_helpers + NDK 编译 libelf |
| 全静态链接 | 独立 CMake + 同一套 NDK runtime |

旧 AOSP blazesym API 回退不再迁入；这一路原先在 Android 构建中未启用。
也不复制旧 helper 的函数签名，以免限制今后跟随上游。

## 新版本补丁维护

1. 在版本配置中指定新 release，执行 `make prepare`。
2. 如果补丁冲突，对比新上游对应实现；已被上游吸收的补丁需要人工确认后
   从 series 移除或缩小，而不是让脚本自动忽略失败。
3. 在独立临时源码副本上修改，用统一 diff 更新对应补丁；保留上游版权说明。
4. 执行完整 NDK 构建、`make test`、`make verify`，最后在目标 Android 内核
   实测 BPF 加载、普通/HWASan libc 分配追踪及符号化。

依赖版本独立锁定：升级 BCC 不会隐式同时升级 libbpf/elfutils/zstd。
libbpf 使用官方 Makefile 的 object 清单；libelf 只构建库本身及 eu-search，
不构建 libdw、debuginfod、命令行工具及无关的 gnulib 组件。

## 缓存和失败恢复

源码包、已应用补丁的源码及构建目录分离。缓存源码内容修改会被拒绝，
请将试验改动整理为补丁，再把明确报错的缓存目录移开后重试。
更新失败不会覆盖上次成功的 memleak；应检查 build-info.json 的实际版本，
不能因为旧文件仍在就认为新版本构建成功。

整个工作流只读官方上游，不提交、不创建 tag、不 push，也不修改 AOSP 或 NDK。

## 目录对齐验证

2026-09-05 在仓库外的 `/tmp` 调用新入口，使用 NDK r27d、原版本配置及
已校验下载缓存完成全量离线构建，输出 `dist/memleak`；构建日志无 warning/error。
34 项 Python 测试、13 项 Android/QEMU 参数测试及 `make verify` 通过。
迁移前后 `build-info.json` 的 `sources`、`toolchain` 完全一致，确认未变更
上游版本、依赖或补丁内容。此次目录对齐没有重复实机测试，先前真机记录及
旧产物保留，不能把旧产物的 SHA256 当作新产物校验值。
