# CrSDKPy architecture

Developer documentation for the library itself. Hardware measurements live in
`docs/characterization/`; this document only refers to them where a design
decision depends on one.

```
Sony camera
  -> Sony Camera Remote SDK
    -> thin native C++ bridge, strict C ABI
      -> backend contract
        -> generic CrSDKPy public API
          -> optional application adapters
```

## Layers

| Layer | Module | Responsibility |
|---|---|---|
| Entry point | `crsdkpy.sdk` | Owns a backend, discovers cameras |
| Identity | `crsdkpy.camera` | Persistent device identity |
| Connection | `crsdkpy.session` | One control-mode connection and everything scoped to it |
| Facades | `liveview`, `video`, `content`, `raw`, `capture` | Feature-shaped access to a session |
| Values | `properties`, `previews`, `events`, `capabilities`, `status`, `enums` | Plain data |
| Helpers | `crsdkpy._jpeg` | Structural validation of returned image bytes |
| Contract | `crsdkpy.backend.contract` | The only interface a driver implements |
| Drivers | `crsdkpy.simulator`, `crsdkpy.backend.host`, `crsdkpy.backend.native` | Simulator, hosted vendor SDK, in-process bridge |

Nothing above the contract knows a vendor code, and nothing below it knows
about the ergonomic API.

## Camera versus Session

A `Camera` is the persistent logical device. A `Session` is one open
connection in one control mode.

This split is mandatory rather than cosmetic. The control mode is fixed when
the connection is opened and there is no API to change it afterwards, so
switching modes means closing one connection and opening another. If `Camera`
were the connection, every mode switch would invalidate the user's object.

```python
camera = sdk.discover()[0]

with camera.open("remote") as session:          # live view lives here
    ...
with camera.open("remote_transfer") as session:  # content index lives here
    ...
# `camera` is still valid, and its device_key never changed.
```

`Camera.capabilities` describes what the body can do at all.
`Session.capabilities` describes what this connection can do right now.

## Connection states

`connecting -> connected -> reconnecting -> connected -> closing -> closed`

The important property is that `connected -> reconnecting -> connected` is a
legal path with **no** intervening closed state. On real hardware a transport
loss produced a reconnecting signal, then recovery, sometimes with a second
connected event and no disconnect at all. Nothing may require a disconnect
before accepting recovery.

`Session.wait_for_state()` polls the state rather than waiting for a specific
event sequence, which is the reliable way to ride out a reconnect.

## Capability model

Capabilities are discovered, never derived from a model name. There is no
model branching anywhere in the library.

A session's capabilities are a function of:

```
camera + control mode + still destination + runtime observation
```

Control mode and destination are **independent axes**. On the first
characterized body, live view exists only in one mode, the content index only
in the other, and postview *delivery* depends on the destination while
postview *configuration* depends on the mode. Those two postview facts
disagree in both directions, which is why they are two separate capabilities:

| | configure | deliver |
|---|---|---|
| remote + card | no | no |
| remote + host+card | **no** | **yes** |
| transfer + card | **yes** | **no** |
| transfer + host+card | yes | yes |

Collapsing that into one boolean would be wrong on three of four cells.

Unrecognised capability names survive in `SessionCapabilities.extra` and are
reachable with `caps.get("name")`, so a backend can describe a feature this
release has never heard of.

## Property model

Properties are numeric codes. `PropertyCode` wraps any integer and names it
only if CrSDKPy recognises it; unknown codes are ordinary values, not errors.
This is not hypothetical — real hardware reported two codes that do not appear
in the vendor's own device-property enumeration.

**Property count is never a health assertion.** It differs by control mode on
a single body.

## Event model

Events are a stream of typed facts. The rules the model enforces:

* no ordering guarantee between channels;
* no guarantee a notification arrives at all;
* no guarantee a value is current when read.

Autofocus is the clearest case. Two independent channels report focus using
different vendor enumerations. Either may lead, and they have been observed
transiently disagreeing, with the property still reporting tracking after the
other channel reported focus. So the gate consumes both and latches the first
accepted state from either, plus a few spaced direct reads to defeat the case
where an already-focused value never notifies at all.

A session keeps two internal queues fed from one drain, so an internal wait
never consumes the caller's event stream.

## Capture lifecycle

```
requested -> focusing -> focused -> exposed -> content_available -> preview_available
                      \-> failed
```

Four separately observable facts, each able to fail alone:

1. the command was accepted — proves nothing;
2. autofocus confirmed;
3. an exposure completed;
4. durable content exists and a preview can be pulled.

`Capture` reports progress rather than success. A `capture()` that returns
`True` while no photo was taken is the specific failure this design exists to
prevent — it was observed on hardware, where an accepted release in an
autofocus mode produced no exposure at all.

