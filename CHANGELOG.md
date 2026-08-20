# Changelog

Dates are the day the change was validated, not the day it was released.

## 0.1.0b2 — 2026-08-20

A hardware session against the reference ILME-FX3A. Every fix below was
reproduced on the camera before it was written, and the record of what the body
does is in `docs/FX3_CHARACTERIZATION.md`.

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

- Opening a session no longer turns on the vendor's reconnection monitor by
  default. `Camera.open()` takes a `reconnect` policy, and the default,
  `ReconnectPolicy.BOUNDED`, leaves the monitor off so the call either connects
  or fails promptly. Previously it was always on and no caller could see or
  change that, which is what allowed a single open to block for the monitor's
  documented five minutes.
- `ReconnectPolicy.VENDOR` keeps the previous behaviour for callers who want a
  session to survive a cable event unaided, and accepts that worst case.
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
