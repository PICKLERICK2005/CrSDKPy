# CrSDKPy

An independent Python interface for Sony Camera Remote SDK (CRSDK).

> **Beta.** The public API is complete and intended to be stable for
> integration. Discovery, sessions, properties, events, still capture, gated
> autofocus, the content index with thumbnails and screennails, RAM postview,
> live view, movie recording and device status are implemented against both a
> deterministic simulator and a native out-of-process backend.
>
> **Hardware validation is tracked separately from implementation.** Which
> features have actually been exercised on a camera, and which have not, is
> recorded in [`docs/FEATURE_MATRIX.md`](docs/FEATURE_MATRIX.md). An
> implemented feature and a validated one are different claims and this
> project does not conflate them.

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

That wheel is pure Python and complete on its own for the simulator, the whole
public API, and anything built against it. No compiler, no Sony SDK.

**Driving a real camera additionally needs a `crsdkpy_host` executable, which
you build yourself.** It is not shipped as a wheel: the host links against the
Sony Camera Remote SDK, which is user-supplied under Sony's own terms and can
never be redistributed here, so there is no lawful prebuilt binary to publish.

The sources for it travel in the source distribution:

```console
pip download --no-binary :all: --no-deps CrSDKPy   # or clone the repository
cmake -S native -B native/build -DCRSDK_ROOT=/path/to/CrSDK/RemoteCli
cmake --build native/build --config Release
```

Put the Sony runtime and its `CrAdapter/` directory beside the built
`crsdkpy_host`, then point CrSDKPy at it if it is somewhere unusual:

```console
set CRSDKPY_HOST=...\native\build\crsdkpy_host.exe
```

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
```

Autofocus is gated: if focus is not confirmed, **no exposure is requested**.

```python
try:
    session.autofocus_and_capture()
except crsdkpy.AutofocusFailedError as exc:
    print("no photo taken:", exc.focus_state)
```

### Device status without vendor codes

Battery and media are the two readings almost every integration wants, and both
are vendor-encoded behind numeric codes. They are typed instead:

```python
session.battery          # BatteryStatus(87%)
session.storage          # (StorageSlot(1, ok, shots=1234),)
```

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
useful for diagnosis, and subject to the adapter constraint above — which is
why the hosted backend is the supported path. See
[`native/README.md`](native/README.md).

## Integrating

[`docs/INTEGRATION_CONTRACT.md`](docs/INTEGRATION_CONTRACT.md) states the
surface an application should bind to, and — more usefully — the vendor
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

| Area | State |
|---|---|
| Public API, capabilities, properties, events | Implemented, stable for integration |
| Capture, gated autofocus, content, previews, postview, live view, video | Implemented |
| Deterministic simulator with profiles and scenarios | Implemented |
| Native out-of-process backend | Implemented |
| Hardware validation | Partial — see [`docs/FEATURE_MATRIX.md`](docs/FEATURE_MATRIX.md) |

Known limitations:

- Validated on one body so far. A second body is the next validation step, as
  a test of the generic architecture rather than a new backend.
- RAM postview, live view, movie recording and destination writes are
  implemented and simulator-tested but **not yet hardware-validated**.
- Live view currently uses the same pipe transport as everything else. Whether
  that is sufficient is a measurement question; `session.live_view.measure()`
  exists to answer it, and no transport change will be made before it does.
- The bridge is Windows-tested only. The design is portable and the host uses
  no platform-specific transport, but no other platform has been built.
- `ContentsTransfer` mode is deliberately unimplemented; the classic
  small-size transfer path is not used.

## License

MIT - See [LICENSE](LICENSE).
