# A7 IV 0.1.0b4 release-candidate hardware gate

The initial stage and its five captures are preserved as MF-only evidence:
the operator confirmed the lens switch was MF throughout. The revised gate and
counted-string rebuild are documented in A7IV_B4_COUNTED_STRINGS.md. Use that
document's new stage and AF-S guard; do not reuse old acceptance for new binaries.

This gate uses an installed wheel in a dedicated venv and matching native
ABI 1.6 / IPC 1.3 binaries. The original 0.1.0b3 checkout, environment and native
runtime are not overwritten. SPTFS source, its camera-neutral contract, PyKS2
source and historical projects are not changed. No capture is authorized by
software staging or by this document.

## Reviewed commit boundaries

- `a401dae`: unsigned property metadata IPC contract and independent tests.
- `0660af2`: event correctness, native provenance, ABI/IPC markers and tests.
- Release preparation is a subsequent commit: package version, release notes,
  this gate, software validator and temporary SPTFS launcher.

These are local commits only. No merge, push, production install or publication.

## Staged layout and software-only validation

The operator's local stage is `characterization/private/crsdkpy-rc-b4` relative
to the SPTFS workspace (outside SDK source). It contains:

- `venv/Scripts/python.exe`: isolated Python 3.13, wheel-installed CrSDKPy
  0.1.0b4, with no system-site-packages. Pinned controller dependencies and
  pytest are installed only here. `requirements-lock.txt` records all versions.
- `native/`: rebuilt host/bridge and the operator-supplied Sony runtime,
  including the required `CrAdapter` directory. `stage-manifest.json` records
  versions and file hashes. Vendor files remain private and are not distributed.
- `host-save/`, `data/`, `evidence/`: separate host-save location, new test
  projects/logs, software-gate results and passive capture traces.

`tools/validate_rc_stage.py --stage-root STAGE` must run with the staged Python.
It asserts the installed package is inside that venv, verifies actual host
negotiation and stopped state without discovery/opening a real session, checks
the actual bridge ABI, and exercises nonzero string/value access, session
cleanup and provenance through an explicitly fake peer. A fake firmware string
is not a measurement of the attached A7 IV. Software results cannot certify
physical connection/disconnection or rebuilt-host camera metadata recovery.

Installed-wheel software gate: **560 passed, 1 skipped** in the SDK suite,
**118 passed** in Sony/controller compatibility checks; `pip check`, Ruff,
compileall, staged provenance/metadata/cleanup checks and launcher help-only
verification passed. The real staged host negotiated ABI 1.6 / IPC 1.3 and
stopped cleanly. No physical camera sessions were opened by these checks.

The full SDK suite should also be run with the staged Python and explicit
`CRSDKPY_BRIDGE` pointing at the staged DLL. Clear PYTHONPATH/PYTHONHOME so an
editable checkout cannot shadow the wheel. No general hardware-validation suite
is part of this software gate. The absent-bridge test is skipped when built.

## Temporary SPTFS selection, without activation or source edits

From the SPTFS workspace, invoke the SDK worktree's launcher:

```powershell
& .\characterization\private\crsdkpy-focus-queue-review\tools\run_sptfs_rc.ps1 -StageRoot .\characterization\private\crsdkpy-rc-b4 -SptfsRoot .
```

The launcher checks binary hashes, selects the staged interpreter, sets
CRSDKPY_HOST / CRSDKPY_BRIDGE / CRSDKPY_SAVE_DIR only for its process scope,
clears editable-import overrides, and restores the previous values on exit.
It runs the existing controller with:

```text
--camera sony --sony-autofocus auto --sony-afs-dwell-ms 35
--sony-capture-trace STAGE/evidence/captures --data-root STAGE/data
```

The explicit data root bypasses the first-run wizard and saved-root selection;
it does not relocate or rewrite historical projects. Do not use data-root or
configuration-changing REPL commands during this gate. SPTFS's ordinary saved
configuration is not a production dependency installation or part of the venv.
The launcher does not pipe commands into the controller or request a photograph.
Use `-HelpOnly` to validate imports/arguments without a camera connection.

Session open retains the already authorized SetSaveInfo initialization. It
does not intentionally write still destination or photographic properties.
Before the first capture, verify the operator's card destination and settings;
stop if anything unexpected appears. Physical clean close must be checked on
controller exit after hardware acceptance; software fake-session cleanup alone
does not prove a hardware session closed.

## Operator-controlled physical gate — prepare only

