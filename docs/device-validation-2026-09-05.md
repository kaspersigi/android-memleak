# 独立 NDK 产物真机验证：2026-09-05

本记录对应这次重构后的静态产物，不沿用旧 AOSP binary 的验证结论。
未重启设备或 Camera Provider，未修改 SELinux、系统分区或安全配置。

## 环境与产物

- 设备：Sun for arm64，Android 16 / SDK 36，arm64-v8a。
- 内核：`6.6.77-android15-8-maybe-dirty-debug`。
- 已有 root adbd；SELinux 全程 `Enforcing`。
- `/sys/kernel/btf/vmlinux` 和 `/sys/kernel/tracing` 可用。
- 产物：`out/android-arm64/memleak`，1,082,808 字节，全静态 ARM64 ELF，16 KiB 对齐。
- BCC：`v0.37.0`，commit `306a2819f73d9525430693e6399d58caf6e12b3b`。
- 工具链：NDK r27d，Android API 35；未链接 AOSP 构建的 `.a` / `.o`。
- 本地与推送到设备的 memleak SHA256 一致：

```text
97c31db66a3991050ac0830c6fb1b7e84fd7949d7b80d008b5fb2cb445c983fc
```

以上路径和校验值保留测试当时的事实。后续目录对齐后，新产物改为
`dist/memleak`，本记录不能替代对后续重新构建产物的校验。

## 实测结果

以下字节数是每次报告中各调用栈的未释放字节数之和；测试用大小过滤隔离
工作负载自身的固定分配。每个正向测试均包含分配、部分释放/扩容、最终释放，
不是仅以启动成功或退出码判断。memleak 均正常退出，未触发超时兜底。

| 场景 | 预期变化 | 实际结果 |
| --- | --- | --- |
| 普通 Bionic malloc/free | 96 KiB → 32 KiB → 0 | 一致；两处分配调用栈可解析 |
| HWASan Bionic malloc/free | 96 KiB → 32 KiB → 0 | 一致；两处分配调用栈可解析 |
| 普通 Bionic `-C` 聚合 | 96 KiB → 32 KiB → 0 | 一致；释放后字节数、分配数均为 0 |
| 普通 Bionic mmap/mremap/munmap | 256 KiB → 512 KiB → 0 | 一致；初次映射及扩容调用栈可解析 |
| 负对照：普通进程指定 HWASan libc | 无法捕获该进程分配 | 全程 0，证明必须核对目标进程 maps |
| Camera Provider / HWASan libc | BPF 加载与短时 uprobe 挂载 | 成功；空闲期间两次报告为 0，未触发拍照 |

堆测试可解析出 `allocate_32k`、`allocate_64k`、`heap_test`、`main` 和
`__libc_init`；映射测试可解析出 `mapping_test`。`-C` 会保留已清零的调用栈
条目，因此最终仍可能显示 `Top 2 stacks`，但两条记录均为 `0 bytes in 0 allocations`，
不代表仍有泄漏。

## 工作负载与执行方法

受控源码保留在 [tests/native/workload.c](../tests/native/workload.c)。它先等待
2 秒，再分三个阶段分配/释放，每阶段保持 3 秒。默认执行堆测试；环境变量
`MEMLEAK_TEST=mmap` 选择映射测试。运行时打印 PID 和实际映射的 libc。

在仓库根目录编译两个动态链接测试程序（被测 memleak 本身仍为全静态）：

```sh
export ANDROID_NDK_HOME=/path/to/android-ndk-r27d
mkdir -p build/device-validation
"$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android35-clang" \
  -O0 -g -fno-omit-frame-pointer -fno-optimize-sibling-calls -fno-builtin \
  -rdynamic -Wall -Wextra -Werror tests/native/workload.c \
  -o build/device-validation/a
"$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android35-clang" \
  -O0 -g -fno-omit-frame-pointer -fno-optimize-sibling-calls -fno-builtin \
  -rdynamic -Wall -Wextra -Werror -fsanitize=hwaddress tests/native/workload.c \
  -o build/device-validation/h
```

