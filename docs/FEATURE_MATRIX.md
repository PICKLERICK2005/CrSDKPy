# Feature and validation matrix

Three different claims, kept apart on purpose:

| Column | Means |
|---|---|
| **Implemented** | The code exists in the native backend and the simulator. |
| **Simulator-tested** | Covered by automated tests that run in CI with no camera. |
| **Hardware-validated** | Exercised against a real body through the public API, with the result recorded. |

A feature can be implemented and simulator-tested and still be wrong on real
hardware. Nothing below is marked hardware-validated on the strength of a code
review or a passing simulator test.

## Validation reference body

```
Sony ILME-FX3A
firmware 2.02
CRSDK 2.02.00, Win64
USB, Cr_PTP_USB, PID 0x0F52
```

Other CRSDK-compatible bodies are expected to work according to the
capabilities they report, because there is no model-name branching anywhere in
the library. That expectation is **not** hardware-validated. The next
validation round is intended to be a second body, which tests the generic
architecture rather than adding a second backend.

## Matrix

| Feature | Implemented | Simulator-tested | Hardware-validated |
|---|:---:|:---:|:---:|
| Discovery and camera identity | yes | yes | **yes** |
| Session open / close / idempotent close | yes | yes | **yes** |
| Connection state and reconnect model | yes | yes | **yes** ¹ |
| Property enumeration and reads | yes | yes | **yes** |
| Unknown numeric property codes | yes | yes | **yes** |
| Property writes (typed, read-modify-write) | yes | yes | **yes** ² |
| Event stream | yes | yes | **yes** |
| Generic commands | yes | yes | **yes** |
| MF still capture, judged on the exposure event | yes | yes | **yes** ³ |
| Gated autofocus (both AF channels) | yes | yes | **yes** ⁴ |
| Autofocus failure leaves no half-press engaged | yes | yes | **yes** |
| Content index, exact content identity | yes | yes | **yes** ⁵ |
| Thumbnail | yes | yes | **yes** ⁵ |
| Screennail | yes | yes | **yes** ⁵ |
| RAM postview | yes | yes | **yes** ⁶ |
| Live view | yes | yes | **yes** ⁷ |
| Movie recording | yes | yes | **yes** ⁸ |
| Battery and media status | yes | yes | **yes** ⁹ |
| Still destination read / write | yes | yes | **yes** ¹⁰ |
| Busy reported as busy, not as a lost link | yes | yes | **yes** ¹¹ |
| Host-process isolation and recovery | yes | yes | **yes** |

¹ Re-exercised against a forced USB disconnect on 2026-08-20 with live view
running. The observed sequence was `connected → reconnecting → connected`, the
recovery being reported by warning `0x20002` with **no `OnDisconnected` at
all**, which is why the state machine must accept that transition. Recovery
took 11.7 s from the reconnecting notification. The same session object stayed
usable throughout: 394 properties still readable, live view resumed on its own,
a following autofocus capture exposed in 1496 ms, camera identity unchanged and
cautions clear.

² ISO 100 → 125 → 100 through the public API: write accepted, property-change
event observed in both directions, readback matched, read-only and unknown-code
writes refused.

³ Exposure confirmed by `Captured_Event`, 94 ms after the request.

⁴ Focus confirmed at 186 ms via the AF-status channel, exposure at 563 ms, and
a lens-covered run that reached `NotFocused_AF_S`, issued no release at all and
left the half-press stage verified clear.

⁵ Association with a *fresh* capture is now validated, which was the open item.
Two exposures with a deliberate framing change between them resolved to content
`131535` / `DSC03774.ARW` and `131536` / `DSC03775.ARW`, each becoming visible
486 ms and 575 ms after its exposure event. Thumbnails came back at 160×120 and
screennails at 1616×1080, transferring in 45–85 ms, with no stale association
in either direction and a `> baseline` filter returning only the newer item.

⁶ Postview delivery is validated at 1616×1080, arriving 128–184 ms after the
exposure event, both with and without live view running. It required a fix
first: see the `SetSaveInfo` entry below.

⁷ 149 frames in 5 s, 29.7 fps, 640×428, 41.0–41.6 KB per frame, frame intervals
1–64 ms, zero empty polls and zero skipped frames, 1.17 MiB/s over the stdio
transport with a worst-case fetch of 64 ms. Throughput varies with scene: a
brighter frame measured 1.97 MiB/s. Live view coexists with autofocus, pauses
around an exposure and resumes without being restarted. No fixed frame rate
should be assumed.

⁸ Recording state observed rather than assumed at every step: idle before,
`recording` after the toggle, a live-view frame delivered *while* recording,
still `recording` after three seconds, `idle` after the second toggle. This is
why start and stop must read the state first -- the vendor exposes a toggle,
so sending "start" twice would stop the recording.

⁹ Battery reported 37% while on USB-PD power; both media slots reported their
status and remaining-shot counts, and the count moved as expected across the
run.

¹⁰ Writing is now validated. `memory_card` ↔ `host_and_memory_card` switched
six times in one session, each confirmed in 101–152 ms. The write is **not**
observable immediately: reading the property in the next statement still
returns the old value, so a caller has to poll for the change.

¹¹ The first content listing after opening a RemoteTransfer session can fail in
about a millisecond with `CrError_RemoteTransfer_GetContentsInfoListProcessing`
while the camera is still building its index, then succeed on the next call.
Reproduced in three of four consecutive sessions. It reports as
`CameraBusyError`, not as a connection error, so a caller does not tear down a
healthy session over it.

