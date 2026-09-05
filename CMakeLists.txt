cmake_minimum_required(VERSION 3.22)
project(memleak_android LANGUAGES C CXX ASM)

if(NOT ANDROID OR NOT CMAKE_ANDROID_ARCH_ABI STREQUAL "arm64-v8a")
  message(FATAL_ERROR "Use scripts/build-memleak.py with an Android NDK (arm64-v8a)")
endif()
foreach(required BCC_SOURCE LIBBPF_SOURCE ELFUTILS_SOURCE ZSTD_SOURCE BPFTOOL ELFUTILS_VERSION)
  if(NOT DEFINED ${required})
    message(FATAL_ERROR "Missing ${required}")
  endif()
endforeach()

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
add_compile_options(-O2 -g -ffunction-sections -fdata-sections)

# Only libelf is needed. Do not build libdw, debuginfod, argp/obstack tools,
# or the rest of BCC/LLVM/Python. Keep zstd support for compressed ELF files.
set(ZSTD_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_STATIC ON CACHE BOOL "" FORCE)
add_subdirectory("${ZSTD_SOURCE}/build/cmake" zstd EXCLUDE_FROM_ALL)

include(CheckSymbolExists)
set(CMAKE_REQUIRED_DEFINITIONS -D_GNU_SOURCE)
check_symbol_exists(mempcpy "string.h" HAVE_DECL_MEMPCPY)
check_symbol_exists(memrchr "string.h" HAVE_DECL_MEMRCHR)
check_symbol_exists(reallocarray "stdlib.h" HAVE_DECL_REALLOCARRAY)
check_symbol_exists(powerof2 "sys/param.h" HAVE_DECL_POWEROF2)
configure_file(cmake/elfutils-config.h.in elfutils/config.h @ONLY)
file(GLOB elf_sources CONFIGURE_DEPENDS "${ELFUTILS_SOURCE}/libelf/*.c")
list(FILTER elf_sources EXCLUDE REGEX "_check\\.c$")
add_library(elf STATIC ${elf_sources} "${ELFUTILS_SOURCE}/lib/eu-search.c")
target_compile_definitions(elf PRIVATE HAVE_CONFIG_H _GNU_SOURCE _FILE_OFFSET_BITS=64)
target_include_directories(elf PUBLIC "${ELFUTILS_SOURCE}/libelf" PRIVATE
  "${CMAKE_CURRENT_BINARY_DIR}/elfutils" "${CMAKE_CURRENT_SOURCE_DIR}/compat"
  "${ELFUTILS_SOURCE}/lib" "${ZSTD_SOURCE}/lib")
target_link_libraries(elf PUBLIC libzstd_static z)

# Read the official object's list instead of maintaining another libbpf list.
file(READ "${LIBBPF_SOURCE}/src/Makefile" libbpf_make)
string(REPLACE "\\\n" " " libbpf_make "${libbpf_make}")
string(REGEX MATCH "\nOBJS := ([^\n]+)" libbpf_objects "${libbpf_make}")
if(NOT libbpf_objects)
  message(FATAL_ERROR "libbpf Makefile changed: cannot identify static library sources")
endif()
string(REGEX MATCHALL "[a-z_0-9]+\\.o" libbpf_objects "${CMAKE_MATCH_1}")
set(libbpf_sources)
foreach(object IN LISTS libbpf_objects)
  string(REGEX REPLACE "\\.o$" ".c" source "${object}")
  list(APPEND libbpf_sources "${LIBBPF_SOURCE}/src/${source}")
endforeach()
add_library(bpf STATIC ${libbpf_sources})
target_compile_definitions(bpf PRIVATE _GNU_SOURCE _LARGEFILE64_SOURCE
  _FILE_OFFSET_BITS=64 __user= __force=)
target_include_directories(bpf PRIVATE "${LIBBPF_SOURCE}/src"
  "${LIBBPF_SOURCE}/include" "${LIBBPF_SOURCE}/include/uapi")
target_link_libraries(bpf PUBLIC elf)

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/include")
file(CREATE_LINK "${LIBBPF_SOURCE}/src" "${CMAKE_CURRENT_BINARY_DIR}/include/bpf" SYMBOLIC)
set(bcc_tools "${BCC_SOURCE}/libbpf-tools")
set(bpf_object "${CMAKE_CURRENT_BINARY_DIR}/memleak.bpf.o")
set(skeleton "${CMAKE_CURRENT_BINARY_DIR}/memleak.skel.h")
get_filename_component(ndk_bin "${CMAKE_C_COMPILER}" DIRECTORY)
add_custom_command(OUTPUT "${bpf_object}"
  COMMAND "${ndk_bin}/clang" --target=bpfel -D__TARGET_ARCH_arm64 -std=gnu17
    -O2 -g -Wall -Werror -I "${CMAKE_CURRENT_BINARY_DIR}/include"
    -I "${bcc_tools}/arm64" -I "${bcc_tools}"
    -c "${bcc_tools}/memleak.bpf.c" -o "${bpf_object}"
  DEPENDS "${bcc_tools}/memleak.bpf.c" "${bcc_tools}/memleak.h"
    "${bcc_tools}/arm64/vmlinux.h"
  VERBATIM)
add_custom_command(OUTPUT "${skeleton}"
  COMMAND "${CMAKE_COMMAND}" -DBPFTOOL=${BPFTOOL} -DOBJECT=${bpf_object}
    -DOUTPUT=${skeleton} -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/skeleton.cmake"
  DEPENDS "${bpf_object}" "${CMAKE_CURRENT_SOURCE_DIR}/cmake/skeleton.cmake"
  VERBATIM)

add_executable(memleak "${bcc_tools}/memleak.c" "${bcc_tools}/trace_helpers.c"
  "${bcc_tools}/uprobe_helpers.c" "${bcc_tools}/btf_helpers.c"
  compat/argp.cpp "${skeleton}")
target_include_directories(memleak PRIVATE compat "${bcc_tools}"
  "${CMAKE_CURRENT_BINARY_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/include")
target_compile_definitions(memleak PRIVATE _GNU_SOURCE)
target_compile_options(memleak PRIVATE -Wall -Werror
  -Wno-unused-parameter -Wno-missing-field-initializers)
target_link_libraries(memleak PRIVATE bpf)
target_link_options(memleak PRIVATE -static -Wl,--gc-sections -Wl,--build-id=sha1
  -Wl,-z,max-page-size=16384)

# A userspace-only regression harness; never loads BPF or attaches probes.
add_executable(memleak-options-test EXCLUDE_FROM_ALL
  "${CMAKE_CURRENT_SOURCE_DIR}/tests/native/options.c" compat/argp.cpp
  "${bcc_tools}/trace_helpers.c" "${bcc_tools}/uprobe_helpers.c"
  "${bcc_tools}/btf_helpers.c" "${skeleton}")
target_include_directories(memleak-options-test PRIVATE compat "${bcc_tools}"
  "${CMAKE_CURRENT_BINARY_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/include")
target_compile_definitions(memleak-options-test PRIVATE _GNU_SOURCE)
target_compile_options(memleak-options-test PRIVATE -Wall -Werror
  -Wno-unused-parameter -Wno-missing-field-initializers)
target_link_libraries(memleak-options-test PRIVATE bpf)
target_link_options(memleak-options-test PRIVATE -static -Wl,--gc-sections
  -Wl,-z,max-page-size=16384)
