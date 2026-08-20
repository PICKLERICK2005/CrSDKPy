# FX3 Characterization — CRSDK v2.02.00

**Hardware:** Sony ILME-FX3A  
**SDK:** CRSDK v2.02.00 (Win64)  
**Transport:** USB (libusbK driver)  
**Host:** Windows 11, Python 3.13, MSVC 2022  
**Date:** 2026-08-13  
**Status:** Phase 0–2 complete. AF/MF complete. Control mode matrix complete.

Measured on hardware unless labeled *inferred* or *doc-only*.

---

## Phase 0 — Environment baseline

| Item | Value |
|---|---|
| SDK version | 2.02.00 |
| SDK serial | recorded in fx3_characterization.json |
| USB driver | libusbK (srcameradriver.inf, amd64) |
| Camera model | ILME-FX3A (`CrCameraDeviceModel_ILME_FX3A`) |
| FX3A firmware | 2.02 (reported from camera) |
| USB PID | `0x0F52` |
| EnumCameraObjects timeout used | 3 s |
| Probe language | C++ (MSVC, /std:c++17) via SimpleCli headers |

The Sony driver binds the FX3 via libusbK rather than the Windows WIA/PTP stack.
In Device Manager the camera appears under **libusbK USB Devices**, not Imaging Devices.

---

## Phase 1 — Core behavioral characterization

### 1.1 Still capture (manual focus)

Command: `SendCommand(handle, CrCommandId_Release, Down)`, ~35 ms, then `Up`.
This is S2 / full-shutter release only.

> **Corrected.** An earlier revision of this section claimed the MF baseline
> used `CrCommandId_S1andRelease` and that it was preferred. Both statements
> were wrong. The baseline was measured with the `CrCommandId_Release`
> Down/~35 ms/Up lifecycle, and `S1andRelease` is ungated — exposure is
> committed before application logic can accept or reject focus — so it is not
> the recommended autofocus capture path. See section 1.2. Timings below are
> unchanged.

`Connect()` is called in `CrSdkControlMode_Remote`. Capture is unavailable in ContentsTransfer mode without a reconnect.

**Capture event timing** (trigger t=0, measured from `SendCommand` call):

| Event | Latency (measured) |
|---|---|
| SDK fires capture-complete event | ~380–435 ms |
| New content appears in card index | ~816–851 ms |

### 1.2 Autofocus

#### S1 and S2 are separate SDK controls

The SDK models the physical shutter in two independent stages:

| Stage | API | Notes |
|---|---|---|
| S1 (half-press / AF) | `SetDeviceProperty(S1_prop, Locked/Unlocked)` | Asserts autofocus |
| S2 (full release) | `SendCommand(CrCommandId_Release, Down/Up)` | Does NOT assert S1 |

`CrCommandId_Release Down/Up` alone is an S2-only command. In AF mode, it returns success but does not produce an exposure unless S1 was already locked. This was confirmed by observing that `FocusIndication` remained `Unlocked`, no `Captured_Event` fired, and no content was created when only S2 was issued in AF-S mode. MF captures with the same command immediately afterward succeeded normally.

#### Correct AF-S capture sequence

```
1. arm FocusIndication listener + OnWarningExt listener
2. SetDeviceProperty(S1, Locked)
3. wait for first Focused_AF_S | Focused_AF_C from either channel
4. SendCommand(Release, Down) + SendCommand(Release, Up)
```

`TrackingSubject_AF_C` is an in-progress state — it must never gate S2.

#### AF-S timing

| Event | Measured |
|---|---|
| S1 asserted → FocusIndication Focused_AF_S | ~169–200 ms |
| S1 asserted → Captured_Event | ~604–633 ms |
| S1 asserted → new content in index | ~1,064–1,170 ms |
| S1 asserted → screennail complete | ~1,130–1,236 ms |

Three repeated AF-S captures all produced unique content, distinct preview hashes, and no ambiguous callbacks.

#### AF-S no-lock (deliberate fail)

