# Sony ILME-FX3A Characterization

Date: 2026-08-13

This report records observations made while characterizing an ILME-FX3A for
CrSDKPy. It contains no Sony SDK source, headers, binaries, documentation, or
captured media.

## Test configuration

- Camera: Sony ILME-FX3A
- Camera firmware: 2.02 (reported from the camera by the operator)
- USB product ID: `0x0F52`
- Host: Windows 11, 64-bit
- Transport: USB using the Sony-compatible libusbK driver
- SDK: Sony Camera Remote SDK 2.02.00, Win64 package
- Focus mode for still tests: manual focus (unless stated otherwise)
- Still file format: RAW
- Normal shooting destination: camera card

The FX3A was the first test body. Results must not be assumed to apply to every
CRSDK-supported camera without capability discovery or testing on another body.

## Runtime, enumeration, and connection

SDK initialization and release completed cleanly in repeated processes. Camera
enumeration requires Sony C++ objects even though several top-level SDK symbols
use a C-compatible ABI. The camera enumerated as `ILME-FX3A`.

Connection is asynchronous. Initial properties arrive through a general
property-change notification followed by property-code notifications. A stable
snapshot contained 394 properties in Remote mode and 392 in RemoteTransfer mode.
Five complete initialize/enumerate/connect/disconnect/release cycles succeeded
in 3.41-3.47 seconds each.

## Properties and callbacks

- Physical ISO changes produced property callbacks.
- SDK ISO writes 100 -> 125 -> 100 succeeded and the original value was
  restored.
- Property arrays are SDK-owned and must be released with the matching SDK
  release function.
- Initial property notifications may arrive in a burst and should be allowed to
  settle before constructing a snapshot.
- Final health checks reported `RecordingState = Not Recording` and both camera
  and system caution status as `NoError`.
- Remote mode exposed 394 properties; RemoteTransfer mode exposed 392. The two
  codes present only in Remote (`0x581`, `0x582`) correspond to live-view
  properties. Property count must not be used as a session health assertion.

## Still-capture lifecycle

Manual-focus card-only RAW capture used a release-button down/up command pair.
Observed representative timing was:

1. Trigger at 0 ms.
2. Captured event at approximately 380-417 ms.
3. Card content/list update at approximately 799-939 ms in the repeated tests.
4. Exact screennail bytes complete at approximately 881-1,010 ms.

A combined host-and-card full RAW transfer produced a 12,849,152-byte ARW and
completed at approximately 827 ms.

Two distinct postview mechanisms were observed. The JPEG RAM postview
(`PullPostViewImage` with `PostViewTransferringType = UserSelect_RAM`) operates
in ordinary `Remote` mode and completed at approximately 683 ms. A RAW file
download via the RemoteTransfer content API completed at approximately 911 ms;
that API requires `RemoteTransfer` control mode — an ordinary `Remote`
connection returned `Api_InvalidCalled`.

## Autofocus

### S1 and S2 are distinct controls

The SDK models the physical shutter in two independent stages:

- **S1 (half-press / autofocus):** controlled through `SetDeviceProperty` on
  the S1 device property. Setting S1 = Locked asserts autofocus.
- **S2 (full release):** controlled through `SendCommand(CrCommandId_Release,
  Down/Up)`. S2 does not implicitly assert S1.

`CrCommandId_Release Down/Up` alone (`S2 only`) is therefore not an autofocus
command. In AF-S mode with only S2 issued, the capture command was accepted by
the SDK and returned success, but `FocusIndication` remained `Unlocked`, no
`Captured_Event` fired, and no content was created. MF captures using the same
command immediately afterward succeeded normally.

### Verified AF-S sequence

A correct autofocus-then-capture sequence:

1. Register listener on `FocusIndication` property.
2. Set S1 = Locked via `SetDeviceProperty`.
3. Wait for `FocusIndication` to reach `Focused_AF_S` (or `Focused_AF_C`).
4. Issue `SendCommand(CrCommandId_Release, Down)` then `Up`.

`TrackingSubject_AF_C` is an in-progress AF-C state and must not be treated as
a focus-success gate.

### AF-S timing (good-lock target)

| Event | Observed |
|---|---:|
| S1 asserted (t=0) | 0 ms |
| FocusIndication: Focused_AF_S | ~169-200 ms |
| Captured_Event | ~604-633 ms |
| New content in index | ~1,064-1,170 ms |
| Screennail complete | ~1,130-1,236 ms |