New content is matched by `id > baseline`, never `baseline + 1`: identifiers
are monotonic but have been seen to skip.

## Autofocus

The shutter has two stages. The release command drives the full press only;
the half press is a separate control that starts autofocus.

```
clear stale half press
  -> engage half press
    -> wait for an accepted focus state (both channels + spaced reads)
      -> release down, ~35 ms, release up      [only if focus confirmed]
        -> exposure event
          -> durable content
```

Accepted states are the two focused ones. A tracking state is *in progress*,
not success; treating it as success would fire early.

Cleanup differs by path and is verified rather than assumed: a successful
release clears the half press by itself on some bodies, while a failed
autofocus leaves it engaged and must be released explicitly.

The vendor also offers a combined half-press-and-release command. It works,
but it is **ungated** — the exposure is committed before any code can inspect
the focus result. It is therefore only available through `session.raw`, never
behind a name implying focus was checked.

## Raw escape hatch

`session.raw` exists because the vendor SDK gains features faster than a
wrapper can model them. It allows unknown numeric property codes and commands,
still enforces session state and lifetime, exposes no native pointers, and
offers `raw.call(operation, **payload)` for vendor features that are neither
property- nor command-shaped.

## Backend contract

`crsdkpy.backend.contract.Backend` is the only interface a driver implements.
It is shaped so a native bridge satisfies it unchanged:

* sessions are opaque string ids, never pointers;
* all values crossing the boundary are plain data a C ABI layer can build;
* events are **pulled from a queue**, so vendor callbacks can stay on native
  threads and hand Python a drained batch;
* image bytes are copied out by the backend — no caller-owned buffers, no
  vendor-owned memory;
* `close_session` is idempotent;
* the backend owns the clock, which is what makes virtual time possible.

Semantic operations (`set_half_press`, `focus_state`, `battery`, `storage`,
`recording_state`) exist so the public layer never needs to know a vendor
property code or value encoding.

### What needed no new ABI

Several features look like they need bridge support and do not, because the
vendor already models them as ordinary properties and commands. Recognising
that kept the C ABI smaller and put the behaviour in one shared place instead
of two:

| Feature | How it is actually expressed |
|---|---|
| Half-press stage, focus reads | Properties `0x0001` and `0x0707` |
| Still destination | Property `0x0119` |
| Battery, media slots | Properties in the read-only `0x07xx` block |
| Movie recording | The record command plus property `0x0705` |

The recording control is a *button*, so it toggles. Both `start_recording` and
`stop_recording` therefore check the observed state first: a blind second start
would stop the recording, which is precisely the bug this avoids.

### What did need new ABI

The content index, compressed previews, postview and live view all involve
vendor-owned memory, asynchronous delivery, or both, so each is a bridge
function that copies bytes out before returning:

* **Content index** — the vendor's list is walked and every field copied while
  the list is still alive, then released. No vendor pointer is retained.
* **Compressed previews** — delivered through a callback that identifies
  neither the request nor the content. Association is therefore *structural*:
  one transfer per session at a time, and a delivery arriving with nothing in
  flight is discarded rather than handed to whoever asks next.
* **Postview** — announced with a size, then pulled. The announcement is
  recorded in the callback; the pull happens on the caller's thread, so no
  vendor call is made from inside a vendor callback.
* **Live view** — the bridge hands the vendor a buffer *it* owns, so there is
  no vendor allocation to copy out of at all.

Sessions are held by `shared_ptr` so a call that must release the global lock
while it blocks — a preview transfer runs for a large fraction of a second —
keeps its session alive. A concurrent close then makes the wait fail instead of
freeing the object underneath it.

## Simulator

A first-class feature, not a test fixture, and deliberately behavioural rather
than a set of canned returns.

**Profiles** describe what a camera *is*: modes, per-mode capabilities,
properties, previews, representative timings. Four ship:

| Profile | Purpose |
|---|---|
| `fx3a` | Mirrors the first characterized body |
| `minimal_still` | No half press, no video, no transfer mode, small property set |
| `inverted_modes` | Deliberately contradicts `fx3a`: live view in transfer mode, content index in remote, destination-independent postview |
| `future_unknown` | Unnamed property codes, unknown capabilities, untyped events |

`inverted_modes` exists specifically so that hard-coding the first body's
mode/capability mapping fails the test suite.

**Scenarios** describe what *happens*: focus channel ordering, transient
disagreement, tracking-before-focus, sticky focus values, no-lock, a half press
left engaged, accepted commands with no exposure, delayed content,
non-contiguous ids, stale previews, live-view fetch failure, an info call that
succeeds while reporting nothing, busy responses, reconnects without a
disconnect, straggler property events, and untyped events. They compose with
`Scenario.replace(...)`.

