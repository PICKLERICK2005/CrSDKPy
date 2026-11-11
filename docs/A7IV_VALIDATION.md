# Sony A7 IV validation — CrSDKPy 0.1.0b4

Measured on 2026-11-11 using the wheel-installed release candidate and matching
native ABI 1.6 / IPC 1.3 host and bridge. Published distributions retain those
exact artifact hashes. Sony ILCE-7M4 firmware 6.01, FE 20–70mm F4 G firmware 02,
CRSDK 2.02.00 over USB on Windows. Electronic shutter, existing photographic
settings unchanged, default 35 ms S2 hold, no automatic retries.

## Capture acceptance

| Path | Identified exposures | Focus evidence | Measured S2 hold |
|---|---:|---|---|
| Gated AF-S | 4/4 | Focused AF-S, status-warning channel; gate elapsed 251–282 ms | 35.51–35.69 ms |
| MF / S2-only | 1/1 | Effective MF; no S1 assertion or focus-gate invocation | 35.34 ms |

Each capture had exactly one S2 DOWN/UP pair, an authoritative post-release
Captured_Event and a distinct exact native content identity. S1 cleanup was
verified after AF-S, and all five sessions closed with stopped SDK and host
exit code 0. No uncertain/unresolved outcome occurred in this acceptance gate.
Raw traces and capture records are retained privately; the five-frame sample
does not establish universal reliability or validate other capture settings.

AF-S required both a lens configured for AF and effective body AF-S. A physical
lens MF switch can make the effective mode MF despite an AF-S menu selection.
Earlier AF-S-labelled captures made with the lens in MF remain MF evidence and
are excluded from the four AF-S passes above. Camera/lens controls were changed
by the operator; the SDK did not rewrite focus, shutter or exposure settings.

## Metadata acceptance

Read-only Remote and RemoteTransfer sessions recovered camera firmware `6.01`,
lens name `FE 20-70mm F4 G`, lens firmware `02`, and advertised enum/range values
through the corrected IPC endpoints. Effective focus properties were recorded
as numeric values; destination stayed `memory_card` before and after probing.

Sony CurrentStr is counted UTF-16: the first word is the payload length including
the NUL terminator, followed by the payload. Decoding excludes that framing word
and produces UTF-8. Native regressions cover real measured strings, empty values,
termination, Unicode/surrogate pairs and malformed framing. The vendor exposes
no independent CurrentStr allocation size; its declared span remains trusted.

## Scope and remaining limits

This gate does not establish A7 IV acceptance for live view, movie recording,
previews, original-download/cancellation APIs, photographic-property writes,
focus or zoom driving, other autofocus modes, forced disconnect recovery or
all focus-failure paths. FX3A measurements remain scoped to that reference body.

Native callback timestamps and sequences describe bridge enqueue ordering,
not camera event-generation time. Vendor-delayed callbacks still limit absolute
freshness. SDK command acceptance alone does not prove physical shutter actuation.
The retained client trace did not serialize the native S2 invocation watermark;
native ordering enforcement also has software regression coverage.

Mechanical-shutter / Long Exposure NR On readiness remains a separate unresolved
issue. No delay, automatic retry, noise-reduction or exposure workaround was
introduced. Deliberate long-exposure support is not inferred from this gate.

The five originals remained pending deferred ingest at acceptance; this gate
does not claim that a download or cancellation API was exercised.
