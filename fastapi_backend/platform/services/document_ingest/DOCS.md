# document_ingest — safe PDF ingest (platform)

## Purpose

Turn a potentially unsafe uploaded PDF into a **new** PDF that is safe to store
on the private object-storage plane and later download, such that **as-received
bytes never become a durable private object**.

## Pipeline

1. Validate magic (`%PDF-`) and size (`MAX_DOCUMENT_BYTES`).
2. Hold the original only in process memory / ephemeral `/tmp` (unlinked after use).
3. Run the **degrade ladder** in `disarm.py`, first rung that succeeds wins — see "The disarm
   ladder" below. Product requirement: a real member's CV is never refused for a reason short
   of "this is not a readable PDF at all"; nobody is going to re-export their CV to satisfy a
   parser, so every rung short of the last one degrades rather than fails the upload.
4. `require_durable_storage` → `save_private` of **safe bytes only** at
   `{key_prefix}/safe/{uuid}.pdf`.
5. Return `SafePrivateObject` (key, sizes, sha256 of safe + sha256 of original for audit).

Refusal (`DocumentDisarmFailedException`, 422) is reserved for the bottom of the ladder: empty
body, over `MAX_DOCUMENT_BYTES`, missing the `%PDF-` magic, or PDFium cannot open the document
at all. There is **no** fail-open path that stores the original.

## The disarm ladder

`disarm_pdf_to_safe_bytes` tries, in order, and returns the first rung that succeeds:

1. **Allowlist rebuild** (`rebuild.py`) — see its own section below. Smallest output, text layer
   intact, the mechanical safety argument. This is the only rung most CVs ever need.
2. **Text-layered raster** — every page rasterised via PDFium, then the ORIGINAL's own text
   re-embedded over the raster as an invisible, selectable layer (`textlayer.py`). PDFium already
   has the source's per-character positions, so this recovers copy-paste/ATS-parseability/screen-
   reader access without needing OCR or trusting any of the source's own structure beyond text
   extraction.
3. **Plain raster** — the same rasterisation with no text layer. Every character of text is lost;
   see "What rasterising costs" below.
4. **Refuse** — only when rung 3 itself fails, which in practice means PDFium could not open the
   document at all (rungs 1 and 2 failing first is not by itself informative — a document rung 1
   cannot allowlist-rebuild, such as one using LZW, is often perfectly openable by PDFium).

Every rung that is skipped or fails logs a WARNING naming the rung and the reason, so a member
silently losing their text layer or vector fidelity is visible in production even though it is
invisible to the member. `flatten=True` on `disarm_pdf_to_safe_bytes` skips straight to rung 3,
for a caller whose threat model wants rasterisation unconditionally; nothing in the product
upload path passes it today.

### Rungs 2 and 3 never refuse on their own budgets

The rasterising rungs share one render step (`_render_pages` in `disarm.py`) and used to raise on
page count, wall clock, total pixels, and a page with no usable declared size. All four are now
either a genuine hard ceiling that degrades instead of refusing, or removed as a refusal point
entirely:

| Bound | Behaviour | Why |
| --- | --- | --- |
| Per-page pixel ceiling (`_RENDER_MAX_PX`) | Hard, cannot degrade | The allocation it bounds is atomic — there is no smaller version to fall back to mid-render, only a smaller scale chosen *before* rendering. |
| Total document pixels (`_MAX_TOTAL_PIXELS`) | Hard ceiling, degrades | Rendering simply stops once the next page would cross the memory budget; everything rendered so far becomes the document. A single page is never held back by this budget on its own — only once at least one page has already rendered. |
| Wall clock (`_MAX_WALL_SECONDS`) | Hard ceiling, degrades | Same shape as the pixel budget: stop rendering further pages, keep what rendered. |
| Page count (`_MAX_PAGES`) | Raised from 40 to 500, sanity-only | No longer a real limit on legitimate documents — a 60-page CV renders all 60 pages. It exists only to bound the cost of the per-page bookkeeping loop itself against a maliciously enormous declared page count, independent of whether any page is ever actually rendered. |
| A page with no usable declared size | Removed as a refusal; degrades to a blank stand-in page | Keeps page count and order intact — the alternative (dropping the page) would silently reorder or truncate a document a human reads start to finish, for content that was already unrenderable. |

