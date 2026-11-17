# Live-view image payload ownership

The caller owns the receive buffer sized by Sony's CrImageInfo.GetBufferSize().
CrImageDataBlock.SetData()/SetSize() configure that allocation. After a successful
GetLiveViewImage(), GetImageData() and GetImageSize() identify the image body;
the payload need not start at the receive allocation base. Sony's reference
retrieval sequence reads that returned pointer while the block and allocation
remain alive. There is no fixed header-offset assumption.

rc2 hardware evidence exposed a receive prefix, an embedded JPEG SOI at offset
1012 and no EOI in the returned slice. The old bridge copied an allocation
prefix using the image-body length, and silently clamped oversized results.
Python labelled those bytes JPEG and fell back to image-info geometry.
This finding does not rewrite prior reports or imply every body has that offset.

rc3 uses a separate receive allocation, checks the returned image pointer and
complete body range against that allocation, and memmoves exactly GetImageSize()
bytes into the caller's output. Null/impossible ranges are rejected; zero image
size means no frame. Output capacity failures report the required image length,
never a truncated success. Frame_NotUpdated maps to no frame; Sony memory
insufficiency maps to buffer-too-small without an automatic fetch retry.
All copying occurs before either allocation or block goes out of scope.

ABI 1.7 / IPC 1.4 layouts and the public LiveViewFrame API are unchanged, but
the corrected bridge and matching rebuilt host must be deployed together from
the rc3 manifest. rc2 binary hashes do not identify corrected implementations.
Hosted frame-count and payload lengths must agree. Python rejects missing
SOI/EOI, unreadable JPEG geometry and inconsistent byte lengths instead of
presenting those bytes as a valid JPEG. This is structure/header inspection,
not full entropy/pixel decoding. Parsed JPEG geometry is distinguished from
image-info fallback geometry; live view is never exact-still exposure evidence.

Standalone tests use a mock image-data block to exercise variable offsets,
zero-offset and overlapping copies, null/foreign/out-of-range pointers, empty
frames, insufficient capacity and successful metadata preservation. Hosted fake
peer tests reject framing prefixes, truncation and length inconsistencies and
verify JPEG geometry supersedes image-info geometry. No vendor runtime or
camera is required for those tests.

Hardware revalidation remains separately authorized: one Remote session, up to
three frames, 2-second frame waits and a 10-second collection bound. Preserve
frame bytes/properties before named predicates, read destination before/after,
then verify session/SDK/host cleanup. No capture, AF, settings changes, downloads
or cancellation is part of that gate. Original-download and cancellation
acceptance remain separate workstreams.