With unfocusable subject: `FocusIndication` reached `NotFocused_AF_S` at ~731 ms. S2 was never issued. No content created. S1 remained Locked and required explicit release before the next operation.

#### AF-C timing

`FocusIndication` progression: `TrackingSubject_AF_C` at ~122 ms → `Focused_AF_C` at ~183 ms → `Captured_Event` at ~617 ms. Gap between TrackingSubject and Focused: ~61 ms. A gate on TrackingSubject alone would have fired too early.

#### CrCommandId_S1andRelease (convenience command)

Observed behavior: AF occurred, property focus at ~181 ms, AF-status channel at ~196 ms, capture at ~633 ms, content created. The command is ungated — S2 fires regardless of focus state, so there is no opportunity to inspect focus and abort. Not recommended as a default capture path. Appropriate only as a low-level escape hatch or for MF sessions.

#### S1 cleanup semantics

- **Successful capture (S2 Down/Up fired):** S1 reads back as Unlocked without an explicit property write. S2 Up clears both stages.
- **AF timeout (S2 never fired):** S1 remains Locked. Must be explicitly released.

#### Two AF-state channels

`FocusIndication` (device property) and `CrWarningExt_AFStatus` (`0x60001`, via `OnWarningExt`) are independent AF-state signals. Either may lead the other with no fixed ordering observed. In one run `FocusIndication` led by ~15 ms; in an AF-C run the AF-status channel led by ~100 ms.

**Rule:** Subscribe to both. Decode each using its own enum. First valid `Focused_*` state from either wins. Direct property reads remain the fallback against stale notification behavior.

#### AF/MF physical switch

Physical AF-to-MF-to-AF transitions produce `OnPropertyChangedCodes` in a coalesced batch, with occasional stragglers ~102 ms later. Unrelated property churn can accompany transitions. The capture path should observe physical AF/MF state from property data; it should not silently change it.

#### Live view during autofocus and capture

Live view continued producing frames while S1/AF was active. Around the actual exposure there was a ~108 ms live-view gap, after which live view resumed automatically. No separate control architecture is required to run live view and AF simultaneously.

### 1.3 Capture-to-preview latency — all paths

All measurements are trigger-to-bytes-in-RAM. All paths verified as definitively tied to the captured still except the live-view frame fallback.

#### Path A — RAM postview (FAST EXACT-STILL PATH)

Setting: `Setting_Key_EnablePostView=1`, `Setting_Key_PostViewTransferringType=CrPostViewTransferring_UserSelect_RAM`.  
Callback: `OnNotifyPostViewImage(filename, size)`.  
Pull: `PullPostViewImage(handle, buf, bufSize)`.

| Metric | Value |
|---|---|
| JPEG dimensions | 4240 × 2832 |
| JPEG size | ~4.64 MB (4,639,743 bytes observed) |
| Trigger → callback fired | ~681 ms |
| Callback → pull complete | ~2 ms |
| **Trigger → bytes in RAM** | **~683 ms** |
| Exact-still association | **Yes** |
| Requires original download | No |
| Mode | Remote + Host+Card |

`SetDeviceSetting(EnablePostView)` is rejected in Remote mode with `0x8402`, but postview bytes are still delivered when destination is `Host PC + Memory Card`. These are independent capabilities — configuration acceptance does not predict delivery.

#### Path B — Screennail (BEST LIGHTWEIGHT EXACT-STILL PATH)

API: `GetRemoteTransferContentsCompressedDataFile` with `CrGetContentsCompressedDataType_Screennail`.  
Requires: RemoteTransfer mode.

| Metric | Value |
|---|---|
| JPEG dimensions | 1616 × 1080 |
| JPEG size | ~123 KB (122,986 bytes observed) |
| Trigger → content appears in index | ~816 ms |
| Trigger → bytes complete | ~902 ms |
| Transfer duration | ~86 ms |
| **Trigger → bytes in RAM** | **~902 ms** |
| Exact-still association | **Yes** (matched by content ID) |
| Requires original download | No |
| Mode | RemoteTransfer (any destination) |