## Known vendor behaviour worth knowing about

These were measured, not inferred. See `FX3_CHARACTERIZATION.md` for the full
record.

- **Control mode changes what a session can do.** Live view works in `remote`
  and not in `remote_transfer`; the content index is the other way round. The
  mode is fixed when the session opens.
- **Postview configuration and postview delivery are independent.** `remote`
  refuses the configuration call outright while `remote_transfer` accepts it,
  and delivery follows the still destination rather than either answer. Do not
  infer one from the other; the library models them as two capabilities.
  Corrected 2026-08-20: an earlier note said delivery followed from the
  destination alone. It does not -- it also needs the save path below.
- **A host-bound still needs a save path configured, or it goes nowhere.** The
  vendor documents this: postview delivery requires the output folder to have
  been set with `SetSaveInfo()`, the still destination to include the host,
  `Setting_Key_EnablePostView` to be Enable, and
  `Setting_Key_PostViewTransferringType` to select RAM delivery. CrSDKPy was not
  setting the save path at all. Without it, a capture whose destination includes
  the host announces no postview -- three exposures across both control modes,
  with and without live view, produced not one delivery -- and
  `StillImageStoreDestination` then reports itself as not settable for the rest
  of that session, consistent with a transfer the camera is still holding.
  Twenty attempts over ten seconds were refused while a *fresh* session could
  change it immediately. With the save path configured, postview arrives in
  128–184 ms and the destination becomes settable again after at most one
  retry.

- **The camera can accept a release and silently not expose.** Driven back to
  back through the autofocus path, 5 of 22 captures produced no exposure:
  autofocus confirmed, the release accepted, and then the camera reported focus
  going from locked to unlocked about 150 ms later and nothing else -- no
  capture event, no warning, no error, and no new content. There is no signal
  to detect it by other than the absence of the capture event, which is exactly
  why a capture reports progress rather than a success flag. The same sequence
  paced 1.5 s apart exposed 10 of 10, and plain MF releases at a *faster* 1.3 s
  cycle exposed 10 of 10, so it is the rapid re-assertion of autofocus the body
  objects to, not the shutter or the card. A caller that treats a
  `Captured_Event` as mandatory is correct; one that assumes a shutter command
  implies a frame is not.

- **The first `Connect` after an unclean shutdown is spent cleaning up.** If a
  previous consumer vanished without disconnecting, the camera still holds that
  transport session; the vendor accepts `Connect` and never delivers the
  connection callback, so the attempt runs out CrSDKPy's own 15 s deadline. In a
  heavier variant the attempt instead runs to the vendor's documented
  five-minute reconnect-monitoring timeout, measured three times at 300.1 s. The failed
  attempt's own `Disconnect` is what clears the stale session, after which the
  next attempt connects in about 0.6 s. Reproduced deterministically by killing
  a host that held an open session.

- **Focus mode is read-only when the body is in MF.** `FocusMode` (`0x0109`)
  reads `2` in AF-S and is writable; in MF it reads `1` and reports read-only,
  because the switch is physical. `FocusModeSetting` (`0x0179`) reads `0` and
  `2` respectively and `PreAF` (`0x0260`) flips `1` to `0`. Values measured on
  the body rather than taken from vendor enum names.

- **Content identifiers are monotonic but not contiguous.** Detect new content
  with `id > baseline`, never `baseline + 1`.
- **A live-view info call can succeed while reporting a zero-byte buffer**, with
  the frame fetch then failing. Reporting success is not the same as being able
  to deliver a frame, which is why `info_ok` and `usable` are separate.
- **Unknown property codes report as an invalid call**, not as a missing code.
  The library normalises that for property lookups only.
- **Releasing the half-press stage is not instantaneous**, so cleanup polls for
  the cleared state rather than assuming the write took effect.
- **Screennail bytes are not stable across repeated transfers of the same
  still.** Two fetches of one stored file returned identical length and
  identical tails, differing in roughly 27–30 bytes across 8 short runs, one of
  which is plainly ASCII UUID text — consistent with a single 36-character
  identifier being regenerated per transfer. Byte-for-byte equality is
  therefore **not** a valid identity test for a preview, and the library does
  not treat it as one. Whether those bytes sit in a metadata segment or in the
  picture data has not been established yet.


## Provenance of the notes above

Anything in this file that describes an API *contract* — which call to make, what
it returns, what must be released, which callback reports a result — comes from
the vendor's API reference, operation-sequence pages and callback reference, and
is not a CrSDKPy finding. What CrSDKPy contributes is measurement against one
body: latencies, throughput, per-mode differences, refusal behaviour, property
values, unknown codes, and the cases where a documented mechanism turned out to
need a condition this library was not meeting.

Two entries in the matrix are currently limited by CrSDKPy rather than by the
camera, and are called out so they are not mistaken for hardware limits:

- **String-valued properties read as `0`.** The backend reads only the numeric
  accessor, so the vendor's string accessor is never consulted. Seven properties
  on the reference body carry string values, including model name, serial number,
  firmware version and lens identity. This is why lens identity was briefly
  recorded as absent.
- **Advertised value sets are discarded.** The vendor exposes each property's
  permitted values or range, and 219 of 394 properties on the reference body
  carry one. CrSDKPy populates neither `allowed_values` nor `value_range` for any
  of them. Every typed facade — exposure, white balance, drive, focus range —
  needs this data.
