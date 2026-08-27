# CrSDKPy

An independent Python interface for Sony Camera Remote SDK (CRSDK).

> **Beta, and still growing.** The core -- discovery, sessions, properties,
> events, capture, autofocus, content, previews, live view, video -- has been
> stable across releases and is what an integration should bind to. Newer
> surfaces are additive and less settled; see the Status table below for which
> is which.
>
> **Implemented, tested and hardware-validated are three different claims**
> and this project does not conflate them. What has actually been exercised on
> a camera is recorded in [`docs/FEATURE_MATRIX.md`](docs/FEATURE_MATRIX.md);
> a good deal of the newer surface is simulator-tested only and is marked so.

CrSDKPy is a general-purpose library, not a wrapper around one camera.
Capabilities are discovered at runtime so there is no model-name branching
anywhere in the codebase.

```text
Hardware validated:  ILME-FX3A, firmware 2.02, CRSDK 2.02.00, USB

Other CRSDK-compatible bodies:
                     expected to work according to the capabilities they
                     report, but not yet hardware-validated
```

## Sony Camera Remote SDK dependency

Sony Camera Remote SDK is an external dependency. Users are responsible for
obtaining it from Sony, accepting its terms, and supplying an appropriate SDK
installation for their platform when using a real camera.

This repository and its distributions do **not** redistribute Sony headers,
libraries, DLLs, samples, documentation, or any other Sony SDK files.

CrSDKPy is not affiliated with, endorsed by, or sponsored by Sony Corporation
or any of its affiliates. Sony and Camera Remote SDK are trademarks or names
of their respective owners.

## Installation

```console
python -m pip install CrSDKPy
```

That wheel is pure Python. It is everything you need to develop against the
simulator, and every API in this document imports and works without a camera.
No compiler, no Sony SDK.

**Driving a real camera additionally needs a `crsdkpy_host` executable, which
you build yourself.** It is not shipped as a wheel: the host links against the
Sony Camera Remote SDK, which is user-supplied under Sony's own terms and can
never be redistributed here, so there is no lawful prebuilt binary to publish.

The sources for it travel in the source distribution, so either clone the
repository or unpack an sdist:

```console
pip download --no-binary :all: --no-deps CrSDKPy
tar xf CrSDKPy-*.tar.gz && cd CrSDKPy-*

cmake -S native -B native/build -DCRSDK_ROOT=/path/to/CrSDK/RemoteCli
cmake --build native/build --config Release
```

Then copy the Sony runtime **and its `CrAdapter/` directory** into the same
directory as the built `crsdkpy_host`. That placement is not a preference:
the vendor SDK resolves `CrAdapter` against the host executable's own
directory, so a runtime staged anywhere else is not found no matter what you
point at it.

```console
set CRSDKPY_HOST=...\native\build\crsdkpy_host.exe
```

To check all of that before involving a camera:

```console
python -m crsdkpy
```

It reports the package, the bridge, the host, and whether the Sony runtime is
staged where the host will look, naming the directory it inspected and what
was missing. It loads no vendor code, so it works when nothing else does.

Nothing from Sony is bundled, downloaded or committed by this project, and no
Sony file ever needs to sit next to your Python installation.

## Quick start

Everything below runs against the simulator, with no hardware and no Sony SDK.

```python
import crsdkpy

with crsdkpy.SDK(backend="simulator", profile="fx3a") as sdk:
    camera = sdk.discover()[0]
    print(camera.info)                      # ILME-FX3A SIM000000 (usb)

    with camera.open("remote") as session:
        caps = session.capabilities
        if caps.live_view:
            frame = session.live_view.get_frame()
            print(frame)                    # LiveViewFrame(#1, 77402 bytes, 640x428)

        capture = session.autofocus_and_capture()
        print(capture.state)                # CaptureState.EXPOSED
```

### Capabilities are discovered, not assumed