**Clock.** The simulator defaults to a `VirtualClock`, so a 28 second reconnect
resolves instantly and nothing is timing-dependent. The whole suite runs in a
fraction of a second. `RealClock` is available for live demonstrations.

## The host process

```
Python process                 crsdkpy_host process
  public API                     C ABI bridge
  HostBackend      <-- pipes -->   vendor SDK
                                   CrAdapter/
```

### Why it exists

Two reasons, one forced and one earned.

**Forced.** The vendor SDK resolves its transport-adapter directory against the
*host executable's* directory. Established by elimination on one machine and
one SDK version:

| Configuration | `EnumCameraObjects` |
|---|---|
| Executable beside `CrAdapter/` | success |
| Same executable, unrelated working directory | success |
| Bridge loaded by an interpreter not beside `CrAdapter/` | `0x8703` |
| ...with the working directory set to the adapter directory | `0x8703` |
| ...with `SetDllDirectory` pointing at it | `0x8703` |
| ...with `CrAdapter/` beside the interpreter binary | success |

A library cannot move the user's interpreter, so it supplies its own executable
and puts that beside the runtime instead. Both in-process mitigations were
implemented, measured and removed once they proved ineffective.

This is a workaround for *observed* behaviour. It is not a claim that every
CRSDK version or platform resolves adapters identically.

**Earned.** Even if that constraint disappeared, the host would be worth
keeping: vendor code runs in a replaceable process, so a native fault reports
as a backend error instead of taking the interpreter down. A vendor crash was
already observed killing a probe during characterization.

### Transport: the child's stdin/stdout pipes

Chosen over named pipes and a loopback socket because it is:

* **local by construction** — no socket, no port, no network surface at all;
* **self-diagnosing** — process death is a clean EOF, needing no liveness ping;
* **portable** — identical on Windows and POSIX, so a future Linux or macOS
  host needs no second transport;
* **testable** — a pure-Python fake host speaks it, so every protocol,
  lifecycle and error test runs in CI with no native build and no camera.

The obvious hazard is vendor code printing to stdout and corrupting the frame
stream. The host duplicates the real stdout handle for exclusive protocol use
and redirects C-level stdout to the null device before touching the vendor
library, so stray vendor output cannot interleave.

### Framing

```
[ header 24 bytes ][ meta meta_len ][ blob blob_len ]

magic 'CRPY' | version_major | message_type | request_id | meta_len | blob_len
```

There is no JSON. Metadata and payload items are the same fixed-layout POD
already defined in `crsdkpy_abi.h`, so both sides describe the format once and
neither needs a parser. `meta` and `blob` lengths are separate specifically so
image buffers stream as themselves rather than encoded into text.

Message types: `HELLO`, `HELLO_ACK`, `REQUEST`, `RESPONSE`, `EVENT`, `BYE`.
Each request carries an id; the matching response echoes it. Unsolicited event
frames arriving while a request is outstanding are buffered rather than
mistaken for the response.

### Version negotiation

The handshake exchanges the IPC protocol version, the C ABI major/minor, a host
build string, and whether the vendor SDK loaded. A mismatch in **either** the
protocol major or the ABI major is refused with an explanation naming which
side to rebuild. Nothing continues across an incompatible major.

### Process lifecycle

`not_started -> starting -> ready -> stopping -> stopped`, plus `crashed`.

Unexpected death is detected on the next operation, through either a poll of
the child or an EOF mid-frame, and surfaces as a backend error. A crashed host
makes `connection_state` raise rather than report `closed`: an orderly closed
session and a vanished backend are different facts and must not look alike.
There is no automatic restart.

### Identity across the boundary

Camera identity remains the opaque `device_key`; session identity remains an
opaque string id. Neither carries a PID, an address, or a vendor pointer. The
underlying handle embeds a generation counter, so a retired session id fails
cleanly instead of aliasing a newly opened one.

### Error transport

Responses carry a normalized category alongside the raw vendor code, the
operation and a diagnostic string, so Python reconstructs its own exception
hierarchy without pattern-matching on text. The known adaptor-create failure
has its own category and keeps its specific explanation rather than degrading
into a generic vendor error.

### Runtime staging and the vendor-file policy

The host executable is built into a directory where the user has placed their
own copy of the vendor runtime and `CrAdapter/`. CrSDKPy locates and validates
that directory; it never downloads, embeds, or redistributes vendor files, and
it never writes into the user's Python installation. The repository and the
wheel contain only first-party code.

## Testing

Run `pytest`. Every test uses the simulator on virtual time.

The generality tests are the architectural guard: if the suite passes only
against the `fx3a` profile, the design is not generic enough.
