# Integration contract

What an application needs from CrSDKPy to drive a Sony body, and — more
usefully — what it must **not** need to know.

This is the surface an external camera abstraction should bind to. A worked
example lives in [`examples/camera_adapter.py`](../examples/camera_adapter.py),
which is executable and covered by tests.

## What an integrator uses

```text
crsdkpy.SDK(backend=...)          start / discover / close
  Camera                          identity, capabilities, open(mode, destination)
    Session                       one connection in one control mode
      .capabilities               what THIS session can actually do
      .properties                 read / write by numeric code when needed
      .events() / .drain_events() typed event stream
      .capture()                  release only
      .autofocus()                focus verdict, no exposure
      .autofocus_and_capture()    gated: no focus, no exposure
      .live_view                  status(), get_frame(), frames(), measure()
      .video                      start(), stop(), state
      .battery / .storage         device status
      .destination                where stills are written
      .close()                    idempotent
```

A `Capture` carries progress rather than a boolean, and previews come from it:

```python
capture = session.autofocus_and_capture()
if capture.exposed:
    content = capture.wait_for_content()      # durable file on the camera
    preview = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
```

## What an integrator must not need

None of the following appears in the path above, and if one of them ever does,
that is a defect in the abstraction rather than something to work around:

- the S1 half-press stage or the S2 release stage
- Sony command identifiers or their up/down parameters
- vendor callback enumerations, or the two disagreeing autofocus channels
- `RemoteTransfer` internals, content-list APIs or compressed-data types
- native handles, session handles or generation counters
- the CRSDK adapter directory, the host executable, or where either lives
- the IPC framing, the C ABI, or anything about the helper process

All of that is real and all of it is handled, but it is the library's problem.

The single vendor concept that legitimately reaches an integrator is the
**control mode**, chosen when the session opens, because it genuinely changes
what the camera can do and no abstraction can hide a decision the hardware
forces you to make. Ask the session what it can do rather than encoding the
consequences:

```python
with camera.open("remote") as session:
    if session.capabilities.live_view:
        ...
```

## The escape hatch

Vendor-specific work that CrSDKPy has no typed wrapper for stays behind
`session.raw`: numeric commands, the half-press stage, raw property writes.
It exists so an integrator is never blocked, and using it is a deliberate
choice rather than something ordinary code drifts into.

```python
session.raw.send_command(0, 1)        # a vendor command by number
session.raw.half_press                # the S1 stage, if you really need it
```

If ordinary operation requires `session.raw`, the missing capability should be
modelled properly instead.

## Capability names

Ask for these by name; unrecognised names are preserved rather than dropped, so
a backend describing something this release has never heard of stays usable.

| Name | Meaning |
|---|---|
| `still_capture` | The session can release the shutter. |
| `autofocus_s1` | A separate half-press stage exists, so focus can gate a release. |
| `live_view` | The stream can actually deliver a frame, not merely answer a query. |
| `content_index` | Durable content can be listed and identified. |
| `thumbnail`, `screennail` | Exact-still previews from the content index. |
| `postview_configuration` | The camera accepts being configured for postview. |
| `postview_delivery` | Postview bytes actually arrive. Independent of the above. |
| `video` | The body reports a recording state, so recording can be driven safely. |
| `raw_commands` | The escape hatch is available. |

## Behaviour an integrator should rely on

- **Capabilities are per session**, and change with control mode and still
  destination. Re-read them after `set_destination`.
- **A capture is not a boolean.** Command acceptance, exposure, durable content
  and a preview are four separate facts, each separately observable.
- **A live-view frame is never the captured still**, whatever its timing.
  `Preview.is_exact_still` says which forms are.
- **Preview bytes are not a stable identity.** Compare `content_id`, never a
  hash of the image: at least one body regenerates an embedded identifier on
  every transfer.
- **Unsupported operations raise** `UnsupportedOperationError` naming the
  capability, rather than returning something falsy.
- **A busy camera raises `CameraBusyError`, not a connection error.** It is
  transient and the same call is expected to work shortly afterwards, so do not
  tear the session down over it. The vendor code is on `backend_code`; the first
  content listing after opening a RemoteTransfer session is the case that
  actually occurs, failing in about a millisecond with `0x8D05`.
- **A reconnect does not require a disconnect.** The state machine must accept
  `connected -> reconnecting -> connected` with no disconnect notification in
  between, because that is what the reference body does.
  `ConnectionEvent.recovered` is set only on a recovery; it is **not** set on a
  first connect. `ConnectionEvent.connection_version` is the reverse: reported
  on a first connect and absent from a recovery. Watch `recovered` if you need
  to resynchronise state after a link came back.
- **A still whose destination includes the host needs a save directory**, which
  the library configures for you. Override it with `CRSDKPY_SAVE_DIR` or
  `HostBackend(save_directory=...)`; the default is a directory under the
  system temporary directory. Without one configured the camera announces no
  postview and leaves the destination property unsettable for the rest of the
  session, so this is not optional -- but it is handled, and an integrator does
  not have to think about it unless they want the files somewhere specific.
- **Writing the still destination is not immediately observable.** Reading it
  back in the next statement returns the old value; the change takes 100-200 ms
  to be reported. Poll for it rather than trusting one read.
- **The property count is a live figure.** Never assert it.
- **Closing anything is idempotent.**