#### Path C — Content thumbnail

| Metric | Value |
|---|---|
| JPEG dimensions | 160 × 120 |
| JPEG size | ~56 KB (56,113 bytes observed) |
| Trigger → bytes complete | ~944 ms |
| Exact-still association | **Yes** |
| Mode | RemoteTransfer (any destination) |

Thumbnail is too small for reconstruction preview. Slower than screennail despite being smaller.

#### Path D — Live-view frame (FALLBACK, NOT EXACT-STILL)

First post-capture frame appears ~504 ms after trigger. Not guaranteed to match captured exposure.

| Metric | Value |
|---|---|
| Format | JPEG, 640 × 428 |
| Typical size | 27.5–86.9 KB |
| Trigger → first post-capture frame | ~504 ms |
| SDK buffer copy | 3–32 µs |
| Exact-still association | **No** |
| Mode | Remote only |

#### Path E — Small-size transfer

Not fully characterized due to stale-handle probe crash. Requires ContentsTransfer mode and a reconnect. Optional optimization only.

### 1.4 Control mode and destination capability matrix

Connection control mode and save destination are independent configuration axes. Both must be considered when reasoning about available functionality.

| Capability | Remote / Card | Remote / Host+Card | RemoteTransfer / Card | RemoteTransfer / Host+Card |
|---|:---:|:---:|:---:|:---:|
| Property count | 394 | 394 | 392 | 392 |
| Still capture | Yes | Yes | Yes | Yes |
| Live view | Yes | Yes | **No** | **No** |
| Postview config accepted | No (0x8402) | No (0x8402) | Yes | Yes |
| Postview bytes delivered | Unresolved | **Yes** (~163 KB) | No (size 0) | **Yes** |
| Content list / index | No | No | Yes | Yes |
| Thumbnail | No | No | Yes | Yes |
| Screennail | No | No | Yes | Yes |

**Critical findings:**

**Live view is unavailable in RemoteTransfer mode.** `GetLiveViewImageInfo` succeeds but reports a zero-byte buffer; `GetLiveViewImage` returns `CrError_Generic` persistently. Tested across 100+ attempts and multiple clean processes.

**Postview configuration acceptance ≠ postview delivery.** `SetDeviceSetting(EnablePostView)` is rejected in Remote mode (error `0x8402`), but postview bytes are still delivered in Remote + Host+Card. Check delivery empirically; do not infer from config acceptance.

**Remote + Host+Card is the recommended single-session mode for SPTFS:** provides continuous live view, AF, still capture, and exact RAM postview delivery without a mode switch. RemoteTransfer is only needed when content index, screennail, or thumbnail access is required.

**Property count differs by mode.** Remote exposes 394 properties; RemoteTransfer exposes 392. The two extra codes in Remote (`0x581`, `0x582`) likely correspond to live-view properties. Property count must not be used as a session health assertion. CrSDKPy must preserve unknown numeric property codes.

### 1.5 Mode transitions

Control mode is chosen at `Connect()` time and cannot be changed in-session. Switching requires disconnect + reconnect.

| Metric | Measured |
|---|---|
| Connect → usable handle | ~119–155 ms |
| Teardown (Disconnect + ReleaseDevice) | ~100 ms |
| Post-reconnect first live-view frame | On first attempt |
| Properties after reconnect (Remote) | 394 |

The same enumerated camera object can be reused after a mode transition. No late callbacks crossed between sessions. This supports a `Camera` / `Session` layering where the Python `Camera` object survives mode transitions by replacing its underlying session.

### 1.6 Live view

| Property | Value |
|---|---|
| Format | JPEG (variable size) |
| Dimensions | 640 × 428 |
| Frame rate | ~29.2 fps (normal), ~26.8 fps during video |
| SDK buffer copy | 3–32 µs (SDK owns buffer; copy out immediately) |
| Behavior during still capture | Stream pauses ~108 ms around exposure |
| Post-capture first frame | ~504 ms after trigger |
| Available modes | **Remote only** |

