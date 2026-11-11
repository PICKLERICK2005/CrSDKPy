# 0.1.0b4 event correctness and metadata repair

Released on 2026-11-11. This document describes the general-purpose SDK changes,
compatibility contract and remaining callback limitations. Body-specific
acceptance is recorded in [A7 IV validation](A7IV_VALIDATION.md).

## Event correction

The bounded pre-S1 drain separates already-queued observations from the next
attempt. Same-batch focus selection considers the most recent state from each
independent channel, not the first focused notification. Warning/content and
other operation events are retained. Pre-S2 CaptureEvents stay in the public
stream for reconciliation but cannot confirm the subsequent release.
The gate also collects immediately available IPC pages before evaluating a
focus verdict, so a contradiction already waiting in the next page is included.
Continuous event production is bounded and rejects the gate with S1 cleanup;
the SDK never loops indefinitely waiting for a quiet queue or issues S2 anyway.

Native callback enqueue now assigns timestamps and unsigned 32-bit sequence
numbers under the queue mutex. The event's existing reserved word carries
sequence bits; its size stays 40 bytes. `Event.native` exposes immutable
`NativeEventInfo` with original time, order and raw vendor fields. Existing
`timestamp_ms` remains the backend delivery/observation time, so previous
latency calculations keep their clock domain. Event constructors, including
positional arguments, are unchanged; provenance does not affect equality.
Dataclass serializers now include the optional `native` diagnostic field, so
strict consumers of serialized event dictionaries should permit this additive
field. Manufactured/custom-backend events can leave it unset.

Additive timed native entry points return a 16-byte watermark immediately before
the existing SDK::SetDeviceProperty or SDK::SendCommand invocation. S1 is still
the existing property write; release remains the same DOWN/dwell/UP pair. The
host appends the watermark to the response metadata without changing the fixed
request/response layouts. No extra camera query, command, write or delay is
introduced by the native watermark. Cumulative drop counts are diagnostic;
they do not authorize replay of a lost exposure.

The focus gate and exposure wait compare callback enqueue order/time with the
corresponding S1/S2 watermark. Native times are never compared with Python times.
Sequence ordering uses modulo arithmetic, rejecting ambiguous distances at or
beyond half the counter range. Compare only within the same active session.
Simulator/custom backends without this optional provenance retain the queue
guards but cannot offer the native ordering guarantee.

### Remaining limits

The boundary records entry into an SDK call, not successful camera application
or physical S1/S2 actuation. Callback enqueue time is not a vendor-supplied
camera-generation timestamp. Vendor-delayed callbacks can arrive after a newer
boundary even when the underlying camera event is older. Separate IPC batches
can contain a later contradictory state that was not observable when a verdict
was returned. No absolute freshness or physical actuation claim is made.
The public CaptureEvent.sequence field is unchanged; the new callback-order
number is Event.native.sequence. Neither field identifies a photographic file.

## Separate metadata correction

PROPERTY_STRING and PROPERTY_VALUES now both obtain the property code from
request.u32_arg, matching Python and the protocol's existing contract. There
is no dual-field workaround or camera-specific special case. Nonzero string,
enum and range requests have independent regression tests. The C++ protocol
test validates the shared native field accessor with conflicting signed fields
and high-bit unsigned codes; Python fake-host tests validate value recovery.
Real-camera string, enum and range recovery passed on the A7 IV;
see [A7 IV validation](A7IV_VALIDATION.md).

## Native compatibility and deployment

- Python 0.1.0b4 requires native ABI 1.6 and IPC 1.3. Rebuild and deploy the host
  and bridge together beside the separately supplied Sony runtime.
- Timed entry points and boundary metadata are additive; legacy entry points
  and fixed POD/wire layouts are retained. New Python rejects older peers at
  negotiation. Older b3 clients can ignore the new host's response tails.
- Event constructors and delivery timestamps remain compatible. Serialized
  dataclasses include optional native provenance; strict consumers must permit
  that additive field.
- No shutter timing, automatic retry, photographic-setting or NR workaround
  is introduced. The default S2 hold remains 35 ms.
- Vendor runtime files and private acceptance evidence are not SDK source and
  must not be redistributed with the Python package.

## Counted-string correction

Sony CurrentStr begins with a uint16 payload count including its terminating
NUL. The bridge validates string type and framing, excludes the count word and
encodes UTF-16 to UTF-8, including surrogate pairs. Empty count 0 and count 1
plus NUL are supported; malformed termination or surrogates fail without partial
output. This is not an unconditional leading-character strip.

GetValueSize describes the separate advertised-values buffer, not CurrentStr.
The vendor supplies no independent CurrentStr allocation-size accessor, so the
bridge must trust the allocation's declared span. The standalone decoder tests
explicit spans and rejects truncated fixtures. Measured firmware/lens strings,
Unicode, empty values and malformed input have native regression coverage.

## Validation

Release acceptance used a wheel-installed candidate with matching rebuilt host
and bridge: four AF-S plus one MF exposure identified on ILCE-7M4, with exact
identities and clean shutdown. Both Remote and RemoteTransfer read-only metadata
checks passed. See A7IV_VALIDATION.md for the narrow hardware scope.

Release-time software validation passed 563 Python tests with one expected skip
(the absent-bridge case when a bridge is present), plus three hardware-free
CTest contracts: ABI layout, metadata IPC and counted strings. Release publishing
checks subsequently added five tests. Compatibility checks exercised old-client /
new-host negotiation and explicit rejection of old hosts by the new client.

Callback enqueue provenance cannot date camera event generation inside the
vendor runtime. A delayed callback may cross an operation boundary. Same-batch
contradictions and already-queued exposure notifications have software regression
coverage; successful captures do not by themselves prove every failure path.
Mechanical shutter / Long Exposure NR On readiness remains a separate issue.

