# CrSDKPy native bridge and host

A thin C++ shared library exposing a strict C ABI over the Sony Camera Remote
SDK, plus a small host executable that runs it out of process. Python loads
either with `ctypes`, so neither is a Python extension module and neither links
against CPython: one build serves every supported interpreter version.

```
Sony CRSDK C++  ->  bridge  ->  strict C ABI  ->  crsdkpy_host  ->  pipe  ->  CrSDKPy
```

**The hosted path is the supported way to talk to a camera** and is what
`SDK(backend="host")` uses. The bridge can also be loaded directly in process
via `SDK(backend="native")`; that is a lower-level, diagnostic path, and it is
subject to the adapter-directory constraint described below, which is precisely
why the host exists.

The Sony Camera Remote SDK is **not** distributed with CrSDKPy. You supply your
own copy under Sony's terms.

## Layout

| Path | Contents |
|---|---|
| `include/crsdkpy_abi.h` | The entire C contract. No vendor types, no C++. |
| `src/bridge.cpp` | Implementation: RAII over vendor objects, event queue. |
| `host/ipc_protocol.h` | The wire format between the host and Python. |
| `host/main.cpp` | The host executable that owns the vendor SDK. |
| `test/bridge_selftest.c` | Plain-C self-test; also proves the header is C-consumable. |

## Building

```console
cmake -S native -B native/build -DCRSDK_ROOT=/path/to/CrSDK/RemoteCli
cmake --build native/build --config Release
```

`CRSDK_ROOT` must contain `app/CRSDK/*.h` and
`external/crsdk/Cr_Core.{lib,so,dylib}`. The build fails with an explanatory
message if it does not.

This produces `crsdkpy_bridge`, `crsdkpy_host` and `crsdkpy_selftest`.

Then copy the vendor runtime (`Cr_Core`, `CrAdapter/`, and any
`monitor_protocol*` libraries) next to the built **host executable** — that
placement is what satisfies the constraint below — and point CrSDKPy at it if
it is not in a default location:

```console
set CRSDKPY_HOST=...\native\build\crsdkpy_host.exe
```

`CRSDKPY_BRIDGE` does the same for the lower-level in-process backend.

## Adapter discovery: a real constraint, measured

**The vendor SDK resolves its transport-adapter directory against the host
executable's directory.** Not the working directory, and not the directory of
the DLL that calls into it.

This was established by elimination, not assumption:

| Configuration | `EnumCameraObjects` |
|---|---|
| Self-test **executable** beside `CrAdapter/` | success |
| Same executable run from an unrelated working directory | success |
| Same bridge loaded by an interpreter **not** beside `CrAdapter/` | `0x8703` adaptor-create |
| ...with the working directory set to the adapter directory | `0x8703` |
| ...with the adapter directory added via `SetDllDirectory` | `0x8703` |
| ...with `CrAdapter/` placed beside the interpreter binary | **success** |

Only the last row fixes it, which is consistent with the SDK building an
absolute path from the host executable's location. Both plausible in-process
mitigations were implemented, measured, and removed once they proved
ineffective.

Consequences:

* The vendor's own sample programs never hit this, because they ship beside
  their adapters.
* A library cannot dictate where the host interpreter lives, so this cannot be
  papered over inside the bridge.

### How this is resolved

`crsdkpy_host` is the answer, and it is implemented. The vendor SDK is moved
out of the interpreter into a first-party executable that sits beside the
runtime, and Python speaks to it over the same POD contract across the child's
stdin/stdout pipes. That removes the constraint entirely, because the
executable the vendor resolves against is now one CrSDKPy controls.

It also buys process isolation, which was not the original motivation but
matters just as much: vendor code runs somewhere replaceable, so a native fault
surfaces as a backend error instead of taking the interpreter with it. One such
fault was observed during hardware characterization.

The remaining alternatives, for the in-process backend only:

1. Run from an application whose executable already sits beside the vendor
   runtime, which is the normal deployment shape for a packaged app.
2. Place `CrAdapter/` and the vendor libraries beside the interpreter binary.
   CrSDKPy never does this to a user's Python installation.

When the constraint does bite, `crsdkpy_last_error` explains exactly this
rather than leaving a bare vendor code.

## Hardware validation

The current status of every feature, and which have actually been exercised
on a camera, is tracked in [`../docs/FEATURE_MATRIX.md`](../docs/FEATURE_MATRIX.md).
What follows is the first validated round.

Validated against an ILME-FX3A (firmware 2.02, CRSDK 2.02.00, USB) through the
**public Python API**, not a private path:

| Check | Result |
|---|---|
| Discovery | 1 camera, `ILME-FX3A`, USB, `Cr_PTP_USB`, PID `0x0F52` |
| Camera identity stable across rediscovery | yes, same object and key |
| Session in `remote` | connected, **394** properties |
| Session in `remote_transfer` | connected, **392** properties |
| Named property values | 22 of 22 correct, cross-checked against characterization |
| Events | connection plus 382 coalesced property notifications |
| Idempotent close, camera outliving both sessions | yes |

The 394/392 split is the characterized mode-dependent property count,
reproduced end to end through the production API on real hardware.
`PriorityKeySettings` is absent from the FX3A property set, independently
confirming that characterization finding.

Two defects were found and fixed by this validation:

* **Missing `UNICODE` define.** The vendor's `CrChar` is `wchar_t` only when
  `UNICODE` is defined. Without it every vendor string is read as narrow
  `char` and truncates at the first NUL of its UTF-16 data, so `ILME-FX3A`
  arrived as `I`. The build now defines it on Windows.
* **`open_session` returned too early.** The connection callback fires before
  the initial property load completes, so a snapshot taken immediately held 8
  codes instead of 394. The bridge now waits for the property burst to go
  quiet, bounded, so returning from `open_session` genuinely means usable.

## Self-test

Runs the ABI from C, with no Python involved, which keeps `ctypes` and
interpreter behaviour out of any bug hunt:

```console
./native/build/crsdkpy_selftest [adapter_dir]
```

With no camera attached it should report `enumerate: 0, cameras=0`. With one
attached it also opens a session, reads the property count, drains events and
closes twice to confirm idempotency.

## Design rules

These come from observed vendor behaviour during hardware characterization:

* Vendor objects are owned by RAII wrappers and released exactly once.
* Vendor-owned arrays are copied out **before** the owning list is released.
  Reusing a handle after releasing its list is an access violation; this was
  observed crashing a probe.
* Vendor callbacks arrive on vendor threads and only push into a
  mutex-protected queue. Nothing calls back into Python.
* Session handles embed a generation counter, so a stale handle is rejected
  rather than aliasing a newer session.
* `crsdkpy_close_session` and `crsdkpy_shutdown` are idempotent.
* The event queue is bounded; a client that stops polling drops the oldest
  events rather than growing without limit.

## ABI versioning

`crsdkpy_abi_version()` returns `(major << 16) | minor`. Python refuses to load
a library whose major version it does not recognise. Bump the major component
for any layout or signature change.
