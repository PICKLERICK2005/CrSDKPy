# 0.1.0b4 event correctness and metadata repair — release candidate notes

Prepared on `fix/a7iv-focus-event-queue` from `9bce8c2`. The original checkout,
installed dependency, SPTFS, PyKS2 and historical records remain unchanged.
The earlier candidate and regression evidence are retained in
`A7IV_FOCUS_EVENT_QUEUE_REVIEW.md`; this revision adds native provenance and
call boundaries rather than treating an empty event poll as proof of freshness.

## Event correction

The bounded pre-S1 drain separates already-queued observations from the next
attempt. Same-batch focus selection considers the most recent state from each
independent channel, not the first focused notification. Warning/content and
other operation events are retained. Pre-S2 CaptureEvents stay in the public
stream for reconciliation but cannot confirm the subsequent release.
The gate also collects immediately available IPC pages before evaluating a
focus verdict, so a contradiction already waiting in the next page is included.
Continuous event production is bounded and rejects the gate with S1 cleanup;
the SDK never loops indefinitely waiting for a quiet queue or issues S2 anyway.

Native callback enqueue now assigns timestamps and unsigned 32-bit sequence
numbers under the queue mutex. The event's existing reserved word carries
sequence bits; its size stays 40 bytes. `Event.native` exposes immutable
`NativeEventInfo` with original time, order and raw vendor fields. Existing
`timestamp_ms` remains the backend delivery/observation time, so previous
latency calculations keep their clock domain. Event constructors, including
positional arguments, are unchanged; provenance does not affect equality.
Dataclass serializers now include the optional `native` diagnostic field, so
strict consumers of serialized event dictionaries should permit this additive
field. Manufactured/custom-backend events can leave it unset.

Additive timed native entry points return a 16-byte watermark immediately before
the existing SDK::SetDeviceProperty or SDK::SendCommand invocation. S1 is still
the existing property write; release remains the same DOWN/dwell/UP pair. The
host appends the watermark to the response metadata without changing the fixed
request/response layouts. No extra camera query, command, write or delay is
introduced by the native watermark. Cumulative drop counts are diagnostic;
they do not authorize replay of a lost exposure.

The focus gate and exposure wait compare callback enqueue order/time with the
corresponding S1/S2 watermark. Native times are never compared with Python times.
Sequence ordering uses modulo arithmetic, rejecting ambiguous distances at or
beyond half the counter range. Compare only within the same active session.
Simulator/custom backends without this optional provenance retain the queue
guards but cannot offer the native ordering guarantee.

### Remaining limits

The boundary records entry into an SDK call, not successful camera application
or physical S1/S2 actuation. Callback enqueue time is not a vendor-supplied
camera-generation timestamp. Vendor-delayed callbacks can arrive after a newer
boundary even when the underlying camera event is older. Separate IPC batches
can contain a later contradictory state that was not observable when a verdict
was returned. No absolute freshness or physical actuation claim is made.
The public CaptureEvent.sequence field is unchanged; the new callback-order
number is Event.native.sequence. Neither field identifies a photographic file.

## Separate metadata correction

PROPERTY_STRING and PROPERTY_VALUES now both obtain the property code from
request.u32_arg, matching Python and the protocol's existing contract. There
is no dual-field workaround or camera-specific special case. Nonzero string,
enum and range requests have independent regression tests. The C++ protocol
test validates the shared native field accessor with conflicting signed fields
and high-bit unsigned codes; Python fake-host tests validate value recovery.
Real-camera metadata recovery on the rebuilt binary remains a hardware check.

## Native compatibility and deployment

- Python package candidate: **0.1.0b4**, not stable or hardware accepted.
- Native ABI **1.6**: two added timed entry points and one added boundary POD;
  old entry points and POD sizes remain intact. New Python requires this minor.
- IPC **1.3**: additive response tails are required for command/property
  provenance; older hosts are rejected during handshake. Existing clients can
  ignore the tails when using a new host. No header/request/response size changes.
