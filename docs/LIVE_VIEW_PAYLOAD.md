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

## rc4 bounded post-EOI normalization

The retained rc3 payload was 75,392 bytes, matching both native byte_length and
IPC count. It started with SOI, contained readable 1024×680 geometry and ended
with EOI followed by exactly 45 FF bytes. Native extraction copies the exact
GetImageSize() range without clamping. The evidence therefore supports a
vendor-returned suffix, but neither headers nor reference code establish its
intent or a universal padding/alignment maximum. No native size correction is
inferred, and rc3 native hashes remain authoritative for rc4.

Live view alone permits at most 64 trailing FF bytes, a small conservative
allowance covering the observed 45. The raw native/IPC lengths must agree before
normalization, and SOI must be at zero. Inspect only the bounded trailing FF run,
requiring EOI immediately before it; do not search for an arbitrary first EOI or
strip a receive prefix. An unpadded JPEG is returned unchanged. Other suffix
bytes, excessive FF runs, missing EOI and unreadable positive geometry fail.
The existing structural/header validation is retained; this is not full pixel
or entropy-stream decoding or proof of camera-original byte integrity.

Public data ends at EOI. byte_length and metadata.byte_length describe normalized
bytes; metadata.raw_payload_length preserves the original length and
metadata.stripped_padding_bytes records the removed suffix. Frame number,
timestamp, time-code, kind and exact-still semantics remain. Simulator live-view
metadata reports zero stripped bytes. Raw IPC bytes remain separate private
evidence. Thumbnail, screennail and postview validators are not relaxed.

## rc5 measured alignment policy

Three retained A7 IV payloads independently match the following relationships:

| Raw bytes | JPEG bytes | FF suffix | Raw modulo 128 |
|---:|---:|---:|---:|
| 75392 | 75347 | 45 | 0 |
| 77312 | 77263 | 49 | 0 |
| 77440 | 77368 | 72 | 0 |

Each suffix equals (-JPEG_length) modulo 128. SOI is at zero, EOI is immediately
before the FF suffix, raw/native/IPC lengths agree and geometry is 1024×680.
Independent Windows GDI+ decoding materialized every pixel of all three private
normalized images successfully, beyond header parsing. No images are shipped.
The SDK headers/reference code do not promise a universal padding/alignment
contract. These observations support a tested-configuration alignment model;
they do not demonstrate a new native size-handling defect.

rc5 replaces the rc4 allowance with a fixed maximum of 127 FF bytes: one
128-byte alignment boundary minus one. It rejects 128 or more rather than
automatically expanding the policy. All existing length/SOI/EOI/geometry and
suffix protections remain; unpadded data remains byte-identical. The EOI remains
anchored before the bounded FF suffix, never found by arbitrary first-marker
search. No native, ABI or IPC change is needed; rc3 hashes are reverified.

metadata.raw_length_mod_128 reports the raw length modulo 128.
metadata.padding_matches_128_alignment is true only when the raw length is
divisible by 128 and the stripped count equals (-public_length) modulo 128.
These are diagnostics, not acceptance predicates: otherwise valid unaligned
JPEGs remain accepted. Existing identity, timestamps, time code, byte-length
and padding metadata retain their semantics. Still-preview/original validation
is unchanged. Corrected live-view and separate cancellation gates remain pending.