Rung 2/3 output must also fit under `MAX_DOCUMENT_BYTES`, since ingest will reject anything
larger downstream. Rather than refuse an oversized rasterised CV, `_raster_to_size_budget` steps
down a DPI ladder (150 → 110 → 85) and re-renders — a lower DPI shrinks both the per-page pixel
cost (letting more pages fit the total-pixel budget above) and the final encoded size — and, if
even the bottom of that ladder does not fit, drops trailing pages one at a time until it does. The
one case this does not talk its way out of is a single page, at the lowest DPI, still exceeding
the size cap on its own — there is no more DPI to drop and no more pages to trim, so that one
outcome is left to fail through to the next rung (or, at rung 3, to the ladder's final refusal).

## Public API

```python
from platform.services.document_ingest import (
    disarm_pdf_to_safe_bytes,
    ingest_private_safe_pdf,
    SafePrivateObject,
)
```

- `disarm_pdf_to_safe_bytes(data, *, rebuild=None) -> bytes`
- `ingest_private_safe_pdf(data, *, key_prefix, storage=None, rebuild=None) -> SafePrivateObject`

Inject `rebuild=` in tests to fake the seam without PDFium.

## Product rules

- Talent CVs call `ingest_private_safe_pdf` from `store_cv`.
- Do **not** call `save_private` with as-received upload bytes.
- Do **not** use `document_sanitize.sanitize_pdf` as a share-plane gate (it fails open).

## The allowlist rebuild (`rebuild.py`, the default path)

The safety argument is not "the dangerous parts were removed" — it is that they were **never
read**. The rebuilder opens page geometry, content-stream bytes and the resource sub-tree, and
copies only keys it names. The catalogue, `/Annots`, `/AA`, AcroForm/XFA, names trees, outlines
and metadata are not visited at all, which is why the property the tests assert is byte equality:

    rebuild(clean) == rebuild(clean + any amount of attack)

That claim is only as good as the walk is thorough, and the first version did not earn it. It
filtered pages and Form XObjects but handed every other resource category to `copy_foreign`,
which copies an object **and its whole referenced subgraph verbatim**. An adversarial pass proved
the consequence: an `/OpenAction` `/JavaScript` dictionary parked inside an `/ExtGState` reached
the output intact. Every category now has its own key list and its own copier, and `copy_foreign`
is called nowhere in that file. `test_document_rebuild.py` carries both halves of the corpus —
attacks in regions never opened, and attacks inside resources that genuinely are.

Two consequences worth knowing before wiring a new consumer:

- **Streams are re-encoded by us.** Content, form, pattern, font-programme and CMap streams are
  decoded here and written back under our own filter, so a viewer never runs an attacker-chosen
  decoder chain. Only **image samples and shading meshes** travel raw, because decoding a JPEG is
  the viewer's job and re-encoding it would destroy the picture — their original `/Filter` travels
  with them, and that is deliberate.
- **LZW is refused, not decoded.** It has an unbounded expansion ratio and no capped decoder here.
  Flate (capped incrementally), the ASCII filters and RunLength are decoded; anything else fails
  the upload closed. In practice nothing written this century emits LZW.

### Residual surface

Keeping the text layer means keeping the viewer's **font and image parsers** in the picture. An
embedded typeface and an image's compressed samples are opaque binaries this module carries
across on purpose, so a 0day in a viewer's font or JPEG decoder is still reachable through a
rebuilt CV. That is the trade for a document that stays searchable and ATS-parseable; the
rasterising path below is what removes it, at the costs listed there.

### Budgets

None of the caps below existed on this path at first, on the theory that not rendering made it
cheap. It does not: `read_bytes` inflates a whole stream before anyone can measure it, so a 120KB
upload expanding to 60MB was measured at 511MB RSS, and the shape scales linearly into multiple
GB inside the 10MB upload cap. The concurrency limiter at the call site bounds how **many**
rebuilds run, never what one costs, so the bound lives in `rebuild.py`:

| Bound | Value | Why |
| --- | --- | --- |
| Pages | 500 | Sanity ceiling on the per-page loop only, matching the rasterising path — a long academic CV declined here used to be handed to the raster rung and silently truncated |
| Wall clock | 20s | Checked per object, not per page |
| Decoded bytes | 32MB | Measured ~3x peak RSS over baseline; two concurrent rebuilds stay under 200MB |
| Raw stream bytes | 24MB | Image samples, which are never decoded |
| Objects | 50,000 | A file that is nothing but millions of tiny resources |

Measured after: a 2GB decompression bomb is refused in 0.1s at 73MB over baseline.

## What rasterising costs (rungs 2 and 3, or `flatten=True`)

**Rung 3 (plain raster)** loses **every character of extractable text** — measured, not estimated:
a source CV yielding 1282 extracted characters yields 0 from its safe copy. A downstream reader
cannot copy-paste it, search it, follow a link in it, or feed it to an applicant-tracking system,
and a screen reader gets nothing. **Rung 2 (text-layered raster)** exists specifically to avoid
this: PDFium's own text extraction over the ORIGINAL supplies the characters and their positions,
which are painted back over the raster as an invisible layer, so the same page keeps its
copy-pasteable, searchable, ATS-parseable and screen-reader-readable text despite being a bitmap
underneath.

Rung 3's pages are encoded as **JPEG** (`_JPEG_QUALITY` in `disarm.py`) rather than Pillow's
PDF-save default (Flate, i.e. lossless compression of the raw pixel grid): measured on a
synthetic 150 DPI A4 page carrying dense 9pt body text, Flate came to 719KB/page against
JPEG-quality-60's 585KB/page (~19% smaller); with a simulated headshot photo added, Flate rose to
634KB/page while JPEG-quality-55 fell to 488KB/page (~23% smaller). This is a **storage**
decision, not a security one — the 10GB free tier needs to hold hundreds of CVs. Rung 2's base
raster PDF is built inside `textlayer.py` (owned separately from this file) and today still uses
Pillow's Flate default rather than JPEG; whoever tunes that module's storage cost should measure
it the same way. Size still grows with page count rather than with the input, so a small
text-only PDF can grow substantially over its original while a photo-heavy one barely changes.

If the assembled output would exceed `MAX_DOCUMENT_BYTES`, `_raster_to_size_budget` steps the
render DPI down and retries rather than refusing — see the disarm-ladder table above for the
full DPI/page-dropping sequence.

None of the lost fidelity is a defect — it is the same property that makes the artefact inert,
since anything that could carry a payload is exactly what rasterising discards. It is recorded
here because it is a **product** consequence that a security-focused reading of this module will
not surface, and any consumer whose recipients expect a parseable document needs to know it
before shipping.

### The invisible text layer (`textlayer.py`)

`raster_with_text_layer(page_images, source_data, *, dpi)` is what makes rung 2 a real rung
rather than a paper distinction from rung 3: it takes the page images the caller already
rendered and paints the ORIGINAL document's own text back over them, invisibly (PDF text render
mode 3), so the bitmap stays copy-pasteable, searchable and ATS-parseable. There is no OCR
anywhere in this path and none is planned — pypdfium2 already exposes the source's real text and
per-character glyph boxes (`get_textpage()`, `get_charbox(..., loose=True)`), so nothing needs to
be recognised, only relocated.

