# Changelog

Dates are the day the change was validated, not the day it was released.

## 0.1.0b3 — 2026-08-27

**A beta. The new surface in this release is simulator, native and protocol
validated; it has not been exercised on a camera.** The b1/b2 core — capture,
autofocus, content, previews, live view, video, device status — remains
hardware-validated on the reference body and is unchanged. Which is which is
listed under *Hardware validation* below and in
`docs/FEATURE_MATRIX.md`.

### Added

- **Original-file downloads.** `session.content.download()` writes a full-size
  original to a local path, reporting progress and terminal outcome through
  the event stream, with cancellation. Uses the vendor's file-writing transfer,
  which takes plain integer identifiers, so no vendor-owned content object is
  ever held.

- **Absolute focus position and focus brackets.** `session.focus` reads and
  drives the position in the camera's own units, reports driving status, and
  waits for a move to settle. `capture_bracket()` acquires a deterministic
  sequence of frames at commanded positions and optionally restores the
  starting position.

- **Typed camera settings.** `session.exposure`, `session.white_balance` and
  `session.drive` present f-number, exposure compensation, shutter speed, ISO,
  exposure program, white-balance mode and drive mode as `Setting` objects over
  the generic property layer. Access, permitted values and ranges come from
  the camera at runtime; values stay in the vendor's own representation.

- **Lens.** `session.lens` reads model, version and serial as the string
  properties they are, and implements the separate focus-distance table
  protocol — request, wait for the vendor's warning, read once, release —
  including its documented enable-status precondition.

- **Zoom.** `session.zoom` exposes setting, scale, digital scale, distance,
  absolute position, driving status, speed range, type status and the zoom bar,
  and drives the zoom. A drive is refused unless the camera currently reports
  the operation as enabled.

- **Electronic framing.** `session.framing` implements the vendor's two
  operations and the order between them: an absolute execute, then relative
  area updates that are only valid afterwards.

- **Interval shooting.** `session.interval` exposes the settings and the
  camera's own progress. There is no scheduler: the camera owns the sequence.

- **Still transfer size.** `session.still_transfer.size` exposes the documented
  `Original` / `SmallSize` choice for host-bound stills.

- **Reconnection policy.** `camera.open(..., reconnect=...)` chooses between a
  bounded open and the vendor's five-minute reconnect monitor. `BOUNDED` is
  the default.

- **Conformance profiles.** A sanitized profile of the reference body ships as
  package data, with `crsdkpy.compare()` for measuring another body against it.
  Differences are classified rather than failed: extra properties, changed
  access and changed value sets are information, and the property count is
  never a health check. `tools/sanitize_characterization.py` and
  `tools/compare_profile.py` cover the workflow.

- **Setup diagnostics.** `python -m crsdkpy` reports the package, bridge, host
  and whether the Sony runtime is staged where the host will look, naming the
  directory inspected and what was missing. Loads no vendor code.

### Fixed

- **Property values were decoded against a fixed element width.** The vendor
  reports an advertised value set's size in bytes, not elements, so every
  32-bit property reported twice as many permitted values as it has —
  `IsoSensitivity` counted 48 where the camera advertises 24. The divisor is
  now the width of the property's own data type.

- **Current values were not narrowed to the declared width.** A signed
  property lost its sign: exposure compensation of −0.7 EV read as 64836
  rather than −700, and could then be neither validated nor written back.

- **A stale bridge or host was accepted and then failed on use.** Only the
  major version was checked, so a peer that predated a newly added operation
  completed the handshake, reported ready, and failed later with an error
  indistinguishable from a camera lacking the feature. Both minors are now
  enforced at load and at handshake; a newer peer is still accepted. Native
  ABI 1.5, IPC protocol 1.2.

- **A dead host reported only an exit code.** Its stderr was discarded, which
  is where a vendor loader failure says what happened. It is captured now, the
  exit status is waited for rather than reported as none, and a host with no
  vendor runtime staged beside it is refused before launch with a message
  naming the directory and what was absent.

- **Non-ASCII paths were mangled on the way into the vendor SDK.** The postview
  filename went through an ASCII-only conversion, the save directory was read
  through the narrow Windows environment, and the adapter directory was entered
  with the narrow `chdir` — each of which breaks for any user whose account
  name is not ASCII, which the default save directory sits under.