Three sequential AF-S captures in separate clean sessions all produced unique
content IDs and distinct screennail hashes with no stale previews, ambiguous
callbacks, or Busy responses.

### AF-S timing (no-lock, deliberate fail)

With the subject/lens made unfocusable:

| Event | Observed |
|---|---:|
| S1 asserted (t=0) | 0 ms |
| FocusIndication: NotFocused_AF_S | ~731 ms |
| S2 issued | Never |
| Captured_Event | None |
| New content created | None |

S1 remained Locked at timeout and had to be explicitly released.

### AF-C timing

| Event | Observed |
|---|---:|
| FocusIndication: TrackingSubject_AF_C | ~122 ms |
| FocusIndication: Focused_AF_C | ~183 ms |
| Captured_Event | ~617 ms |

Confirms that `TrackingSubject_AF_C` leads `Focused_AF_C` by approximately
61 ms and must not trigger S2.

### CrCommandId_S1andRelease

This convenience command combines S1 and S2 in a single SDK call. Observed
behavior: AF occurred, focus became visible at approximately 181 ms (property)
/ 196 ms (AF-status channel), capture event at approximately 633 ms, S1
observable as Locked during operation, Release Up cleared S1 afterward. Content
was created and previews matched.

The command is ungated — exposure is committed without the caller being able to
inspect focus state first. It is not recommended as a default capture path where
AF failure handling is required.

### S1 cleanup semantics

After successful AF + capture (S2 Down/Up sequence): S1 read back as Unlocked
without an explicit property write; the S2 Up command cleared both stages.

After AF timeout (S1 locked, S2 never fired): S1 remained Locked. Explicit
release required.

### Second AF-status channel

`OnWarningExt` carries warning code `0x60001`
(`CrWarningExt_AFStatus`), which provides a second AF-state signal independent
of the `FocusIndication` device property. Either channel may lead the other;
no fixed ordering was observed. In one run `FocusIndication` led by roughly
15 ms; in an AF-C run the AF-status channel reported `Focused_AF_C` at ~183 ms
while `FocusIndication` still showed `TrackingSubject_AF_C` until ~283 ms.

Both channels must be subscribed to; the first valid focused state from either
is the action trigger. `TrackingSubject_AF_C` must not count as focused on
either channel.

### AF/MF physical switch

Physical AF-to-MF-to-AF transitions on the lens body produced
`OnPropertyChangedCodes` callbacks. Related property codes arrived as a
coalesced batch, with an occasional straggler approximately 102 ms later.
Unrelated property churn accompanied some transitions. State was immediately
readable inside the callback.

### Live view during autofocus and capture

Live view continued producing frames while S1/AF was active. Around the actual
exposure there was approximately a 108 ms live-view gap, after which live view
resumed automatically and returned to baseline cadence. No separate control
architecture for AF and live view was required.

## Preview routes

| Route | Exact still | Format and dimensions | Observed size | Trigger to usable bytes |
|---|---:|---|---:|---:|
| JPEG RAM postview | Yes | JPEG, 4240 x 2832 | 4,639,743 bytes | ~683 ms |
| RAW RAM postview | Yes | ARW | ~12.87 MB | ~911 ms |
| Content screennail | Yes | JPEG, 1616 x 1080 | ~119-123 KB | ~881-1,010 ms |
| Content thumbnail | Yes | JPEG, 160 x 120 | 56,113 bytes | ~944 ms |
| Post-shot live view | No guarantee | JPEG, 640 x 428 | ~85 KB typical | ~504 ms |

The JPEG RAM postview is the best verified high-quality exact preview. The
screennail is the best verified lightweight exact preview. A post-shot live-view
frame is faster but is not guaranteed to represent the captured exposure.

Small-size `ContentsTransfer` remains deferred. An early exploratory probe used
a content handle after releasing its owning list and crashed in native code.
The camera recovered normally, but this route must not be revisited without
strictly corrected lifetime handling.

## Control mode and destination capability matrix

Connection control mode (`Remote` vs `RemoteTransfer`) and save destination
(`Memory Card` vs `Host PC + Memory Card`) are independent axes that each
affect available functionality.

| Capability | Remote / Card | Remote / Host+Card | RemoteTransfer / Card | RemoteTransfer / Host+Card |
|---|:---:|:---:|:---:|:---:|
| Property enumeration | 394 | 394 | 392 | 392 |
| Still capture | Yes | Yes | Yes | Yes |
| Live view | Yes | Yes | No | No |
| Postview configuration | Rejected (0x8402) | Rejected (0x8402) | Yes | Yes |
| Postview bytes delivered | Unresolved | Yes (~163 KB JPEG observed) | No (size 0) | Yes |
| Content list / index | No | No | Yes | Yes |
| Thumbnail | No | No | Yes | Yes |
| Screennail | No | No | Yes | Yes |

