# A7 IV focus-event queue correction candidate

Revision note: the initial candidate and its regression results below are kept
as review evidence. The expanded upstream revision now retains native enqueue
provenance and SDK-call watermarks; see `AF_EVENT_CORRECTNESS_RELEASE.md` for the
current implementation, remaining vendor-delay limitations and deployment rules.

Prepared on `fix/a7iv-focus-event-queue` from CrSDKPy `9bce8c2` in an isolated
worktree. This candidate is uncommitted and is not installed. The original SDK
checkout and hardware-test baseline are unchanged.

## Implemented scope

- Before asserting S1, drain existing backend notifications with zero-timeout
  polls and exclude prior focus/capture events from the new operation queue.
  Keep their public event-stream evidence. A maximum of 32 drain batches
  prevents a continuous producer from causing an unbounded preflight loop.
  This accommodates the hosted 4096-entry queue / 256-event poll capacity;
  excess backlog fails before S1/S2 instead of permitting stale focus.
- Consume only focus events in the gate. Preserve unrelated operation events
  and the complete public event stream.
- Use the last verdict per independent focus channel within a received batch.
  A later unlocked verdict in that channel invalidates earlier focused evidence;
  an independently lagging channel does not veto a valid focused channel.
- Exclude CaptureEvents already observed during focusing from confirmation of
  the later S2 release. Preserve them publicly for reconciliation.
- Keep current sticky-focus direct reads, S1 control, focus timeouts, S2 hold,
  exposure timeout and photographic properties unchanged. Queue polling before
  S1 is an intentional event-handling change, not an added dwell or sleep.

## Freshness limitation — review before adoption

This fixes already-buffered/backlogged evidence and same-batch contradictions;
it does not establish an atomic native attempt boundary. The hosted decoder
still substitutes delivery time for native callback time. An event generated
between the final empty poll and the S1 write, or delayed by the vendor before
queue insertion, cannot be aged reliably. Separate IPC batches can also expose
a later focus transition only after the gate has returned.

Before claiming a complete freshness guarantee, preserve native callback
timestamps and sequence/provenance and define a boundary in that same clock
domain at the actual S1 operation. Do not compare native and Python clock
origins directly. Such a native/IPC revision needs separate ABI/backward-
compatibility tests and hardware review; it is not silently included here.
No candidate can rule out an unobservable future transition without adding
another measurement. Do not describe this patch as the NR-readiness fix.

## Validation

- Nine new queue regressions, plus the existing AF/capture tests: 38 passed.
- Full offline SDK unit suite: 536 passed, 6 skipped (native bridge not built in
  this worktree). No general hardware-validation suite was run.
- Changed-file Ruff, compileall and git diff --check passed.
- Six regression cases fail against the original installed baseline; two pass
  and the nonquiescing-producer guard case is deliberately excluded there to
  avoid an unbounded baseline loop.
- Sony adapter safety/tracing/dwell/focus and neutral integration against this
  worktree via PYTHONPATH: 118 passed, simulator only.

From this worktree, set `PYTHONPATH` explicitly so editable installation does
not select the original checkout:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python -m pytest tests -q
python -m ruff check src/crsdkpy/session.py tests/test_focus_event_queue.py
python -m compileall -q src tests
git diff --check
```

No SPTFS exposure state, NR/shutter/exposure choice, SDK metadata protocol defect
or production timing workaround is changed. No commit, installation, native
rebuild or physical capture is authorized by these offline tests.