The same camera exposes different capabilities depending on control mode *and*
on where stills are saved. Always ask the session:

```python
with camera.open("remote_transfer") as session:
    caps = session.capabilities
    caps.live_view       # False in this mode on this body
    caps.screennail      # True here, False in "remote"

    session.set_destination(crsdkpy.StillDestination.HOST_AND_MEMORY_CARD)
    caps = session.capabilities          # recomputed: destination changed it
```

### A capture is not a boolean

Command acceptance does not mean a photo was taken, and an exposure does not
mean durable content exists yet. These are separate, observable facts:

```python
capture = session.autofocus_and_capture()
capture.exposed                       # the camera confirmed an exposure
content = capture.wait_for_content()  # durable media on the card
preview = capture.preview(crsdkpy.PreviewKind.SCREENNAIL)
assert preview.is_exact_still

# Original downloads are explicit, asynchronous, and require the documented
# RemoteTransfer content mode.
transfer = session.content.download(content, "captures/DSC00001.ARW")
path = transfer.wait(timeout_ms=60_000)
```

Only one original-file download may be active per session. A second start is
rejected with `CameraBusyError`; `wait()` timing out does not cancel the camera
operation, and `cancel()` is explicit and idempotent. Existing destination
files are refused unless `overwrite=True` is requested.

Autofocus is gated: if focus is not confirmed, **no exposure is requested**.

```python
try:
    session.autofocus_and_capture()
except crsdkpy.AutofocusFailedError as exc:
    print("no photo taken:", exc.focus_state)
```

### Absolute focus position

When the camera/lens exposes Sony's documented focus-position properties, the
session provides a small generic facade:

```python
session.focus.position          # opaque camera/lens position integer
session.focus.range             # PropertyRange reported by the camera, or None
session.focus.is_driving        # True, False, or None for an unknown status

session.focus.move_to(30_600)   # starts a non-blocking absolute move
position = session.focus.wait_until_settled(timeout_ms=3_000)
session.focus.move_relative(-100)  # current absolute position plus delta
```

These values are not distances, diopters, millimetres, or normalized physical
positions. Moves are validated against the property's advertised metadata;
unsupported and read-only camera/lens combinations fail explicitly.

Deterministic acquisition across caller-selected positions is available without
adding any stacking or distance assumptions:

```python
bracket = session.focus.capture_bracket(
    [30_000, 30_200, 30_400],
    restore_start=True,
)
for frame in bracket.frames:
    print(frame.commanded_position, frame.settled_position, frame.capture.state)
```

Each entry receives exactly one shutter attempt. Missing exposures and exposed
frames whose durable identity cannot be resolved remain distinguishable through
the original `Capture` object. Movement or restoration failures raise
`FocusBracketError` with the partial `result` attached.

### Device status without vendor codes

Battery and media are the two readings almost every integration wants, and both
are vendor-encoded behind numeric codes. They are typed instead:

```python
session.battery          # BatteryStatus(87%)
session.storage          # (StorageSlot(1, ok, shots=1234),)
```

### Typed camera settings

Exposure, white-balance, and drive controls are live views over the generic
property layer:

```python
session.exposure.iso.value
session.exposure.shutter_speed.allowed_values
session.exposure.f_number.writable
session.exposure.compensation.value_range
session.exposure.program.set(1)

session.white_balance.mode.value
session.drive.mode.value
```

Every setting also exposes `available`, `accepts(value)`, and `set(value)`.
Access and constraints come from the camera's current property metadata, not
its model name. Unknown current numeric values remain unchanged even when they
are absent from `allowed_values`; advertised constraints govern requested
writes only. A `values_undecoded` setting remains readable but reports that
trustworthy client-side validation is unavailable.

Values are Sony CRSDK's raw property representation. CrSDKPy does not guess
seconds, f-stops, EV, Kelvin, or physical units from numeric codes — but where
the vendor documents an encoding, that encoding is the contract:

| Setting | Vendor encoding |
| --- | --- |
| `f_number` | F-number x 100, so `280` is F2.8. `0xFFFD` iris closed, `0xFFFE` unknown, `0xFFFF` nothing to display. |
| `compensation` | Signed EV x 1000, so `-700` is -0.7 EV. |
| `shutter_speed` | Upper 16 bits numerator, lower 16 bits denominator, so `0x000103E8` is 1/1000. `0` is bulb, `0xFFFFFFFF` nothing to display. |
| `iso` | Bits 0-23 the ISO value, bits 24-27 the mode, bits 28-31 the extension flag. A value of `0xFFFFFF` is ISO AUTO. |
| `program`, `white_balance.mode`, `drive.mode` | Vendor enumerations, listed in the CRSDK API reference. |

Writability follows camera state rather than model: the vendor documents
`f_number` as settable in exposure modes M and A and read-only in P and S, and
that is what `writable` reports. Writing `program` changes what the other
settings accept, and the vendor asks for 500 ms after that write before setting
a shooting parameter such as `shutter_speed`. A write is asynchronous — the
camera reports the new value through the event stream, not through `set()`.

### Lens

Identity is three ordinary string properties and needs no request:

```python
session.lens.model_name      # 'FE 50mm F1.4 GM'
session.lens.version         # '01'
session.lens.serial_number
```

The lens's focus-distance table is a separate feature with its own protocol.
The vendor allows the request only while the camera reports the precondition,
reports the outcome as a warning and nowhere else, and permits one read per
request. `load_information()` does the whole documented sequence:

```python
if session.lens.information_available:          # the camera's own enable flag
    for row in session.lens.load_information():
        row.unit                                 # 'meter' | 'feet' | None
        row.normalized_value                     # matches the follow-focus value
        row.focus_position                       # distance, in the camera's units
```

A lens that carries no distance data raises `UnsupportedOperationError` — the
vendor reports the same status for that and for a read with no successful
request behind it, so the message says both. That is a lens limitation, not a
body or API one.

### Zoom

Every documented zoom control is a property, so support is a runtime question
the camera answers. A body can expose the whole family with a lens that cannot
motorise anything:

```python
session.zoom.available          # the camera exposes the family
session.zoom.operable           # a drive would meet its precondition now
session.zoom.type_status.name   # 'optical' | 'smart' | 'clear_image' | 'digital'
session.zoom.speed_range        # what a drive will accept, per body

session.zoom.operate(1)         # tele; negative is wide
session.zoom.stop()
session.zoom.move_to(20_000)    # absolute; watch session.zoom.is_driving
```

A drive is refused when the camera does not currently allow one, rather than
sent for the camera to ignore. Scale, digital scale and focal distance stay in
the camera's own units: `scale` counts in thousandths, so `1200` is x1.2.

### Electronic framing

The one family here with real operations, and the vendor documents an order
between them — a relative update is enabled only after a successful execute
and cannot move a frame on its own:

```python
from crsdkpy import FramingArea, FramingRectangle, FramingType

session.framing.available                  # the camera says it is executable
session.framing.execute(
    horizontal_denominator=640,
    vertical_denominator=480,
    framing_type=FramingType.AUTO,
    input_areas={1: FramingRectangle(1, 1, 640, 480)},
    output_areas={1: FramingRectangle(0, 0, 640, 480)},
)
session.framing.update_area(1, FramingArea.INPUT, x=10, y=-10)
```

Coordinates are plain numbers. The vendor states that every coordinate and
denominator is multiplied by 1024 on the wire, so that is applied for you —
and a value that would not survive the vendor's narrower relative-update
parameter is refused rather than silently truncated, which is what its own
sample does. Nothing here infers crop or aspect behaviour from a property name.

### Interval shooting

The camera owns the sequence. There is no start or stop operation in the vendor
SDK and no scheduler here — arming is a property write and progress is a
read-only status the camera updates:

```python
session.interval.number_of_shots.set(120)
session.interval.shooting_interval.value_range   # what this body accepts
session.interval.enable()
session.interval.status.name                     # 'waiting_start' | 'shooting'
```

Values stay in the camera's units. The vendor documents the shooting interval
as ten times the value in seconds, so `300` is 30 s; it describes the start
time as seconds while giving a maximum that only holds at a tenth of a second,
so that one is genuinely ambiguous and is left exactly as reported.

### Still transfer size

What a host-bound still is delivered as:

```python
session.still_transfer.size.value      # 0 Original, 1 SmallSize
session.still_transfer.is_original     # True | False | None
```

The reference body reported `SmallSize`, which is why a host-bound capture
arrived downscaled rather than as the captured file. Whether that body honours
`Original` has not been tested on hardware, so this exposes the setting and
claims nothing about the result.

### Conformance profiles

What one body was measured to expose, sanitized and tracked, plus the machinery
to compare another body against it:

```python
profile = crsdkpy.reference_profile()          # the measured ILME-FX3A
profile.body["firmware"]                        # '2.02'
profile.codes_for("remote+memory_card")         # 394 codes
profile.unknown_codes                           # frozenset({0x0581, 0x0582})

report = crsdkpy.compare(profile, observation)
report.conformant                               # no declared invariant broke
report.of_kind(crsdkpy.FindingKind.NEW_PROPERTY)
```

A comparison is not an assertion that every camera is the reference body. A
different Sony body is *expected* to differ, so extra properties, changed
access, and changed value sets come back as information. Only a small set of
declared invariants can fail a comparison, and the property count is
deliberately not among them -- one body reported 394 in Remote and 392 in
RemoteTransfer, so a count is an observation and never a health check.

The workflow for a body nobody here owns:

```
tools/characterize_camera.py       # capture, stays private
tools/sanitize_characterization.py # -> the tracked profile, once per body
tools/compare_profile.py           # -> what this body does differently
```

Capture and sanitization are separate commands on purpose: the capture carries
the body serial and host details, and the moment that becomes publishable is
one explicit step rather than a side effect.

### Raw escape hatch

Vendor features CrSDKPy has not modelled stay reachable, including property
codes it has no name for:

```python
session.raw.get_property(0x0581)
session.raw.send_command(0xD2FF, crsdkpy.CommandParameter.DOWN)
session.raw.call("lens_information")
```

## Simulator

The simulator is a first-class feature intended for day-to-day development
without hardware. It is behavioural, deterministic, and runs on a virtual
clock, so a 28-second reconnect costs no wall-clock time.

```python
from crsdkpy.simulator import Scenario, AfOutcome

sdk = crsdkpy.SDK(
    backend="simulator",
    profile="inverted_modes",
    scenario=Scenario(af_outcome=AfOutcome.NO_LOCK, content_id_step=2),
)
```

Profiles: `fx3a`, `minimal_still`, `inverted_modes`, `future_unknown`.
The last two deliberately contradict the first characterized body, so that
hard-coding its behaviour fails the test suite.

Scenarios cover focus-channel ordering and disagreement, sticky focus values,
autofocus failure, accepted commands that never expose, delayed and
non-contiguous content, stale previews, live-view failures, busy responses,
reconnects without a disconnect, and unknown event codes.

## Using a real camera

