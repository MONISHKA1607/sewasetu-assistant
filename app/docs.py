# -*- coding: utf-8 -*-
"""Age-proof document validation. A file is accepted only if it is really a readable PDF, JPEG or PNG.

Checking the first bytes ("%PDF") only proves a file CLAIMS to be a PDF. This parses it. What it still cannot
prove: that the document is genuine, belongs to the applicant, or states the right date of birth. That needs
human review (the officer) or OCR/verification, and the officer view says so.
"""
import io

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 50_000_000          # refuse decompression bombs
_FORMAT = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG"}
_MAGIC = {"pdf": (b"%PDF-",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",), "png": (b"\x89PNG\r\n\x1a\n",)}


def validate_document(filename, content):
    """Returns (ok, ext, message). `message` is safe and plain enough to show an elderly applicant."""
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if ext not in _MAGIC:
        return False, ext, "Please upload a JPG, PNG or PDF file."
    if not content:
        return False, ext, "The file is empty. Please choose the photo or scan again."
    if len(content) > MAX_BYTES:
        return False, ext, "File is larger than 5 MB. Please upload a smaller file."
    if not any(content.startswith(m) for m in _MAGIC[ext]):
        return False, ext, "This file does not look like a real %s." % ext.upper()
    try:
        if ext == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                return False, ext, "This PDF is password-protected. Please upload an unprotected copy."
            if len(reader.pages) < 1:
                return False, ext, "This PDF has no pages."
            reader.pages[0].mediabox          # forces the first page to be parsed
        else:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = MAX_PIXELS
            img = Image.open(io.BytesIO(content))
            if img.format != _FORMAT[ext]:
                return False, ext, "The file is a %s image but is named .%s. Please upload it with the right name." % (img.format, ext)
            if img.width * img.height > MAX_PIXELS:
                return False, ext, "The image is too large. Please upload a smaller photo."
            img.verify()
            Image.open(io.BytesIO(content)).load()    # decode fully: catches truncated images
    except Exception:
        return False, ext, "This file cannot be opened - it may be damaged or incomplete. Please scan or photograph the document again."
    return True, ext, "ok"