两次编译均无 warning/error。HWASan 测试使用设备已有的 sanitizer runtime；
其他设备需满足 [NDK HWASan 运行条件](https://developer.android.com/ndk/guides/hwasan)。

本次使用设备临时目录 `/data/local/tmp/amem.d7vHI5`，只推送 `memleak`、`a`、`h`。
下面是当时的 root shell 命令；该临时目录现已清理，复测需重新创建专用临时
目录并推送文件、设置执行权限。`-c` 的上游命令缓冲区目前只有 32 字节，
应使用短路径（命令总长度不超过 31 字节），不能把 `-O` 的长路径支持套用到 `-c`。

```sh
# 普通 Bionic：默认 /system/lib64/libc.so 与该测试进程的 libc 是同一 ELF。
timeout -s INT -k 3 25 /data/local/tmp/amem.d7vHI5/memleak \
  -c /data/local/tmp/amem.d7vHI5/a \
  --stack-storage-size 4096 --perf-max-stack-depth 32 \
  -z 32768 -Z 65536 -o 1 -T 5 1 16

# HWASan：测试程序 h 的 maps 确认使用下面的 allocator。
timeout -s INT -k 3 25 /data/local/tmp/amem.d7vHI5/memleak \
  -c /data/local/tmp/amem.d7vHI5/h \
  -O /apex/com.android.runtime/lib64/bionic/hwasan/libc.so \
  --stack-storage-size 4096 --perf-max-stack-depth 32 \
  -z 32768 -Z 65536 -o 1 -T 5 1 16

# 聚合模式。
timeout -s INT -k 3 25 /data/local/tmp/amem.d7vHI5/memleak \
  -C -c /data/local/tmp/amem.d7vHI5/a \
  --stack-storage-size 4096 --perf-max-stack-depth 32 \
  -z 32768 -Z 65536 -T 5 1 16

# 映射、扩容和完整解除映射。
MEMLEAK_TEST=mmap timeout -s INT -k 3 25 /data/local/tmp/amem.d7vHI5/memleak \
  -c /data/local/tmp/amem.d7vHI5/a \
  --stack-storage-size 4096 --perf-max-stack-depth 32 \
  -z 262144 -Z 524288 -o 1 -T 5 1 16

# 当时 Camera Provider 的 PID 为 13334；复测需重新查 PID 和 maps。
timeout -s INT -k 3 15 /data/local/tmp/amem.d7vHI5/memleak \
  -p 13334 -O /apex/com.android.runtime/lib64/bionic/hwasan/libc.so \
  --stack-storage-size 4096 --perf-max-stack-depth 32 -T 3 1 2
```

负对照使用 HWASan 命令，但把 `-c` 的目标从 `h` 换成 `a`。即使这个命令
正常退出且没有必需探针挂载错误，它也无法追踪普通 libc 的分配。

## 提示、边界与清理

每次运行均有 `valloc` / `pvalloc` 符号地址为 0 的 libbpf 提示，各两条
（入口和返回探针）。这两个探针在上游 Android/Bionic 路径中为可选探针；
上述必需探针成功挂载，实际采样结果正常。此次未隐藏或修改这些提示。

尚未覆盖内核分配模式、所有 allocator API、失败的 realloc/mremap、部分
munmap、长期高负载、Camera 拍照业务或其他设备/内核。不能据此宣布上游
全部边界问题已修复，也不能保证所有 root 设备都允许 BPF。

六份原始日志已拉回本机 `build/device-validation/`：`heap-default.log`、
`heap-hwasan.log`、`heap-combined.log`、`mmap.log`、`heap-wrong-object.log`、
`camera.log`。该目录属于忽略的本地测试输出，不随 Git 提交。
父子进程输出共用日志，个别阶段标记与调用栈文本交错，数值仍可按报告汇总。

已清理本次设备专用临时目录及其文件，本地日志和测试 ELF 保留；未触碰设备
原有 `/data/local/tmp/memleak`。最后确认无测试进程残留，SELinux 仍为
`Enforcing`，Camera Provider PID 仍为 `13334`。
