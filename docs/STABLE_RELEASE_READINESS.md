# 0.1.0 stable readiness

Target: 0.1.0 final. This is a development review, not release authorization.
The corrected unpublished candidate is 0.1.0rc4, not 0.1.0 final.
The starting reference is main at 3cd1ec002b78f4fe944bf4594bf20876ce1678bf.
Published 0.1.0b4, its artifacts and accepted ABI 1.6 / IPC 1.3 pair remain
unchanged. No new camera validation is claimed.

## Corrected offline

- Failed Session construction previously left the successfully opened backend
  session without an owner. Camera.open now closes it before propagating errors.
- Rediscovery previously replaced the SDK camera registry with only visible
  devices. Missing cameras could lose cleanup ownership and stable object identity.
  Discovery still returns only visible devices; SDK retains known objects until close.
- One close error previously prevented remaining sessions and backend shutdown.
  Cleanup now attempts the remaining operations and propagates failure. A backend
  shutdown failure leaves SDK.started true; a successful shutdown does not hide
  a preceding close error. Session.closed denotes local facade closure, not
  independent proof that a failed backend disconnect physically succeeded.
- Transfer waits now check session usability instead of spinning on closed
  sessions, and a rejected cancel does not latch cancellation as accepted.
  No automatic transfer or exposure retries were added.
- Content.preview now rejects a returned conflicting content identifier, matching
  Capture.preview. Missing identifiers retain the existing compatibility policy;
  they are not newly certified as independent identity evidence.
- Native contract tests can build without proprietary headers/runtime using
  CRSDKPY_CONTRACT_TESTS_ONLY=ON. Windows/Linux CI jobs are configured, not yet run
  remotely for these changes. Fixed wire layouts remain unchanged; development
  bridge/host behavior now requires ABI 1.7 / IPC 1.4, described below.

## Established correctness defects: corrected in development

Originals now use a unique private staging directory and an operation-unique
filename. An owned terminal result plus validated completed path, regular-file
type and positive/matching known size are required before publication. No final
path fallback or partial final file is accepted. No-overwrite publication is
exclusive, including competing creators; overwrite uses atomic replacement only
after validation. Uncertain starts, timeout, cancellation, connection loss and
failed publication retain quarantined staging instead of unlinking a potential
writer. See ORIGINAL_DOWNLOAD_SAFETY.md for the precise contract and limits.

ABI 1.6 / IPC 1.3 could not associate a session-global path with one event.
The additive development protocol is ABI 1.7 / IPC 1.4. New original downloads
carry an internal request ID; native callbacks must first match the unique
filename (including a per-request nonce). Only then can they tag an event and
alter terminal/path state. Unattributed notifications remain unowned evidence.
No callback timestamp is treated as physical camera-generation time. Existing
fixed structs and legacy operations remain; old b4 artifacts are not replaced.

Post-release fake-backend regressions precede the cleanup correction. Normal S2
sequencing is unchanged. Failed commands/polls retain the primary error, capture
object and conservative exposure flags; gated captures attempt existing S1
cleanup once. Cleanup failure is reported separately; no additional S2, retry,
dwell or readiness workaround is introduced. Late AutofocusFailedError is wrapped
with its cause after a possible release, so it cannot promise no exposure.

## Remaining release blockers

1. Review the candidate PR's actual Python 3.9/Linux/macOS/Windows CI results;
   configured jobs and local Windows tests alone do not establish that execution.
2. Freeze a reviewed final source/artifact pair and rebuilt host/bridge. No
   accepted environment is upgraded merely by preparing development binaries.
3. rc1 A7 IV exact previews, one short-path original transfer, publication and
   shutdown have passed. Local hashing did not establish independent source-side
   cryptographic equality. rc2 only adds a Windows UTF-16 staging-path refusal;
   the successful transfer sequence is unchanged. Separately authorize bounded
   live-view with rc3's corrected GetImageData() extraction and rc4's bounded
   post-EOI FF normalization (rc3 rejected an observed FF suffix), plus separate
   existing-original cancellation/quarantine checks. Existing b4
   AF-S/MF acceptance is retained, not relabelled as this build's capture gate.

