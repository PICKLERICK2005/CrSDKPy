# Changelog

Dates are the day the change was validated, not the day it was released.

## 0.1.0b2 — 2026-08-20

A hardware session against the reference ILME-FX3A. Every fix below was
reproduced on the camera before it was written, and the record of what the body
does is in `docs/FX3_CHARACTERIZATION.md`.

### Fixed

- **A host-bound still had nowhere to go.** The vendor's own sample calls
  `SetSaveInfo` immediately after every successful `Connect`; CrSDKPy never did.
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