- Build and stage the matching `crsdkpy_host.exe` and `crsdkpy_bridge.dll`
  together beside the operator-supplied Sony runtime. Do not replace only the
  executable. The native build remains independent of the pure-Python wheel.
- Vendor runtime files, binaries and private diagnostics must not enter the
  Python distribution. No production install, publish, push, merge or tag has
  been performed.

Hardware-free native tests are registered with CTest. The existing general
hardware selftest is disabled in this review build and must not be run without
separate authorization. Independently check the built bridge ABI/exports and
real host handshake/start/shutdown without discovery or opening sessions.

## Hardware evidence and next acceptance

Operator-reported baseline A7 IV electronic-shutter/35 ms results include 12/12
and 36/36 turntable captures plus one recovery frame, with 49/49 originals
ingested. A 200 ms hold is therefore not universally necessary. Mechanical
NR-On failures and improved NR-Off results do not establish the complete
readiness mechanism; this patch does not disable NR or alter exposure choices.

Before adopting this candidate, separately authorize a read-only rebuilt-host
metadata/provenance check, then a stationary one-shot AF-S test using the
existing 35 ms default. Review its complete event/command/content evidence before
any second shot. Follow with deliberate pre-queued-event and focus-rejection
acceptance without blindly repeating an uncertain logical shot. Test MF/S2 and
interruption/no-reshoot separately. Long exposures and NR-On processing need a
measured readiness/timeout workstream; no fixed sleep or setting rewrite is a fix.

## Final review validation

- Full Python suite with the rebuilt bridge available: **560 passed, 1 skipped**.
  The skip is the deliberately absent-bridge scenario; a bridge is present here.
- CTest: **2 passed** (C ABI/header layouts and C++ metadata protocol contract).
- Full Ruff over src/tests/tools, compileall and git diff --check: passed.
- Independent rebuilt-host startup/shutdown used initialization only, without
  enumeration, session opening or camera commands. Timed C ABI functions reject
  invalid handles. Native build hashes and detailed results are retained in the
  ignored `native/build/independent-validation.json`.
- New Python rejects the installed ABI 1.5 / IPC 1.2 binaries with a rebuild
  instruction. Old Python 0.1.0b3 starts/stops the new host and binds the retained
  legacy C entry points on ABI 1.6.
- The 0.1.0b4 wheel and sdist build without installation. Inspection excludes
  native build outputs, proprietary runtime libraries and private diagnostics.
  The sanitized `docs/characterization` reference remains legitimate source
  documentation; it is not the excluded top-level private capture directory.

### Files for review

Event handling and provenance:

- `src/crsdkpy/session.py`
- `src/crsdkpy/events.py`
- `src/crsdkpy/backend/native.py`
- `src/crsdkpy/backend/host.py`
- `src/crsdkpy/backend/_cabi.py`
- `src/crsdkpy/backend/_ipc.py`
- `native/src/bridge.cpp`
- `native/include/crsdkpy_abi.h`
- `native/host/ipc_protocol.h`
- `tests/fake_host.py`
- `tests/test_focus_event_queue.py`
- `tests/test_native_event_boundaries.py`

The independent metadata fix is the PROPERTY_STRING/PROPERTY_VALUES dispatch
change in `native/host/main.cpp`, its shared u32-code accessor in
`native/host/ipc_protocol.h`, and `tests/test_metadata_ipc_contract.py`.
It shares no autofocus sequencing or settings workaround.

Build/release and native contracts:

- `native/CMakeLists.txt`
- `native/test/protocol_contract_test.cpp`
- `native/test/abi_layout_test.c`
- `src/crsdkpy/__init__.py`
- `CHANGELOG.md`
- `docs/A7IV_FOCUS_EVENT_QUEUE_REVIEW.md`
- `docs/AF_EVENT_CORRECTNESS_RELEASE.md`