## Important nonblocking findings and explicit limits

- A7 IV remaining-shot zero needs raw status/property/type readbacks. Native
  storage reads slots 1/2 using 0x0709/0x070F, retaining reported zero. Zero is not
  silently changed to None or evidence that the camera cannot take a picture.
  Compare raw readings with camera state before asserting a decoding defect.
- RAW/JPEG association must retain content_id, file_id and slot. Freshness uses
  greater-than-baseline identifiers, not adjacent numbers. Multi-slot switching,
  counter resets and new files from other camera clients need bounded acceptance;
  do not infer universal multi-body or multi-slot support.
- Session mode is explicit: close a capture session before opening a different
  control mode, keep camera/device identity, re-enumerate exact content and inspect
  capabilities again. No automatic mode/destination/property changes are added.
- Download starts are serialized and simultaneous waiters for the same download
  are rejected; explicit cancellation remains available. Serialize other session
  operations. Competing camera clients do not acquire isolated physical queues.
- Mechanical shutter with Long Exposure NR, arbitrary long exposures and vendor-
  delayed callback freshness remain separate limits. Keep the 35 ms default.
- FX3A measurements remain distinct from A7 IV evidence. The shipped conformance
  profile is ILME-FX3A; the A7 IV capture evidence does not fabricate a full profile.
- Local validation covers Windows and static Python 3.9 compatibility. Candidate
  PR checks record actual Python 3.9/Linux/macOS execution separately from hardware.

## Bounded hardware gates, separately authorized

After the download/software blockers are closed:

1. Operator confirms intended A7 IV, no competing client and unchanged photographic
   settings. Freeze package/host/bridge hashes. Authorize initialization, discovery,
   a bounded session and unavoidable SetSaveInfo host-save routing explicitly.
2. One RemoteTransfer session: read capabilities, destination before/after, battery,
   storage and raw remaining-shot properties. Enumerate existing media with exact
   slot/content/file identities; do not request an exposure or alter media.
3. Fetch a thumbnail and screennail of one selected existing image. Record requested
   and returned identity, format and dimensions. Unsupported forms are observations,
   not permission to change camera settings or substitute a live-view frame.
4. Download that exact existing ARW into a fresh host-only directory. Confirm
   terminal event, reported path, size, parser-readable original and SHA256. A hash
   alone proves no source equality: compare to an independently trusted source
   hash when available. Exercise cancellation only after its software cleanup
   contract is reviewed, using another existing-file transfer, never a new shot.
5. Close RemoteTransfer, verify cleanup, then open Remote only if authorized and
   needed for live view. Read capabilities and collect a small bounded number of
   frames; no still capture, AF or photographic-property writes. Close and confirm
   SDK shutdown and no orphaned host. Stop on disconnect, timeout or cleanup error.
6. Only if still necessary, separately authorize one capture-to-transfer lifecycle
   on the final candidate, with exact identity and no retry after unknown exposure.
   Effective AF-S requires both body AF-S and lens AF. Existing b4 capture evidence
   remains recorded; it is not relabelled as validation of a modified build.

No test is authorized merely by appearing in this plan. Do not run a general
hardware-validation suite that changes properties or fires a shutter.

## Stable release gate

NO-GO until the blockers above are closed. Then set 0.1.0 consistently, update
classifiers/changelog/model limits, run Python/native/cross-platform/packaging
checks, audit wheel/sdist exclusions and freeze reviewed artifact hashes.
The existing publication workflow downloads checksummed approved assets rather
than rebuilding at publish time. Tag, release and PyPI publication each require
separate operator authorization. Do not overwrite b4 or install development
artifacts into a production application environment.