- Property names `0x0101` and `0x0105` now match the vendor's own enumeration.

### Hardware validation

Unchanged from 0.1.0b2 and still valid: discovery, sessions, properties,
events, still capture, gated autofocus, content index, thumbnails,
screennails, RAM postview, live view, movie recording, device status,
destination writes, busy classification and host isolation, all on
ILME-FX3A firmware 2.02 with CRSDK 2.02.00 over USB.

Awaiting a hardware session, and implemented against the vendor's documented
contract in the meantime:

- the public download lifecycle's callback and cancellation behaviour (the
  transfer itself is proven; the lifecycle around it is not);
- `ReconnectPolicy.BOUNDED`;
- the typed setting facades;
- the public focus facade and the bracket helper;
- the lens-information request/read/release path, which needs a lens that
  reports the feature as enabled;
- motorized zoom, which needs a lens that can move;
- electronic framing;
- an interval sequence;
- `StillImageTransSize` set to `Original`, and whether the body then delivers
  the captured file to the host;
- the Unicode path fixes, against a real runtime.

## 0.1.0b2 — 2026-08-20

**A beta, not a release candidate and not stable.** It changes a connection
default and adds API surface, and one supported body is not broad coverage.

Two groups of changes, and they did not get the same scrutiny:

*Validated on the reference ILME-FX3A.* The save-path and postview fix, the busy
classification, the connection-event fix, the destination handling and the
characterization tooling were each reproduced on the camera before they were
written. The record of what the body does is in
`docs/FX3_CHARACTERIZATION.md`.

*Not yet run against a camera.* The transfer result overload, string-valued
properties, advertised value sets and the reconnection policy were implemented
**after the final physical-camera session had ended**, prompted by auditing the
implementation against the vendor's own API reference and samples. They are
covered by the automated suite, the native build and self-test, and the
fake-host protocol tests, and they follow the documented contracts — but no part
of them has been exercised on hardware. Treat them accordingly.

### Fixed

- **A host-bound still had nowhere to go.** The vendor documents four conditions
  for postview delivery, the first being that the output folder has been set with
  `SetSaveInfo()`; CrSDKPy never called it, and the vendor's own sample calls it
  immediately after every successful `Connect`.
  Without it, a capture whose destination includes the host announced no
  postview at all — three exposures across both control modes, with and without
  live view, produced not one delivery — and `StillImageStoreDestination` then
  reported itself as not settable for the rest of that session. Postview now
  arrives 128–184 ms after the exposure event.

  The directory is chosen for you: `CRSDKPY_SAVE_DIR`, or
  `HostBackend(save_directory=...)`, otherwise a directory under the system
  temporary directory. It is deliberately **not** the directory the host runs
  in, which belongs to the vendor runtime and need not be writable.

- **A busy camera reported as a broken connection.** The first content listing
  after opening a RemoteTransfer session can fail in about a millisecond with
  `CrError_RemoteTransfer_GetContentsInfoListProcessing` (`0x8D05`) while the
  camera builds its index, then succeed on the next call. Every positive vendor
  status outside the adaptor family fell through to `CameraConnectionError`, so
  this transient looked exactly like a dead link and invited callers to tear
  down a healthy session. It now raises `CameraBusyError` with the vendor code
  preserved on `backend_code`.

- **A first connect claimed to be a recovery.** The vendor reports a connection
  version on `OnConnected` — 300 on the reference body — and it shared an event
  slot with the reconnect path's recovered flag, so `bool(300)` made every fresh
  session announce `recovered=True`. `ConnectionEvent.recovered` is now set only
  on a genuine recovery, and the version is exposed as
  `ConnectionEvent.connection_version` rather than discarded.

- **The first session open after an unclean shutdown failed.** If a previous
  consumer vanished without disconnecting, the camera still holds that transport
  session: the vendor accepts `Connect` and never delivers the connection
  callback, so the attempt spends its whole 15 s deadline waiting. The failed
  attempt's own disconnect is what clears the stale session, which makes one
  more attempt materially different from the first rather than a hopeful repeat.
  Exactly one retry, only for that condition — a vendor rejection of `Connect`
  is reported as-is. Measured: 15.03 s failure followed by a 0.59 s success.

  A heavier variant of the same situation instead runs to the vendor's documented
  five-minute reconnect-monitoring timeout, measured three times at 300.1 s, and
  the retry does not recover it. Reconnection monitoring is always enabled by
  this library, so that five minutes is the current worst case for a single open
  and nothing bounds it. Treat it as open.