Live view streams from a single SDK-managed background buffer. Copy out immediately; do not hold references across frames. Live view is unavailable in RemoteTransfer mode.

### 1.7 Camera properties

`GetDeviceProperties()` returns 394 properties in Remote mode, 392 in RemoteTransfer mode. `OnPropertyChanged()` / `OnPropertyChangedCodes()` fire on any change. Physical camera changes trigger `OnPropertyChangedCodes`. Unknown property codes must be preserved, not discarded.

### 1.8 Video recording

| Metric | Value |
|---|---|
| Start | `SendCommand(CrCommandId_MovieRecord, Down)` |
| Stop | `SendCommand(CrCommandId_MovieRecord, Up)` or `CrCommandId_MovieRecButtonToggle` |
| Recording state active | 182–234 ms after start |
| Recording idle after stop | 152–202 ms after stop |
| Result callback | `OnCompleteOperation(0x20069)` = `MovieRecordingOperation_Result_OK` |
| Live view during video | Continues at ~26.8 fps |

### 1.9 Sleep / wake

No general sleep property was exposed in the 394 retrieved properties. Remote USB control likely inhibits automatic sleep. Not probed.

### 1.10 Disconnect / reconnect / error behavior

#### Software reconnect (5 cycles)

| Metric | Value |
|---|---|
| Clean cycles | 5/5 |
| Full process lifecycle | 3.41–3.47 s |
| Loss signal | `OnWarning(CrWarning_Connect_Reconnecting)` — NOT `OnDisconnected` |
| Recovery signal | `CrWarning_Connect_Reconnected` + second `OnConnected` |
| Recovery time (power cycle) | ~44.5 s |

**Critical rule:** With `CrReconnecting_ON`, the SDK does not fire `OnDisconnected` during a transient disconnect. State machine must handle: Connected → Reconnecting → Reconnected (second OnConnected) without an intermediate Disconnected.

#### Physical USB unplug/replug (idle)

Recovery interval ~28 s. Camera displayed USB mode prompt; operator selected Remote Shooting. `OnDisconnected` fired only at the explicit final disconnect.

#### Physical USB unplug/replug (during live view)

Recovery interval ~23 s. First valid JPEG arrived 38 ms after recovery. Live view resumed automatically. Original device handle remained usable. 394 properties readable after recovery. Failed polls during loss returned SDK errors normally; no native crash.

---

## Phase 2 — Bridge evaluation

### Calling convention

All `SCRSDK` public API functions are `extern "C"` with MSVC x64 ABI. No name mangling. ctypes can call them directly. Confirmed from `CameraRemote_SDK.h`.

### Callback threading

`IDeviceCallback` virtual methods fire on an SDK-internal thread. Callbacks must not block; route to an event queue and process on Python's side.

### Object and memory ownership

| Object | Ownership |
|---|---|
| `ICrEnumCameraObjectInfo*` | Caller; `Release()` required |
| `ICrCameraObjectInfo*` from enum | Owned by enum; do not separately release |
| `CrDeviceHandle` | Caller; `ReleaseDevice()` + `Disconnect()` required |
| `CrDeviceProperty*` arrays | Caller; `ReleaseDeviceProperties()` required |
| PostView buffer | Caller-allocated; `PullPostViewImage` copies into it |
| Content handle lists | Caller; separate Release per list type |

### Bridge verdict

**Recommended: thin C++ shared library with strict C ABI, consumed from Python via ctypes.**

Rationale:

1. 95% of the API is `extern "C"` and works directly with ctypes. The 5% requiring C++ (callbacks, enumeration interface methods) is isolated.
2. A C ABI shim (≈300 lines of C++) translates `IDeviceCallback` into C function pointer callbacks and wraps `ICrCameraObjectInfo` getters into plain C functions.
3. The C ABI binary is independent of Python ABI version.
4. SDK callbacks never enter Python directly. The shim queues events into a lock-free ring buffer; Python drains it by polling.
5. A C ABI is the natural isolation point for Sony's proprietary types and headers.
6. Consistent across Windows, Linux (ARM/x64), and macOS SDK variants.

