# CrSDKPy native bridge.
#
# The Sony Camera Remote SDK is NOT distributed with CrSDKPy. Point this build
# at your own copy:
#
#   cmake -S native -B native/build -DCRSDK_ROOT="/path/to/CrSDK/RemoteCli"
#   cmake --build native/build --config Release
#
# CRSDK_ROOT must contain:
#   app/CRSDK/*.h                       vendor headers
#   external/crsdk/Cr_Core.{lib,so,dylib}   vendor import library
#
# The resulting shared library is loaded by ctypes at runtime. It is not a
# Python extension module and does not link against CPython, so one build
# serves every supported interpreter version.

cmake_minimum_required(VERSION 3.16)
# C as well as C++: the bridge is C++, the self-test is plain C, which also
# proves the public header really is C-consumable.
project(crsdkpy_bridge LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)

if(NOT DEFINED CRSDK_ROOT)
  message(FATAL_ERROR
    "CRSDK_ROOT is not set.\n"
    "The Sony Camera Remote SDK is user-supplied and is not bundled with "
    "CrSDKPy. Re-run with -DCRSDK_ROOT=/path/to/CrSDK/RemoteCli")
endif()

set(CRSDK_INCLUDE_DIR "${CRSDK_ROOT}/app/CRSDK")
if(NOT EXISTS "${CRSDK_INCLUDE_DIR}/CameraRemote_SDK.h")
  message(FATAL_ERROR
    "CameraRemote_SDK.h not found under ${CRSDK_INCLUDE_DIR}. "
    "Check that CRSDK_ROOT points at the SDK's RemoteCli directory.")
endif()

find_library(CRSDK_CORE_LIBRARY
  NAMES Cr_Core
  HINTS "${CRSDK_ROOT}/external/crsdk"
  REQUIRED)

add_library(crsdkpy_bridge SHARED src/bridge.cpp)

target_include_directories(crsdkpy_bridge
  PRIVATE include "${CRSDK_INCLUDE_DIR}" "${CRSDK_ROOT}/app")

target_compile_definitions(crsdkpy_bridge PRIVATE CRSDKPY_BUILDING)

if(WIN32)
  # Required, not cosmetic. The vendor's CrChar is wchar_t only when UNICODE is
  # defined; without it every vendor string is read as narrow char and
  # truncates at the first NUL byte of its UTF-16 data, so a model name comes
  # back as a single letter. The vendor's own samples define both.
  target_compile_definitions(crsdkpy_bridge PRIVATE UNICODE _UNICODE)
endif()
target_link_libraries(crsdkpy_bridge PRIVATE "${CRSDK_CORE_LIBRARY}")

if(MSVC)
  target_compile_options(crsdkpy_bridge PRIVATE /W3 /EHsc)
else()
  target_compile_options(crsdkpy_bridge PRIVATE -Wall -Wextra)
  find_package(Threads REQUIRED)
  target_link_libraries(crsdkpy_bridge PRIVATE Threads::Threads)
endif()

# Predictable name on every platform: crsdkpy_bridge.{dll,so,dylib}
set_target_properties(crsdkpy_bridge PROPERTIES PREFIX "" OUTPUT_NAME "crsdkpy_bridge")

# The host executable. Its whole purpose is to sit beside the vendor runtime,
# because the SDK resolves its adapter directory against the executable's
# location. It also isolates vendor code from the caller's process.
add_executable(crsdkpy_host host/main.cpp)
target_include_directories(crsdkpy_host PRIVATE include host)
target_link_libraries(crsdkpy_host PRIVATE crsdkpy_bridge)
set_target_properties(crsdkpy_host PROPERTIES OUTPUT_NAME "crsdkpy_host")
if(WIN32)
  target_compile_definitions(crsdkpy_host PRIVATE UNICODE _UNICODE)
endif()

# Optional C self-test. Exercises the ABI without Python, which keeps ctypes
# and interpreter behaviour out of any bug hunt.
option(CRSDKPY_BUILD_SELFTEST "Build the native self-test executable" ON)
if(CRSDKPY_BUILD_SELFTEST)
  add_executable(crsdkpy_selftest test/bridge_selftest.c)
  target_include_directories(crsdkpy_selftest PRIVATE include)
  target_link_libraries(crsdkpy_selftest PRIVATE crsdkpy_bridge)
endif()
