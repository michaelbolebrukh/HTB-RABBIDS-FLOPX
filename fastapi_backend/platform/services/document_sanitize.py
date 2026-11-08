"""Best-effort PDF sanitisation: stripping active-content vectors from an
uploaded PDF before it is stored.

One replaceable seam: `sanitize_pdf(data)` returns sanitised bytes, or the
original bytes unchanged when sanitisation cannot run or fails. This is a
reusable platform capability (any product accepting uploaded PDFs benefits),
never product-specific.

Removed on a best-effort basis:

- JavaScript actions (/JS, /JavaScript), wherever they are attached
  (document open, annotations, bookmarks, form fields).
- Auto-run actions (/OpenAction, /AA) at the document, page, and annotation
  level.
- Annotation / outline primary actions (/A) whose subtype is an active
  vector (/JavaScript, /Launch, /URI, /SubmitForm, /ImportData, /GoToR) —
  click-to-run, not only additional-actions dictionaries.
- Embedded files (/EmbeddedFiles under the /Names tree) and the /JavaScript
  name tree.
- File-attachment annotations (/Subtype /FileAttachment), which can carry
  embedded payloads outside the Names tree.

IMPORTANT: this is a best-effort mitigation, not a guarantee. It targets the
PDF-native active-content mechanisms above; it does not defend against every
possible PDF parser exploit or general malware, and it never rejects an
upload — see `sanitize_pdf` for the fail-open contract.

Share-plane / partner-download uploads MUST NOT use this module. Use
`platform.services.document_ingest` (fail-closed allowlist rebuild) so as-received
bytes never land on durable private storage.
"""

import io
import logging


logger = logging.getLogger(__name__)

try:
    import pikepdf
    _PIKEPDF_AVAILABLE = True
except ImportError:
    _PIKEPDF_AVAILABLE = False


def sanitize_pdf(data: bytes) -> bytes:
    """Stripping active content from an uploaded PDF, or returning the input
    unchanged when that cannot be done safely.

    FAILS OPEN, deliberately: losing a member's CV to a parser quirk is worse
    than storing an unsanitised one, so any failure (missing dependency,
    malformed/encrypted PDF, unexpected pikepdf error) logs and returns the
    ORIGINAL bytes rather than raising. Callers must not treat this as a
    validation gate — `_validate_pdf` already ran before this is called.
    """
    if not _PIKEPDF_AVAILABLE:
        return data
    try:
        return _strip_active_content(data)
    except Exception:
        # A warning, not an error: an unparseable PDF is a fact about the file
        # a person uploaded, not a fault in this service, and error level is
        # reserved for things somebody should act on:
        logger.warning("PDF sanitisation failed; storing the original file unmodified", exc_info=True)
        return data


def _strip_active_content(data: bytes) -> bytes:
    """Opening the PDF, deleting known active-content entries, and
    re-serialising. Raises on any pikepdf error — the caller (`sanitize_pdf`)
    is the fail-open boundary, not this function."""
    with pikepdf.open(io.BytesIO(data)) as pdf:
        root = pdf.Root

        # Document-open auto-action (runs the moment the PDF is opened):
        if "/OpenAction" in root:
            del root.OpenAction

        # Document-level additional actions (before/after print, close, ...):
        if "/AA" in root:
            del root.AA

        # The /Names tree carries the embedded-file and named-JavaScript
        # subtrees; dropping both removes attachments and named scripts:
        if "/Names" in root:
            names = root.Names
            if "/EmbeddedFiles" in names:
                del names.EmbeddedFiles
            if "/JavaScript" in names:
                del names.JavaScript

        # Per-page and per-annotation actions: both /AA (additional) and
        # primary /A when the action subtype is an active vector. Also drop
        # FileAttachment annotations entirely (embedded payload carriers):
        for page in pdf.pages:
            if "/AA" in page.obj:
                del page.obj.AA
            annotations = page.obj.get("/Annots")
            if annotations is not None:
                kept = pikepdf.Array()
                for annotation in annotations:
                    try:
                        if annotation.get("/Subtype") == pikepdf.Name("/FileAttachment"):
                            continue
                    except Exception:
                        pass
                    if "/AA" in annotation:
                        del annotation.AA
                    _strip_active_action_dict(annotation, "/A")
                    # Embedded-file streams can also hang off non-FileAttachment
                    # annots via /FS or /EF — drop those keys when present:
                    for embed_key in ("/FS", "/EF"):
                        if embed_key in annotation:
                            try:
                                del annotation[embed_key]
                            except Exception:
                                pass
                    kept.append(annotation)
                if len(kept) == 0:
                    del page.obj.Annots
                else:
                    page.obj.Annots = kept

        # A final sweep over every indirect object catches JavaScript actions
        # and active primary /A dictionaries attached anywhere else in the
        # graph (bookmarks/outline items, form fields, orphans):
        for obj in pdf.objects:
            try:
                if not isinstance(obj, pikepdf.Dictionary):
                    continue
                subtype = obj.get("/S")
                if subtype == pikepdf.Name("/JavaScript") and "/JS" in obj:
                    del obj.JS
                if "/A" in obj:
                    _strip_active_action_dict(obj, "/A")
                # Chained next-actions sometimes carry JS outside /A:
                if "/Next" in obj:
                    _strip_active_action_dict(obj, "/Next")
                    next_action = obj.get("/Next")
                    if isinstance(next_action, pikepdf.Dictionary):
                        if next_action.get("/S") == pikepdf.Name("/JavaScript") and "/JS" in next_action:
                            try:
                                del next_action.JS
                            except Exception:
                                pass
            except Exception:
                continue

        buffer = io.BytesIO()
        pdf.save(buffer)
        return buffer.getvalue()


# Action subtypes that turn a PDF into something that runs code, opens a
# local binary, hits a URL, or exfiltrates form data when clicked:
_ACTIVE_ACTION_SUBTYPES = frozenset(
    {
        "/JavaScript",
        "/Launch",
        "/URI",
        "/SubmitForm",
        "/ImportData",
        "/GoToR",
    }
)


def _strip_active_action_dict(owner, key: str) -> None:
    """Deleting owner[key] when it is an action dictionary whose /S is active."""
    try:
        action = owner.get(key)
    except Exception:
        return
    if action is None:
        return
    try:
        subtype = action.get("/S")
        subtype_name = str(subtype) if subtype is not None else ""
    except Exception:
        return
    if subtype_name in _ACTIVE_ACTION_SUBTYPES:
        del owner[key]