**Session concept in the shim:** Mode transitions produce a new `Session` object (new CRSDK connection) while the `Camera` identity persists. Runtime capabilities attach to the session, not the camera model, enabling:

```python
session.capabilities.live_view
session.capabilities.ram_postview_delivery
session.capabilities.content_list
```

rather than model-gated conditionals.

### CrSDKPy must support (derived from characterization)

1. `Init()` / `Release()` SDK lifecycle.
2. USB camera enumeration via `EnumCameraObjects`.
3. Transport-selectable connection: USB first, IP/Ethernet later.
4. `Connect()` / `Disconnect()` / `ReleaseDevice()` with RAII.
5. Explicit control mode and destination at connect time.
6. Runtime capability discovery per session (live view, postview delivery, content APIs).
7. Still capture via `SendCommand(Release)`.
8. Explicit S1 (AF assertion) via `SetDeviceProperty`, with focus-gate logic before S2.
9. Dual AF-channel monitoring: `FocusIndication` property + `OnWarningExt` `0x60001`.
10. `TrackingSubject_AF_C` rejection as a focus-success state.
11. S1 cleanup on AF timeout.
12. PostView (RAM mode) for fast exact-still preview: observe delivery separately from config acceptance.
13. Screennail retrieval via RemoteTransfer compressed-data API.
14. Content thumbnail retrieval.
15. Live-view frame polling: `GetLiveViewImage` / `GetLiveViewImageInfo` (Remote mode only).
16. Full device property get/set/watch: preserve unknown numeric codes.
17. Video start/stop via `SendCommand(MovieRecord)`.
18. Connection state machine: Connecting → Connected → Reconnecting → Reconnected; no `OnDisconnected` between reconnects.
19. Session lifecycle modeling: Camera (identity) vs Session (mode + capabilities).
20. Error and warning callback routing.
21. Thread-safe event queue (SDK fires callbacks on internal thread).
22. Opaque generation-checked handles — never expose raw Sony pointers.

---

## Preview path comparison (SPTFS context)

| Use case | Path | Format | Size | Trigger → bytes | Mode |
|---|---|---|---|---|---|
| High-quality exact-still preview | RAM postview | JPEG | ~4.64 MB, 4240×2832 | ~683 ms | Remote + Host+Card |
| Lightweight exact-still preview | Screennail | JPEG | ~123 KB, 1616×1080 | ~902 ms | RemoteTransfer |
| Smallest exact-still preview | Thumbnail | JPEG | ~56 KB, 160×120 | ~944 ms | RemoteTransfer |
| Fastest visual (not exact-still) | Live-view frame | JPEG | 27–87 KB, 640×428 | ~504 ms | Remote only |

**Recommended SPTFS session:** `Remote + Host PC + Memory Card`. Provides:

- Continuous live-view preview (~29 fps) for operator framing
- Autofocus with S1-gated S2 capture
- Exact RAM postview at ~683 ms without mode switch
- ~108 ms live-view interruption around exposure, then automatic resume

RemoteTransfer session needed only when content index or screennail access is required. Mode switch costs ~220–255 ms total.

**K-S2 comparison:** K-S2 fast view-JPEG was ~54 KB at ~54 ms. FX3A screennail (~123 KB, ~902 ms) is the functional equivalent. Sony's architecture provides no sub-200 ms small-JPEG exact-still path. RAM postview (683 ms) is faster but at ~4.64 MB.

---

## Architecture and API design conclusions

### Capability discovery is the contract

The FX3A characterization exposed that mode, destination, and camera firmware version each gate distinct capabilities. Hardcoding FX3A behavior as the generic CrSDKPy API would produce an A7-hostile library. Every capability-dependent operation must check an observed runtime flag, not a model string.