Use electronic shutter throughout. Mechanical shutter with Long Exposure NR On
is excluded: that readiness issue is separate and is not solved by these fixes.
Choose a stationary well-lit high-contrast target; do not use the turntable.
Operator controls all focus/exposure/shutter/NR settings. No automatic setting
rewrite or shutter retry is permitted. The hold stays at the existing 35 ms.

1. Separately authorize **one MF shot**. Set effective camera/lens mode to MF
   and focus manually. Launch the staged controller above, then enter:

   ```text
   new SONY-A7IV-B4-RC
   scan RC-MF-01 1 --target cam --mode defer
   ```

   Confirm one physical exposure, EXPOSED_IDENTIFIED, exact filename/native
   identity, S2-only policy, 35 ms evidence, one DOWN/UP pair and an authoritative
   post-release CaptureEvent. Retain the manifest and trace together. Do not
   continue on unknown/unresolved exposure, disconnect or unexpected behavior.

2. Only after reviewing MF, separately authorize **one AF-S shot**. Operator
   selects body AF-S/lens AF. The existing auto resolver requires AF-with-shutter
   On and the measured Balanced Emphasis configuration; it must block otherwise.
   Do not set those properties automatically. In the same controller session:

   ```text
   scan RC-AFS-01 1 --target cam --mode defer
   ```

   Check resolved mode, accepted focus source/state, S1 assertion/cleanup,
   S2 pair, CaptureEvent, exact media identity, native callback provenance and
   absence of unexplained events. Native enqueue provenance is not proof of
   vendor camera-generation time. A pre-S2 capture event cannot confirm this shot.

3. If both shots are unambiguous, authorize at most **three additional AF-S
   shots individually**, entering just one line per authorization and reviewing
   each result before the next:

   ```text
   scan RC-AFS-R01 1 --target cam --mode defer
   scan RC-AFS-R02 1 --target cam --mode defer
   scan RC-AFS-R03 1 --target cam --mode defer
   ```

   These are not a batch or loop. Stop on the first unknown/unresolved or
   unexpected result. Proven focus failure before S2 does not authorize an
   automatic retry. Never resume or overwrite an uncertain logical shot.

4. Exit with `exit`. Verify disconnect and SDK cleanup; retain evidence. Compare
   originals read-only by exact identity. Ingest, if separately authorized, must
   copy only those identified originals into the new RC project. No historical
   scan/recovery records are edited or reused by this gate.

Physical acceptance is pending. The earlier 49/49 electronic/35 ms original
ingest result belongs to baseline 0.1.0b3, not this wheel.

## Release checklist after physical acceptance

1. Record operator-approved gate results with package, source commit, wheel and
   binary hashes, firmware, lens, settings and trace/manifest references. Resolve
   any unexpected behavior before proceeding; do not reclassify unknowns to pass.
2. Confirm metadata and event changes remain separately reviewable in history;
   review the final diff, native compatibility, licensing and artifact contents.
   Keep vendor files/private diagnostics out of source and distributions.
3. Run the complete Python suite, CTest, Ruff, compileall, diff checks and
   installed-wheel gate against the exact release artifacts. Rebuild if source
   changes; record fresh hashes. Keep ABI 1.6 / IPC 1.3 deployment requirements.
4. Build wheel/sdist and run `python -m twine check dist/*` in a release tooling
   environment. Inspect the archives for private/vendor/native build files.
5. Confirm that the intended PyPI version is unused. Obtain explicit authorization
   before pushing, merging, tagging v0.1.0b4,
   publishing a GitHub release or uploading with `python -m twine upload dist/*`.
   Do not replace a published version's contents. Publish only approved hashes.
6. After publication, verify PyPI metadata/hashes and install the published wheel
   in another disposable environment. Repeat software negotiation and compatibility
   checks with the matching native pair; retain 0.1.0b3 as a rollback option.
7. In a separately authorized SPTFS dependency update, change its tested minimum
   to `crsdkpy>=0.1.0b4,<0.2` (or the project's approved pin/lock), document the
   required native deployment, and run its full tests/lint/compile checks and
   approved Sony smoke gate. Do not widen camera-neutral behavior or affect
   Pentax as part of a dependency-only update.
8. Obtain explicit authorization to install/deploy that dependency and native
   pair in the production controller. Never disable NR or change exposure/shutter
   type automatically as a condition of upgrading. The NR/readiness investigation
   and deliberate-long-exposure acceptance remain separate workstreams.
