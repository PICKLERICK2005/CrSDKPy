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
| Connection state and reconnect model | yes | yes | partial ¹ |
| Property enumeration and reads | yes | yes | **yes** |
| Unknown numeric property codes | yes | yes | **yes** |
| Property writes (typed, read-modify-write) | yes | yes | **yes** ² |
| Event stream | yes | yes | **yes** |
| Generic commands | yes | yes | **yes** |
| MF still capture, judged on the exposure event | yes | yes | **yes** ³ |
| Gated autofocus (both AF channels) | yes | yes | **yes** ⁴ |
| Autofocus failure leaves no half-press engaged | yes | yes | **yes** |
| Content index, exact content identity | yes | yes | partial ⁵ |
| Thumbnail | yes | yes | partial ⁵ |
| Screennail | yes | yes | partial ⁵ |
| RAM postview | yes | yes | no |
| Live view | yes | yes | no |
| Movie recording | yes | yes | no |
| Battery and media status | yes | yes | no |
| Still destination read / write | yes | yes | partial ⁶ |
| Host-process isolation and recovery | yes | yes | **yes** |

¹ Reconnect was characterized on hardware before this library existed and is
modelled from those observations. The library's own reconnect path has not been
re-exercised against a forced disconnect.

² ISO 100 → 125 → 100 through the public API: write accepted, property-change
event observed in both directions, readback matched, read-only and unknown-code
writes refused.

³ Exposure confirmed by `Captured_Event`, 94 ms after the request.

⁴ Focus confirmed at 186 ms via the AF-status channel, exposure at 563 ms, and
a lens-covered run that reached `NotFocused_AF_S`, issued no release at all and
left the half-press stage verified clear.

⁵ The read path is validated: mode gating, index contents, filenames, sizes,
timestamps, and thumbnail and screennail transfers of existing stills at the
characterized geometries. What is **not** yet validated is association with a
*fresh* capture, which is the first item on the next hardware run.

⁶ Reading the destination is validated. Writing it is implemented and
simulator-tested but has not been exercised on the camera.

## Known vendor behaviour worth knowing about

These were measured, not inferred. See `FX3_CHARACTERIZATION.md` for the full
record.

- **Control mode changes what a session can do.** Live view works in `remote`
  and not in `remote_transfer`; the content index is the other way round. The
  mode is fixed when the session opens.
- **Postview configuration and postview delivery are independent.** One control
  mode refuses the configuration call outright and still delivers postview
  bytes once the still destination includes the host. Do not infer either from
  the other; the library models them as two capabilities.
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