A second Sony body (A7-series) should plug in as:

```python
cam = crsdkpy.discover()[0].open()
print(cam.info)                   # model, firmware, transport
print(cam.session.capabilities)   # live_view, ram_postview, content_list, ...
cam.session.live_view.start()
cam.session.autofocus_and_capture()
```

If adding an A7 requires `if model == "A7":` anywhere, the architecture failed.

### Camera vs Session layering

| Concept | Contains | Persists across |
|---|---|---|
| `Camera` | model, firmware, transport, identity | Mode transitions, reconnects |
| `Session` | control_mode, destination, connection state, observed capabilities | Single CRSDK connection |

Mode transitions (`Remote` ↔ `RemoteTransfer`) replace the `Session` while the `Camera` survives. The Python API exposes `Camera.open(mode, destination)` → `Session`. SPTFS should never need to reference a specific Sony mode or property code.

### SPTFS layering

```
Sony body
↓
Sony CRSDK (native .dll/.so)
↓
CrSDKPy native bridge (C ABI shim + ctypes)
↓
CrSDKPy Python API (Camera / Session / capabilities)
↓
SPTFS Sony adapter (autofocus_and_capture, get_preview, ...)
↓
SPTFS camera-neutral interface
```

SPTFS should never see: S1, S2, CRSDK enums, RemoteTransfer, AFWarningExt, Sony handle lists, or content control codes.

### Simulator / fake backend

The characterization data is sufficient for a serious fake backend (not a canned mock). It must support:

- Normal paths: discovery, connect/disconnect, properties, AF, capture, previews, live view, video, reconnect.
- Configurable synthetic cameras: with/without S1, with/without video, different mode capabilities, unknown property codes, different AF timing.
- Race/failure injection: property focus before AF-warning, AF-warning before property focus, transient disagreement, `TrackingSubject_AF_C`, sticky focused state with no callback, reconnect without `OnDisconnected`, repeated `OnConnected`, late callback stragglers, non-contiguous content IDs, live-view interruption around exposure, successful info call followed by unusable frame fetch, mode-dependent capability differences.

The fake backend must implement the same internal interface as the real native backend. No separate toy `FakeCamera` API.

---

## FX3A-specific quirks (complete list)

1. Appears as `ILME-FX3A` (not `ILME-FX3`); SDK enum includes `CrCameraDeviceModel_ILME_FX3A` separately.
2. `maxNums=1` in `GetRemoteTransferContentsInfoList` returns the **oldest** record for a date. Request all and select by max content ID.
3. `GetRemoteTransferContentsInfoList` does not accept a null capture-date pointer; enumerate dates first with `GetRemoteTransferCapturedDateList`.
4. Content handle lists must be copied and released while still valid. Reuse after `Release` causes access violation.
5. `CrWarning_Connect_Reconnecting` fires on disconnect, not `OnDisconnected`. Bridge state machine must not require `OnDisconnected` between reconnects.
6. `SessionAlreadyOpened` (0x8210) on rapid reconnect — add a short delay.
7. USB re-enumeration displays a mode selection prompt on camera body; operator must select Remote Shooting before SDK reconnection completes.
8. `CrCommandId_Release` (S2) does not assert S1/AF; in AF mode, SDK returns success but no exposure occurs without prior S1 lock.
9. `TrackingSubject_AF_C` is an in-progress AF-C state, not a focus-success state.
10. `OnWarningExt` `0x60001` (`CrWarningExt_AFStatus`) is a second AF-state channel; either may lead `FocusIndication` with no fixed ordering.
11. S1 remains Locked after AF timeout if S2 was never issued; must be explicitly released.
12. `GetLiveViewImage` returns `CrError_Generic` in RemoteTransfer mode; `GetLiveViewImageInfo` reports zero buffer. Live view is unavailable in RemoteTransfer mode.
13. `SetDeviceSetting(EnablePostView)` is rejected (0x8402) in Remote mode, but postview bytes are still delivered when destination is Host+Card. Configuration acceptance and delivery are independent.
14. Property count differs by mode (Remote=394, RemoteTransfer=392). Must not be used as a health assertion. Unknown property codes must be preserved.
15. Content IDs are monotonically increasing but not guaranteed contiguous; new-content detection must use `new_id > baseline`, not `new_id == baseline + 1`.
16. AF/MF physical switch produces a coalesced property-change callback batch with an occasional straggler ~102 ms later.