Safety keeps the same shape as the rest of this file: the output is built by us from images plus
text strings and coordinates, never by copying an object, stream, font programme or filter chain
out of the source. The one PDF this module reopens and edits (via pikepdf) is the raster PDF
Pillow just built from the caller's own images — our own construction, not a parse of the
untrusted upload — and the invisible glyphs use the base-14 Helvetica font (WinAnsiEncoding)
rather than anything extracted from the source, since nothing about this layer is ever seen, only
selected.

**Text correctness, measured, not assumed.** Re-extracting text from this module's own output and
diffing it against extraction from the original source is exact (`difflib` ratio 1.0) on a
45-line, ~2900-character dense body-text page and on short multi-line, multi-page, accented
fixtures — see `test_document_textlayer.py`. Getting there took two failed shapes worth recording
so nobody repeats them: an absolute text-matrix reset per character positioned every glyph exactly
right, but reproducibly corrupted PDFium's own re-extraction into a spurious space or line break
after nearly every glyph on real-CV-sized (roughly A4) pages, while staying clean at smaller page
sizes; a single `TJ` array with one string per glyph and a kerning correction between each fixed
the line breaks but introduced spurious spaces between literally every character, because PDFium
apparently treats every string boundary inside a `TJ` array as a word boundary regardless of the
correction's size. What works, and is what ships, is **one plain string per detected line**, Tm
placed once at the line's true start, leaning on the standard-14 font's own built-in advance
widths for everything after that — the shape ordinary justified PDF text already has, so it reads
back the way ordinary text does. The cost is some positional drift deeper into a very long line if
the source's real font's metrics diverge a lot from Helvetica's; that only affects where a
selection rectangle sits, never what gets extracted.

**Unicode.** The font is `WinAnsiEncoding` (cp1252), so it holds the Latin-1 accented letters a CV
name commonly needs and the Windows-style curly quotes/dashes — "José Piñeiro" and a curly
apostrophe both round-trip exactly (tested). It holds nothing outside that single byte: Cyrillic,
Greek and CJK characters cannot be represented by this font at all and are dropped from the layer
character-by-character rather than substituted with the wrong glyph, because silently wrong
extracted text is a worse failure than a small, honest gap in coverage. A CV with a CJK name will
have that name missing from the invisible layer while the rest of the page's Latin text still
works.

