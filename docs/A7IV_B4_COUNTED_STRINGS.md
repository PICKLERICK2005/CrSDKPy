# Counted-string correction and revised RC gate

Publication remains blocked pending genuine AF-S acceptance on the rebuilt
artifacts. Version remains unpublished 0.1.0b4; ABI 1.6 / IPC 1.3 are unchanged.

## MF-only acceptance explained

The operator confirmed the physical FE lens switch was MF throughout the five
old-stage captures, including those named AF-S. All five recorded effective
FocusMode (0x0109) = 1, read-only, and FocusModeSetting (0x0179) = 2.
The existing SPTFS resolver correctly selected S2-only. It never invoked the
SDK AF gate, so those captures cannot demonstrate an AF implementation failure
or an AF pass. Earlier body/lens switch measurements are consistent with this.
Preserve DSC07803 through DSC07807 and their original manifests/traces as valid
MF evidence associated with their original binary hashes. Do not relabel scans.

Read-only checks with the new bridge and body AF-S / lens AF report mode 2
(read-write), setting 0, AF-with-shutter 0x025E = 2 and AF-S priority 0x0210 = 3.
The unmodified resolver selects gated_af. Both Remote and RemoteTransfer passed;
destination remained memory_card and sessions closed. Private evidence is in
the new stage, separate from the old captures.

## Native string contract

Sony RemoteCli CameraDevice.cpp's string accessor reads a uint16 count then
decodes the payload starting at word 1; the count includes its terminating NUL.
Observed fixtures: [5, '6', '.', '0', '1', 0], lens name count 16,
and lens firmware count 3. The former bridge decoded the count as text.

The correction validates STR type, framing, final and embedded NULs, and UTF-16
surrogate pairing, then encodes UTF-8 explicitly on every platform. Empty count
0 and count 1 plus NUL both produce empty strings. Output length remains UTF-8
bytes excluding the output NUL. Invalid input returns INVALID_ARG without
partial output. There is no unconditional first-character strip.

The vendor exposes no allocation-size accessor for CurrentStr. GetValueSize
belongs to the independent advertised-values array and must not bound this
pointer. The bridge therefore trusts the vendor allocation's declared count,
as the reference does; it cannot prove safety if a corrupt vendor pointer
overstates its own allocation. The standalone decoder accepts an explicit span
and rejects truncated fixtures before any payload read. Native tests include
the measured strings, empty strings, Unicode/surrogate pairs, legitimate control
content and malformed counts, termination and surrogates.

This change is separate from the unsigned-property-code IPC correction and
event-provenance commits. No focus/release sequencing, default 35 ms dwell,
camera settings, retry policy or SPTFS source changes are included.

## Exact operator procedure (prepare only)

From the SPTFS workspace, with other camera clients closed, require BOTH body
AF-S and lens AF. Leave electronic shutter and photographic settings unchanged.
Use a stationary high-contrast target; no turntable. First run read-only:

```powershell
$sdk = '.\characterization\private\crsdkpy-focus-queue-review'
$stage = '.\characterization\private\crsdkpy-rc-b4-stringfix'
& "$stage\venv\Scripts\python.exe" "$sdk\tools\a7iv_afs_preflight.py" --stage-root $stage --sptfs-root . --output "$stage\evidence\operator-afs-preflight-01.json"
```

It checks staged binary hashes, permits only an explicit read-only IPC allowlist
(plus authorized session SetSaveInfo initialization), records raw numeric codes,
types/values/access/ranges and the actual existing resolver decision, and closes
in finally. Nonzero exit or afs_preflight_passed=false blocks acceptance.
An existing output is refused. Do not infer AF-S from scan names or menu alone.
Do not move body/lens controls during an individual capture; property reads and
release cannot be atomic against operator changes.

ONLY after separate authorization for one exposure, start:

```powershell
& "$sdk\tools\run_sptfs_rc.ps1" -StageRoot $stage -SptfsRoot . -RequireAfS
```

This optional RC-only in-process guard acts after the existing per-shot auto
resolver: it refuses S2-only/overrides/non-35-ms dispatch and permits at most one
gated invocation per process, even if the attempt raises or fails. It never
forces AF-S or substitutes a new capture sequence, and edits no SPTFS files.
The controller starts without requesting a photograph. After authorization,
enter ONLY these commands for the first AF-S shot:

```text
new SONY-A7IV-B4-STRINGFIX-RC
scan RC-AFS-VERIFIED-01 1 --target cam --mode defer
exit
```

Before another separately authorized process/shot, review manifest and passive
trace together: effective AF-S/gated_af, initial S1 and stale cleanup, S1 assertion,
accepted focus evidence/source, exactly one S2 DOWN/UP with 35 ms hold, authoritative
post-release CaptureEvent with native timestamp/sequence, distinct exact media
identity, and verified S1 cleanup. Native markers establish enqueue ordering,
not the camera-generation time of delayed vendor callbacks. Stop on unknown,
unresolved, disconnect, missing trace stages or unexpected behavior. No retry or
resume of uncertain logical shots. At most three individually reviewed repeats
in fresh processes after first-shot acceptance; no bulk scan.

The guarded launcher explicitly closes its adapter on exit and records session
closure, stopped SDK and host return code in a unique closure-*.json. Inspect
that evidence; historical controller exit alone was not proof of an acknowledged
close. A further MF check on the rebuilt bridge
also needs separate authorization; old MF results are retained but do not certify
the new binary hash. Mechanical shutter with Long Exposure NR On remains outside
this gate and unexplained. The confirmed queue defect remains independent.

## Release notes and publication checklist

0.1.0b4 fixes nonzero property metadata IPC codes, Sony counted UTF-16 strings,
focus-event batch correctness/unrelated-event preservation, and native event
provenance/operation markers. Capture signatures and 35 ms default are unchanged.
Deploy matching Python plus ABI 1.6 bridge / IPC 1.3 host; rebuild native artifacts.
Never distribute Sony runtime DLLs in Python archives.

After operator-reviewed physical acceptance: repeat all final validation against
recorded hashes, verify package contents, obtain explicit publish authorization,
then push reviewed branch/PR, merge, tag and upload only the audited artifacts.
The existing release checklist remains applicable; do not execute its publish
commands while this hardware gate is pending. Update SPTFS dependency separately
only after release and explicit authorization. No production install has occurred.