### Changed

- Native ABI minor version 1 → 2: adds `crsdkpy_status_is_busy`, first-party
  warning codes for the save path, and IPC categories for busy and for the
  connection-callback timeout. The major version is unchanged and existing calls
  keep their signatures.

### Documentation

- `docs/FEATURE_MATRIX.md`: postview, live view, video, battery and media
  status, destination writes, fresh-capture content association and the
  reconnect path move to hardware-validated, each with measured numbers. A
  previous claim that postview delivery followed from the still destination
  alone is corrected — it also needs the save path.
- `docs/FX3_CHARACTERIZATION.md`: a dated session covering the formal gates,
  phase-by-phase capture timing, the measured focus-mode values, and a read-only
  survey of the lens, zoom, focus-position and electronic-framing families.
- Records that the camera can **accept a release and silently not expose**: 5 of
  22 captures when the autofocus path was driven back to back, with no event of
  any kind to detect it by. Paced 1.5 s apart, 10 of 10 exposed; plain manual
  focus releases at a faster cycle, 10 of 10. This is why a `Capture` reports
  progress rather than a success flag.


- Audited the characterization record against the vendor API reference and
  restated provenance throughout. Several behaviours the record presented as
  findings are specified by the vendor: the reconnect state transitions and the
  retained device handle, `CrNotify_Captured_Event` as the authority for an
  exposure, the four conditions postview delivery requires, and content
  identifiers being plain integers taken from `CrContentsInfo`.
- The five-minute session-open stall is the vendor's documented
  reconnect-monitoring timeout, not an unlocalized hang. It is reached because
  this library always connects with reconnection enabled.
- Corrected the lens-information entry. Lens identity is a string-valued
  property and needs no request; the request family concerns focus-distance data,
  which the tested lens does not provide.
- Corrected the note about host destinations and original files. The vendor
  documents automatic transfer to the PC; the size is governed by
  `StillImageTransSize`, which was set to SmallSize on the tested body.

### Changed (reconnection)

**This changes a default.** Ordinary session opening -- `Camera.open()` and
`Backend.open_session()` -- now connects with the vendor's reconnection monitor
**off**, passing `CrReconnecting_OFF`. Previously it always passed
`CrReconnecting_ON`, and no caller could see or change that.

- `ReconnectPolicy.BOUNDED` is the new default: the monitor is off, so opening a
  session either connects or fails promptly.
- `ReconnectPolicy.VENDOR` opts into the vendor's documented reconnect monitor
  by passing `CrReconnecting_ON`. That monitor retries a lost transport for
  **approximately five minutes** before reporting the session disconnected, so
  in the failure states where it engages a single call can block for about that
  long. Nothing above the vendor can shorten it. Choose this for a long-lived
  session that should survive a cable event unaided.
- Callers who relied on the previous behaviour get it back by passing
  `ReconnectPolicy.VENDOR` explicitly.
- The connection-callback retry now applies only under the bounded policy. Under
  the vendor policy a failed attempt has already spent the monitor's timeout, so
  a second attempt would spend it again without changing the outcome.

### Known gaps this audit exposed

- String-valued properties are read through the numeric accessor only and
  therefore report `0`, including model name, serial, firmware and lens identity.
- Advertised value sets and ranges are never populated, though most properties
  on the tested body carry one.
All three are now fixed; the entries above describe what was wrong.

### Tooling

- `tools/capture_timing.py`: times a capture phase by phase and flags any phase
  that ended on one of the library's own deadlines. Field reports of capture
  times clustering near 11 s, 13 s and 20 s turned out to be sums of those
  deadlines rather than camera latency.
- `tools/hardware_validation.py`: confirms a destination change instead of
  reading it back immediately, and tolerates the window after a
  host-destination capture where the camera refuses the write. Previously stage
  B could abort the run from inside its own cleanup, which skipped stage D.
- Two tests had never executed: they imported the fake host as `tests.fake_host`,
  which never resolves because `tests/` is not a package.

## 0.1.0b1

First beta.
