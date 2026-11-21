"""Upload validation: size, extension allow-list, magic bytes, zip-bomb guard, filename sanitising."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath

from app.core.config import Settings
from app.core.exceptions import UnsupportedFormat, ValidationFailed
from app.multimodal.images import IMAGE_EXTENSIONS, looks_like_image

_OOXML_PARTS = {".docx": "word/", ".pptx": "ppt/", ".xlsx": "xl/"}
_MAX_UNCOMPRESSED = 300 * 1024 * 1024
_MAX_RATIO = 200


def sanitize_filename(name: str) -> str:
    base = PurePosixPath(name.replace("\\", "/")).name
    base = re.sub(r"[^\w.\- ]", "_", base).strip(" .")
    if not base:
        raise ValidationFailed("Invalid filename")
    return base[:200]


def validate_upload(filename: str, data: bytes, settings: Settings) -> str:
    """Returns the sanitised filename or raises. Never trusts the client-supplied content type."""
    safe = sanitize_filename(filename)
    ext = PurePosixPath(safe.lower()).suffix
    if ext not in settings.allowed_extensions:
        raise UnsupportedFormat(f"File type '{ext}' is not allowed")
    if not data:
        raise ValidationFailed("Empty file")
    if len(data) > settings.max_upload_bytes:
        raise ValidationFailed(f"File exceeds {settings.max_upload_mb} MB limit")

    if ext == ".pdf":
        if not data.lstrip()[:5] == b"%PDF-":
            raise ValidationFailed("File content is not a PDF")
    elif ext in _OOXML_PARTS:
        if data[:4] != b"PK\x03\x04":
            raise ValidationFailed(f"File content is not a valid {ext} container")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                infos = zf.infolist()
                total = sum(i.file_size for i in infos)
                if total > _MAX_UNCOMPRESSED or (len(data) and total / len(data) > _MAX_RATIO):
                    raise ValidationFailed("Archive rejected (decompression bomb guard)")
                names = zf.namelist()
        except zipfile.BadZipFile as exc:
            raise ValidationFailed("Corrupt archive") from exc
        if "[Content_Types].xml" not in names or not any(n.startswith(_OOXML_PARTS[ext]) for n in names):
            raise ValidationFailed(f"Not a valid {ext} document")
    elif ext in IMAGE_EXTENSIONS:
        if not looks_like_image(data):
            raise ValidationFailed(f"File content is not a valid {ext} image")
    else:  # text-like formats
        if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise ValidationFailed("Binary content in a text file")
    return safe