**Size.** Measured on the same 45-line dense page: roughly 875 bytes added per page once the
appended content stream is Flate-compressed by pikepdf's save (the default), against a
539KB two-page baseline for the images alone. The layer's cost scales with how much text a page
carries, not with page count or image size.

**Known limitations, honestly:**

- **Rotation** (`/Rotate` 0/90/180/270) is handled and tested — line grouping and the text matrix
  both key off the page's own rotation, and re-extraction is exact on single-line fixtures at all
  four values. Dense multi-line rotated CVs are untested; the mechanism is the same, but nobody
  has measured it on real rotated content the way the unrotated path is measured above.
- **Multi-column layouts** are not reconstructed. Lines are grouped by baseline only, so two
  columns at the same vertical position interleave into one "line" in reading order. This mirrors
  a real limitation of PDFium's own text extraction (it does no layout analysis either), not a
  regression this module introduces.
- **Vertical writing** (e.g. traditional CJK layout) is not attempted; combined with the CJK
  encoding gap above, this path is Latin-script-only by design, which matches its purpose (an ATS
  fallback for CVs) rather than being a general-purpose invisible-OCR-layer tool.

## The one refusal that is not "this is not a PDF"

The ladder's promise is that a member is never refused, and it holds with a single deliberate
exception worth stating plainly, because it looks like a contradiction until you measure it.

Rung 1 declines for two very different reasons. **Compatibility** (an LZW filter, an object shape
it will not read) says nothing about how expensive a document is, so falling through to
rasterisation is exactly right. **Cost** (its byte or object budget) says the opposite: PDFium
does not escape that cost by being asked instead, it pays more, because it executes the operators
rather than counting the bytes. Measured here: a 2GB content stream is declined by rung 1 in 0.1s
and then takes **70 seconds** to rasterise, against a gunicorn worker timeout of 30. Falling
through does not save that upload. It kills the worker and takes every other in-flight request on
it down as collateral.

There is a second half to this, and an adversarial pass found it after the first half shipped.
Rung 1 decides *can I rebuild this?* before it ever asks *what does this cost?*, so a content
stream carrying a filter it will not decode — an LZW chain, or a `/DecodeParms /Predictor` — was
declined as a compatibility problem with nothing known about its size. Measured: a 390KB upload
whose one page draws 200 million glyphs cost 11.5s and 988MB; at 973KB, 40s and 2.1GB. So the
ladder now runs a **cost probe before any renderer**, measuring decoded content with the same
capped inflate rung 1 uses, and refusing above 64MB of decoded page content. That ceiling was
picked from measurement, not taste: at 64MB the raster rung costs 1.8s and +145MB, and a real
CV's page content decodes to under 2MB. A `/Predictor` is ignored by the probe, because it is a
per-row transform that does not change size — which is the whole point, since the two questions
are separate.

So a cost failure stops at rung 1 (`rebuild.ResourceExhausted`, hung on the exception's cause
chain so every existing caller keeps the message it had). That is not a member being refused: no
CV has a 2GB content stream, and a file that does is a resource-exhaustion payload wearing a
document's clothes.

## Threat model (honest)

| After successful ingest | As-received OG on durable private plane? |
| --- | --- |
| App ObjectStorage credentials | **No** — never Put |
| Product download / book / presign | **No** — keys point at safe only |
| Converter 0day during rebuild | Residual on the **host process**, not a durable OG object |
| Viewer font/image decoder bugs on the safe PDF | Real residual when rung 1 wins the ladder (see above); none on rungs 2/3 or `flatten=True` |

## Caps

- Rung 1 (allowlist) path: the budget table above rung 1's own section.
- Rungs 2/3 (rasterising) path, including `flatten=True`: up to 500 pages (sanity ceiling, not a
  real limit — see the disarm-ladder table), ~25s wall clock, 2200px longest page side, 95M total
  pixels, all of which degrade to a partial document rather than refuse; DPI steps down
  (150/110/85) and pages drop from the end if needed to fit `MAX_DOCUMENT_BYTES`.
- Encrypted PDFs: rejected (fail closed) on all rungs — none of PDFium, pikepdf's page-content
  reader, or PDFium's own text extraction can read an encrypted document's content without the
  password, so every rung fails identically and the ladder ends in refusal.
