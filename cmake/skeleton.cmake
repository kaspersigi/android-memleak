# SPDX-License-Identifier: Apache-2.0
execute_process(COMMAND "${BPFTOOL}" gen skeleton "${OBJECT}"
  OUTPUT_FILE "${OUTPUT}.tmp" RESULT_VARIABLE status)
if(NOT status EQUAL 0)
  file(REMOVE "${OUTPUT}.tmp")
  message(FATAL_ERROR "bpftool gen skeleton failed: ${status}")
endif()
file(RENAME "${OUTPUT}.tmp" "${OUTPUT}")
