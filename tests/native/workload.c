/* SPDX-License-Identifier: Apache-2.0 */
/* Controlled device workload: bounded allocations, then explicit releases.
 * Compile dynamically with the NDK so allocator uprobes target device libc.
 * Run under memleak -c; MEMLEAK_TEST=mmap selects the mapping scenario.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

__attribute__((noinline)) void *allocate_32k(void)
{
    return malloc(32768);
}

__attribute__((noinline)) void *allocate_64k(void)
{
    return malloc(65536);
}

static int heap_test(void)
{
    void *held = allocate_32k();
    void *released = allocate_64k();
    if (!held || !released) {
        free(held);
        free(released);
        return 1;
    }
    memset(held, 0x32, 32768);
    memset(released, 0x64, 65536);
    puts("PHASE heap=98304");
    sleep(3);
    free(released);
    puts("PHASE heap=32768");
    sleep(3);
    free(held);
    puts("PHASE heap=0");
    sleep(3);
    return 0;
}

__attribute__((noinline)) static int mapping_test(void)
{
    void *mapping = mmap(NULL, 262144, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (mapping == MAP_FAILED)
        return 1;
    memset(mapping, 0x26, 262144);
    puts("PHASE mapping=262144");
    sleep(3);
    void *expanded = mremap(mapping, 262144, 524288, MREMAP_MAYMOVE);
    if (expanded == MAP_FAILED) {
        munmap(mapping, 262144);
        return 1;
    }
    puts("PHASE mapping=524288");
    sleep(3);
    if (munmap(expanded, 524288))
        return 1;
    puts("PHASE mapping=0");
    sleep(3);
    return 0;
}

int main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("WORKLOAD pid=%ld\n", (long)getpid());
    FILE *maps = fopen("/proc/self/maps", "r");
    if (maps) {
        char line[512];
        while (fgets(line, sizeof(line), maps)) {
            if (strstr(line, "/libc.so"))
                printf("LIBC %s", line);
        }
        fclose(maps);
    }
    sleep(2);
    const char *scenario = getenv("MEMLEAK_TEST");
    return scenario && strcmp(scenario, "mmap") == 0 ? mapping_test() : heap_test();
}