Key observations:

- `SetDeviceSetting(EnablePostView)` is rejected in Remote mode with error
  `0x8402`, but postview bytes are still delivered when destination is
  `Host PC + Memory Card`. Postview configuration acceptance and postview
  delivery are independent capabilities.
- `GetLiveViewImage` returns `CrError_Generic` persistently in RemoteTransfer
  mode. `GetLiveViewImageInfo` succeeds but reports a zero-byte buffer. Tested
  across more than 100 attempts and multiple clean processes. Live view is not
  available in RemoteTransfer mode on this body.
- `Remote + Host+Card` provides live view, autofocus, still capture, and exact
  RAM postview delivery in a single session without a mode switch.
- Content index and screennail access require RemoteTransfer mode.

## Mode transitions

Control mode is set at `Connect()` time and cannot be changed in-session.
Switching modes requires disconnect and reconnect. Observed timings:

- `Connect()` returning a usable handle: approximately 119-155 ms.
- Teardown (`Disconnect()` + `ReleaseDevice()`): approximately 100 ms.
- After reconnecting into Remote mode: first live-view frame arrived on first
  attempt; 394 properties restored; same enumerated camera object was reusable.
- No late callbacks crossed between sessions.

## Repeated exact-still association

Three deliberately spaced captures were tested in separate clean sessions:

| Shot | Captured event | Content available | Content ID | Filename | Screennail complete | Bytes | SHA-256 prefix |
|---:|---:|---:|---:|---|---:|---:|---|
| 1 | 387 ms | 939 ms | 131154 | `DSC03394.ARW` | 1,010 ms | 118,695 | `98E00967` |
| 2 | 402 ms | 799 ms | 131155 | `DSC03395.ARW` | 881 ms | 118,774 | `25F0C1D1` |
| 3 | 387 ms | 811 ms | 131156 | `DSC03396.ARW` | 894 ms | 118,792 | `F7A7BDC5` |

Each pre-capture baseline was the immediately preceding content. IDs and file
numbers increased monotonically, filenames were coherent, and all JPEG hashes
were distinct. No busy response, ambiguous callback, or previous-shot image was
observed. Long-session callback accumulation was not tested because the proven
single-shot probe was used for safety.

Three sequential AF-S captures were also tested. Content IDs increased
monotonically but were not guaranteed contiguous — the new-content detection
rule must be `new_id > baseline`, not `new_id == baseline + 1`.

## Live view

No explicit start command was required in an ordinary remote connection.
`GetLiveViewImageInfo` reported a 640 x 428 image and a 307,200-byte caller
buffer. Frames were variable-size JPEGs, normally around 85-87 KB. Sixty unique
frames arrived over 2.023 seconds, approximately 29.2 frames per second.

`GetLiveViewImage` copies the latest SDK background buffer into caller-owned
memory; observed copy time was approximately 3-32 microseconds. During a still,
live view paused. In one run the last pre-trigger frame was at 487 ms, the
captured event was observed near 900 ms, and the first genuinely post-capture
frame was at 1,023 ms. Live view resumed without an explicit restart.

Live view is not available in RemoteTransfer mode (see control mode matrix).

## Video

Movie start and stop used the documented movie-record button press/release
command. Recording state became active 182-234 ms after start and returned to
idle 152-202 ms after stop. Sony warning `0x20069` confirmed
`MovieRecordingOperation_Result_OK`.

Live view continued during a four-second recording: 107 unique 640 x 428 JPEG
frames, approximately 26.8 frames per second. Frame sizes ranged from 27,520 to
86,912 bytes. Both short clips were written to the camera card, and recording
was confirmed stopped afterward.

## Reconnection and transport loss

### Software and power cycle

Software reconnect cycles were stable. With automatic reconnect enabled, a
physical power cycle did not emit `OnDisconnected`. It emitted
`Connect_Reconnecting`, later a second `OnConnected`, and then
`Connect_Reconnected`. Recovery took approximately 44.5 seconds between the
reconnecting and recovered signals. A fresh property health check succeeded.

### Actual USB unplug/replug while idle

