# Original download safety — development contract

Unpublished candidate: 0.1.0rc1. This does not authorize 0.1.0 final or deployment.

The public download(content, destination, overwrite=False), cancel() and wait()
interfaces are retained. Destination can be a file or existing directory.
This source requires a matching ABI 1.7 / IPC 1.4 native pair; published b4 and
its accepted ABI 1.6 / IPC 1.3 pair remain unchanged and must not be overwritten.

## Completion and publication

The vendor writes only inside a newly created .crsdkpy-download-* directory
beside the destination. Its filename carries an internal process request ID,
a fresh nonce and the destination extension. Ordinary applications do not
construct tokens, manipulate native handles or issue transfer protocol commands.

The bridge checks the callback filename against that request before changing
active/terminal/path state. Exact full paths or the complete operation-unique
basename are accepted. Missing, different, truncated or ambiguous names do not
confirm the operation. An unowned notification remains visible evidence.
Separators are normalized only on Windows. Matching is otherwise exact,
including case and Unicode code points; no Unicode folding manufactures identity.
The ID is carried in existing transfer-event i3 and reserved content-argument
words; no fixed struct is widened. IPC opcode 29 and the additive C entry point
are minor-version gated. Legacy entry points remain usable by older clients.

The facade consumes only matching-ID transfer events, retaining unrelated
operation events and the public stream. Delivery/native timestamps are retained
for diagnostics, not used as proof of vendor event-generation freshness.

OK is insufficient by itself. The completed path must name this staging file,
which must be a nonempty, unredirected, private regular file with no hard-link
alias. A positive integer camera-reported size must match; missing/zero size is
unknown, not fabricated. Metadata is checked across local validation. Filesystem
type is validated; the SDK does not add a general image-format parser or claim
independent camera-side SHA256 verification.

Without overwrite, Windows rename or POSIX exclusive hard-link publication
refuses a competing destination. Unsupported hard-link filesystems fail closed.
With overwrite, replacement occurs only after validation. Failed transfers and
failed publication leave the existing destination unchanged. Publication commit
and process interruption cannot form a cross-process transaction: after an
interrupt or power loss inspect the final file and retained evidence rather
than assuming a failed caller received no completed publication.

## Uncertainty and quarantine

Timeout does not cancel, remove staging, or authorize another transfer. Explicit
wait may resume the same operation. Cancellation is serialized against new starts,
including cancellation racing with a completed wait. The next start stays blocked
until publication is resolved. A rejected/ambiguous start retains session
ownership because it may already have started a writer. UNKNOWN terminal codes
also retain ownership; do not initiate another original until the session is
closed/reconciled. There are no automatic transfer or exposure retries.

Cancellation requests do not themselves prove completion. Cancellation, failure,
session closure and rejected operations retain staging conservatively. Exception
staging_path/transfer and ContentDownload.quarantine_path identify retained data.
Closing the facade does not delete it: native close can announce STOPPED before
the vendor finishes disconnecting. Do not remove a quarantined directory until
backend/host termination is independently established. Never promote partial or
quarantined files automatically. Failed publication preserves complete staging
for explicit recovery; a new transfer does not silently reuse it.

A clean success removes staging. Cleanup failure after a committed publication
does not retroactively report a failed replacement; quarantine_path identifies
leftovers. Disk-full, permissions and destination conflicts remain explicit errors.

This is point-in-time validation under cooperative local filesystem use, not
authenticated storage or descriptor-relative protection against a malicious
directory-swap attacker. Full power-loss durability is not claimed. Windows
does not fsync a read-only staging handle; terminal completion and filesystem
publication are distinct from a durable archival write receipt.

## Compatibility and acceptance

Custom drivers must implement supports_owned_downloads and
start_owned_content_download with genuine per-callback ownership. Simply tagging
whichever callback arrives with the current ID is incorrect. Drivers that lack
the contract fail before a writer starts rather than using legacy unsafe downloads.

New Python refuses older native peers through normal negotiation. Rebuild and
stage host and bridge together, separately from accepted installations. Local
compilation or fake-host success is not hardware acceptance. Vendor callback
filename form and complete-file timing need bounded A7 IV tests before release.
Do not change photographic settings, add retries or expose another image merely
to make a download test pass.

Capture failures after release carry capture, exposure_requested,
exposure_confirmed, release_command_completed, focus_cleanup_confirmed,
control_cleanup_confirmed and cleanup_error on the exception.
False exposure_confirmed means unconfirmed, never proof that no image exists.
The primary exception stays primary; an unsafe late autofocus-refusal type is
wrapped with its original cause. Context-manager cleanup failures are attached
as lifecycle_cleanup_errors instead of masking that primary exception.
Gated capture uses existing best-effort S1
cleanup. Focus cleanup does not prove shutter-control state; overall cleanup is
unknown unless actually established. release_command_completed records command
acceptance, not physical actuation. MF does not gain S1 commands. No failed S2 DOWN/UP is retried; if S2 state
is uncertain, stop and reconcile/close under operator control.
