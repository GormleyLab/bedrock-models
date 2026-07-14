"""Uploaded-file handling for the Converse API.

Chainlit attachments live in a session temp dir that can be cleaned up, so
accepted files are copied into data/uploads/ and referenced from the
JSON-serializable conversation history. Refs are materialized back into
Converse image/document content blocks (raw bytes) at call time.
"""

import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

UPLOAD_DIR = Path("data/uploads")

# Converse API constraints
MAX_IMAGE_BYTES = int(3.75 * 1024 * 1024)
MAX_DOC_BYTES = int(4.5 * 1024 * 1024)
MAX_IMAGES_PER_REQUEST = 20
MAX_DOCS_PER_REQUEST = 5

IMAGE_FORMATS = {
    "png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp",
}
DOC_FORMATS = {
    "pdf": "pdf", "csv": "csv", "doc": "doc", "docx": "docx",
    "xls": "xls", "xlsx": "xlsx", "html": "html", "htm": "html",
    "txt": "txt", "md": "md",
}

_MIME_TO_EXT = {
    "image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif",
    "image/webp": "webp", "application/pdf": "pdf", "text/csv": "csv",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/html": "html", "text/plain": "txt", "text/markdown": "md",
}


def _detect_format(name: str, mime: Optional[str]) -> Optional[str]:
    ext = Path(name or "").suffix.lstrip(".").lower()
    if ext in IMAGE_FORMATS:
        return IMAGE_FORMATS[ext]
    if ext in DOC_FORMATS:
        return DOC_FORMATS[ext]
    if mime and mime in _MIME_TO_EXT:
        return _MIME_TO_EXT[mime]
    return None


def _sanitize_doc_name(name: str, taken: set) -> str:
    # Converse allows only alphanumerics, single spaces, hyphens,
    # parentheses and square brackets in document names
    stem = Path(name).stem
    clean = re.sub(r"[^A-Za-z0-9\s\-\(\)\[\]]", " ", stem)
    clean = re.sub(r"\s+", " ", clean).strip() or "document"
    candidate, n = clean, 1
    while candidate in taken:
        n += 1
        candidate = f"{clean} ({n})"
    taken.add(candidate)
    return candidate


def process_elements(elements: List[Any]) -> Tuple[List[Dict], List[str]]:
    """Convert Chainlit message elements into history refs.

    Returns (refs, rejected) where each ref is a JSON-serializable dict
    ({"image_ref": ...} or {"doc_ref": ...}) and rejected is a list of
    human-readable reasons for skipped files.
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    refs: List[Dict] = []
    rejected: List[str] = []
    doc_names: set = set()
    n_images = n_docs = 0

    for el in elements:
        name = getattr(el, "name", None) or "file"
        path = getattr(el, "path", None)
        mime = getattr(el, "mime", None)
        if not path or not Path(path).exists():
            rejected.append(f"**{name}**: file content unavailable")
            continue

        fmt = _detect_format(name, mime)
        if fmt is None:
            rejected.append(
                f"**{name}**: unsupported type. Supported: "
                f"{', '.join(sorted(set(IMAGE_FORMATS) | set(DOC_FORMATS)))}"
            )
            continue

        size = Path(path).stat().st_size
        is_image = fmt in IMAGE_FORMATS.values()
        limit = MAX_IMAGE_BYTES if is_image else MAX_DOC_BYTES
        if size > limit:
            rejected.append(
                f"**{name}**: {size / 1024 / 1024:.1f} MB exceeds the "
                f"{limit / 1024 / 1024:.2f} MB Bedrock limit"
            )
            continue

        if is_image and n_images >= MAX_IMAGES_PER_REQUEST:
            rejected.append(f"**{name}**: over the {MAX_IMAGES_PER_REQUEST}-image limit per message")
            continue
        if not is_image and n_docs >= MAX_DOCS_PER_REQUEST:
            rejected.append(f"**{name}**: over the {MAX_DOCS_PER_REQUEST}-document limit per message")
            continue

        stored = UPLOAD_DIR / f"{uuid.uuid4().hex}.{fmt}"
        shutil.copyfile(path, stored)

        if is_image:
            n_images += 1
            refs.append({"image_ref": {"path": str(stored), "format": fmt}})
        else:
            n_docs += 1
            refs.append({
                "doc_ref": {
                    "path": str(stored),
                    "format": fmt,
                    "name": _sanitize_doc_name(name, doc_names),
                }
            })

    return refs, rejected


def materialize_messages(
    history: List[Dict],
    include_images: bool = True,
    include_docs: bool = True,
) -> List[Dict[str, Any]]:
    """Turn ref-based history into Converse API messages (with raw bytes).

    Attachment types the current model can't accept are replaced with a
    short text placeholder so the model knows something was omitted.
    """
    messages: List[Dict[str, Any]] = []
    for turn in history:
        blocks: List[Dict[str, Any]] = []
        for block in turn["content"]:
            if "text" in block:
                if block["text"]:
                    blocks.append({"text": block["text"]})
            elif "image_ref" in block:
                ref = block["image_ref"]
                if include_images and Path(ref["path"]).exists():
                    blocks.append({
                        "image": {
                            "format": ref["format"],
                            "source": {"bytes": Path(ref["path"]).read_bytes()},
                        }
                    })
                else:
                    blocks.append({"text": "[an image attachment was omitted]"})
            elif "doc_ref" in block:
                ref = block["doc_ref"]
                if include_docs and Path(ref["path"]).exists():
                    blocks.append({
                        "document": {
                            "format": ref["format"],
                            "name": ref["name"],
                            "source": {"bytes": Path(ref["path"]).read_bytes()},
                        }
                    })
                else:
                    blocks.append({"text": f"[document '{ref['name']}' was omitted]"})
        if not blocks:
            blocks = [{"text": "(empty message)"}]
        # A document block requires an accompanying text block
        if any("document" in b for b in blocks) and not any("text" in b for b in blocks):
            blocks.append({"text": "Please review the attached document(s)."})
        messages.append({"role": turn["role"], "content": blocks})
    return messages