- `Connect_Reconnecting` at 27,261 ms.
- No `OnDisconnected` during loss.
- Second `OnConnected` at 55,322 ms.
- `Connect_Reconnected` immediately afterward.
- Recovery interval between SDK loss/recovery signals: approximately 28.06 s.
- The original device handle remained usable.
- A 394-property read succeeded after recovery.
- `OnDisconnected` occurred only during the explicit final disconnect.

The camera displayed a USB mode prompt after re-enumeration; the operator had to
select **Remote Shooting**.

### Actual USB unplug/replug during live view

- `Connect_Reconnecting` at 22,828 ms.
- No `OnDisconnected` during loss.
- Second `OnConnected` and `Connect_Reconnected` at 45,998 ms.
- Recovery interval: approximately 23.17 s.
- First valid JPEG arrived 38 ms after the recovered `OnConnected`.
- Live view resumed automatically without another start/configuration call.
- The original device handle remained usable.
- A 394-property read succeeded after recovery.

Failed live-view polls while transport was absent returned normally as SDK
errors; no native crash occurred.

## Known quirks

- Do not model a physical loss as necessarily producing `OnDisconnected` when
  automatic reconnect is enabled.
- Treat reconnecting/reconnected warnings and repeated `OnConnected` callbacks
  as lifecycle state transitions.
- Content-list and property initialization are asynchronous.
- Copy all content identifiers and metadata before releasing an SDK list.
- Never expose or retain raw Sony content/list pointers beyond their owning
  lifetime.
- Live-view JPEG length varies per frame, including during recording and state
  transitions.
- Capability discovery is required; do not encode FX3A behavior as the generic
  public API.
- `CrCommandId_Release` (S2) does not assert S1/AF. In AF modes without an
  explicit S1 set, the command returns success but no exposure occurs.
- `TrackingSubject_AF_C` is an in-progress AF-C state, not a focus-success
  state.
- `OnWarningExt` carries `CrWarningExt_AFStatus` (0x60001) as a second
  AF-state channel. Either channel may lead `FocusIndication`; no fixed
  ordering exists.
- S1 remains Locked after an AF timeout if S2 was never issued; it must be
  explicitly released.
- `GetRemoteTransferContentsInfoList` with `maxNums=1` returns the oldest
  record for a date, not the newest. Request all records and select by max
  content ID.
- `GetRemoteTransferContentsInfoList` does not accept a null capture-date
  pointer; enumerate dates first with `GetRemoteTransferCapturedDateList`.
- Content handle lists must be copied and released while valid. Reuse after
  `Release` causes access violation.
- `SessionAlreadyOpened` (0x8210) observed on rapid consecutive connects;
  add a short delay.
- USB re-enumeration displays a mode selection prompt on the camera body;
  the operator must select Remote Shooting before SDK reconnection completes.
- Live view is unavailable in RemoteTransfer mode; `GetLiveViewImage` returns
  `CrError_Generic` even though `GetLiveViewImageInfo` succeeds.
- Postview configuration (`SetDeviceSetting(EnablePostView)`) is rejected
  (0x8402) in Remote mode, but postview bytes are still delivered when
  destination is `Host PC + Memory Card`. These are independent capabilities.
- Property count differs by mode: Remote = 394, RemoteTransfer = 392. Property
  count must not be used as a session health assertion.
- Content IDs are monotonically increasing but not guaranteed contiguous;
  new-content detection must use `new_id > baseline`, not
  `new_id == baseline + 1`.
- AF/MF physical switch produces a coalesced property-change callback batch with
  an occasional straggler approximately 102 ms later.

## Deferred or untested

- Unsafe small-size transfer path
- USB loss during capture or recording
- Card-full and recording-failure behavior
- Passive sleep/wake; the remote session exposed no general sleep property and
  may inhibit automatic sleep
- Live-view OSD/overlay data
- AF area positioning and focus-position stepping
- Burst/continuous shooting
- Remote + Card postview delivery (unresolved; only Host+Card was confirmed)
- Behavior on another Sony body, especially an Alpha 7-series camera

## Final verified camera state

After the final capture, a read-only health check reported:

- 394 readable properties (Remote mode)
- recording state: not recording
- camera caution status: no error
- system caution status: no error
- FocusIndication: S1 Unlocked
- AF/MF: AF-S (restored)
- save destination: Memory Card (restored)
- clean disconnect
- clean device release

All temporary setting changes made during characterization were restored. Raw
probes, generated binaries, logs, images, captured media, and the Sony SDK remain
outside version control.