Once `crsdkpy_host` is built (see [Installation](#installation)), select it:

```python
with crsdkpy.SDK(backend="host") as sdk:
    camera = sdk.discover()[0]
```

The helper process is not a stylistic choice. The vendor SDK resolves its
transport-adapter directory against the **host executable's** directory, which
a library cannot change for an interpreter it did not start; supplying our own
executable is the only thing that satisfies it. Running vendor code out of
process also means a native fault reports as a backend error instead of taking
the interpreter with it.

`backend="native"` loads the same bridge in process instead. It is lower level,
useful for diagnosis, and subject to the adapter constraint above, which is
why the hosted backend is the supported path. See
[`native/README.md`](native/README.md).

## Integrating

[`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md) states the
surface an application should bind to, and, more usefully, the vendor
machinery it must never need: no S1 or S2, no command enumerations, no control
mode internals, no native handles, no IPC.

[`examples/camera_adapter.py`](examples/camera_adapter.py) is a runnable
adapter demonstrating exactly that. It works against the simulator:

```console
python examples/camera_adapter.py
```

## Documentation

* [`docs/FEATURE_MATRIX.md`](docs/FEATURE_MATRIX.md): what is implemented,
  what is simulator-tested, and what has been validated on real hardware.
* [`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md): the surface
  to build an application against.
* [`docs/architecture.md`](docs/architecture.md): architecture, Camera vs
  Session, backend contract, capability model, simulator, event model, capture
  lifecycle.
* [`docs/FX3_CHARACTERIZATION.md`](docs/FX3_CHARACTERIZATION.md): the hardware
  measurements the design is based on.
* [`tools/hardware_validation.py`](tools/hardware_validation.py): the gates
  that only a real camera can answer. Run `python tools/hardware_validation.py
  --list` to see the stages.

## Development

```console
python -m venv .venv
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m build
python -m twine check dist/*
```

## Status

Three separate columns, because they are three separate claims. "Tested" means
the automated suite covers it against the simulator, the fake host, or both;
"hardware" means it has been exercised on a real camera.

| Area | Implemented | Tested | Hardware |
|---|:---:|:---:|:---:|
| Discovery, sessions, properties, events | yes | yes | **yes** |
| Still capture, gated autofocus, device status | yes | yes | **yes** |
| Content index, thumbnails, screennails | yes | yes | **yes** |
| RAM postview, live view, movie recording | yes | yes | **yes** |
| Destination read/write, busy classification | yes | yes | **yes** |
| Deterministic simulator, profiles, scenarios | yes | yes | n/a |
| Native and out-of-process backends | yes | yes | **yes** |
| Original-file downloads | yes | yes | **yes** ¹ |
| Reconnection policy (`BOUNDED` / `VENDOR`) | yes | yes | vendor only ² |
| Absolute focus position, focus brackets | yes | yes | no |
| Typed exposure / white-balance / drive settings | yes | yes | no |
| Lens identity | yes | yes | **yes** |
| Lens focus-distance table | yes | yes | no ³ |
| Zoom state, drive, absolute position | yes | yes | no ⁴ |
| Electronic framing | yes | yes | no |
| Interval shooting, still-transfer size | yes | yes | no |
| Conformance profile and body comparison | yes | yes | reference body only |

¹ The transfer itself is proven; the download lifecycle's public callback and
cancellation paths are not.
² The vendor policy is what every hardware session used. `BOUNDED` has never
been exercised on a camera.
³ The reference lens reports the feature as disabled, so the request path has
never had a body that would answer it.
⁴ The family is exposed on the reference body, but its lens cannot motorise
anything, so no zoom has ever moved.

Known limitations:

- Validated on one body so far. A second body is the next validation step, as
  a test of the generic architecture rather than a new backend.
- Everything added after the core is simulator- and protocol-tested but, with
  the exceptions marked above, **not yet hardware-validated**. Treat those
  surfaces as working-by-construction rather than proven.
- Live view currently uses the same pipe transport as everything else. Whether
  that is sufficient is a measurement question; `session.live_view.measure()`
  exists to answer it, and no transport change will be made before it does.
- The bridge is Windows-tested only. The design is portable and the host uses
  no platform-specific transport, but no other platform has been built.
- `ContentsTransfer` mode is deliberately unimplemented; the classic
  small-size transfer path is not used.

## License

MIT - See [LICENSE](LICENSE).