---

## Remaining unknowns (non-gating)

- Small-size transfer (`PullContentsFile` SmallSize) — probe not completed safely. Optional optimization.
- Remote + Card postview delivery — unresolved (only Host+Card confirmed in Remote mode).
- USB cable loss during capture or recording.
- Passive sleep / remote wake under long idle.
- OSD/overlay metadata.
- AF area positioning and focus-position stepping.
- Burst/continuous shooting.
- Card-full, media-removal, and recording-failure paths.
- Second supported body (A7-series) to separate FX3A quirks from generic CRSDK behavior.

---

# Session 2026-08-20 — library validation against the same body

**Hardware:** Sony ILME-FX3A, firmware 2.02, CRSDK 2.02.00 (Win64), USB
`Cr_PTP_USB`, PID `0x0F52`, connection version 300, on USB-PD power.
**Under test:** the CrSDKPy public API through the host backend, not raw probes.
**Exposures used:** 47 stills (content `131535`–`131581`) and one 3 s recording.

Everything below was measured through the public API. Where it contradicts an
earlier entry, the correction is stated rather than the old text edited.

## Formal gates

| Gate | Result | Evidence |
|---|---|---|
| R regressions | PASS | 10 checks: discovery, Remote open, 394 properties, cautions clear, battery, both media slots, ISO 1000→125→1000, capability reads, RemoteTransfer capabilities, clean teardown |
| A content association | PASS | 20 checks, 2 exposures — see below |
| B postview | PASS | 1616×1080, +128–184 ms after the exposure event |
| C live view | PASS | 149 frames / 5 s, 29.7 fps, 640×428, 0 empty polls, 0 skipped |
| D video | PASS | idle → recording → frame while recording → 3 s → idle |
| M manual-focus capture | PASS | plain release in MF exposed at 771 ms |
| N autofocus no-lock | PASS | refused at 3000 ms, no release issued, half-press verified clear |

R + B/C + D also passed as one consolidated run: 27 checks, 0 failed, 0 skipped.

## Capture timing, phase by phase

Autofocus path, `remote_transfer`, card destination:

```
autofocus        694 – 794 ms
exposure event  1491 – 1585 ms after the request
content visible  490 – 639 ms after the exposure
whole operation 2012 – 2103 ms
```

Plain release in MF, same session type:

```
exposure event   759 – 814 ms
content visible  498 – 546 ms
whole operation 1264 – 1354 ms
```

The autofocus path costs about 750 ms more, which is the focus phase. A baseline
content read costs 5–13 ms. Scene was f/16, ISO 1000, shutter 0.6 s.

## Accepted release with no exposure

Field integrations reported capture times clustering near 11 s, 13 s and 20 s.
Those are sums of this library's own deadlines, not camera latency:

```
11 s  ≈ 10 s exposure wait + ~1 s of real work
13 s  ≈  3 s focus timeout + 10 s exposure wait
20 s  ≈ two 10 s waits
 6 s  ≈  3 s focus timeout + 1.5 s half-press release
```

Driven back to back through the autofocus path, **5 of 22 captures produced no
exposure at all**. In each case autofocus confirmed, the release was accepted,
and the only subsequent events were focus reporting locked and then unlocked
about 150 ms later. No capture event, no warning, no error, no new content, and
the content-id sequence stayed contiguous — so the camera genuinely did not
expose; nothing was lost in transit.

Rate depends on how hard the body is driven:

| Sequence | Exposed |
|---|---|
| autofocus, back to back | 17 / 22 |
| autofocus, paced 1.5 s apart | 10 / 10 |
| plain release in MF, back to back, faster 1.3 s cycle | 10 / 10 |

Plain releases at a shorter cycle never failed, so this is the rapid
re-assertion of autofocus rather than shutter or card throughput. There is no
signal to detect it by except the absence of the capture event.

## Focus mode values, measured

| Property | AF-S | MF |
|---|---|---|
| `FocusMode` `0x0109` | `2`, read-write | `1`, **read-only** |
| `FocusModeSetting` `0x0179` | `0` | `2` |
| `PreAF` `0x0260` | `1` | `0` |

`0x0109` becoming read-only in MF is expected: the switch is physical.
`PriorityKeySettings` `0x011A` is **not supported** on this body, so an
AF-priority setting cannot be read to explain the refusal above.

## Corrections to earlier entries

**Item 13 was incomplete.** Postview delivery does *not* follow from the
destination alone. The vendor's own sample calls `SetSaveInfo` immediately after
every successful `Connect`, and without it a capture whose destination includes
the host announces no postview at all — three exposures across both control
modes, with and without live view, produced not one delivery — and
`StillImageStoreDestination` then reports itself as not settable for the rest of
that session, consistent with a transfer the camera is still holding. Twenty
attempts over ten seconds were refused while a fresh session changed it
immediately. With the save path configured, postview arrives in 128–184 ms and
the destination becomes settable again after at most one retry.

Configuration acceptance does differ by mode, as originally recorded: `remote`
refuses it, `remote_transfer` accepts it. Delivery worked in both once the save
path existed.

**Item 6 has a mechanism.** A `Connect` after a consumer vanished without
disconnecting is accepted and then never delivers the connection callback, so it
runs out its 15 s deadline. The failed attempt's own `Disconnect` is what clears
the camera's stale session, after which the next attempt connects in ~0.6 s.
Reproduced deterministically by killing a host holding an open session: first
open failed at 15.03 s, next succeeded at 0.59 s.

## Newly observed

- **Transient busy on the first content listing.** Opening a RemoteTransfer
  session and listing immediately can fail in ~1 ms with
  `CrError_RemoteTransfer_GetContentsInfoListProcessing` (`0x8D05`) while the
  camera builds its index; the next call succeeds. Seen in three of four
  consecutive sessions. It is transient and must not be read as a lost link.
- **Destination writes are not immediately observable.** `memory_card` ↔
  `host_and_memory_card` switched six times in one session, each confirmed in
  101–152 ms, but a read in the next statement still returns the old value.
- **Screennail bytes still differ across transfers.** A refetch of the same
  content returned the same 227005 bytes with a different digest, consistent
  with the earlier finding. Byte equality is not an identity test.
- **Reconnect needs no `OnDisconnected`.** With live view running, a USB
  unplug/replug produced `connected → reconnecting (0x20002) → connected`, the
  recovery flagged on the second connected event and no disconnect notification
  at any point. 11.7 s from the reconnecting notification. The same session
  object stayed usable: 394 properties readable, live view resumed unaided, a
  following autofocus capture exposed in 1496 ms, identity unchanged, cautions
  clear.
- **The connection version is reported only on a first connect** (300 here), and
  is absent from a recovery.

## Remaining unknowns after this session

Unchanged from the list above except that Remote+Host postview, fresh-capture
content association, live view, video, destination writes and the reconnect path
are no longer unknowns. Still open:

- Remote + Card-only postview delivery.
- Original-file transfer (`PullContentsFile`); deferred card ingest is the
  current mechanism.
- Why the body refuses a release after rapid autofocus, beyond the pacing
  correlation.
- Cable loss *during* a capture or recording, as opposed to while idle.
- AF area positioning, focus-position stepping, burst, card-full and
  media-removal paths.
- A second body, to separate FX3A quirks from generic CRSDK behaviour.
