"""Binary attachment validation: the bytes must be what the extension claims.

The attachment tools exist so a scheduled routine can drop hero images into
the vault. The vault is synced to every device and, for published notes, its
attachments are served from the public site. So an upload is checked twice
over before it lands:

* raster formats must start with their format's magic bytes, so a `.png` that
  is really HTML (or an executable) is refused;
* SVG is XML that a browser will execute when the file is opened directly, so
  it must parse, have an `<svg>` root, and contain nothing active: no
  `<script>`, no `<foreignObject>` (which can carry arbitrary HTML), no `on*`
  event attributes, no `javascript:` and no reference that leaves the file --
  only `#fragment` hrefs and `url(#id)` paint servers.

DOCTYPE and entity declarations are refused before parsing. Hero SVGs never
need them, and refusing them outright is simpler to audit than trusting the
XML parser's expansion limits.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import xml.etree.ElementTree as ET

from .errors import ToolError

CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_GIFS = (b"GIF87a", b"GIF89a")

# Any url(...) whose target is not a same-document fragment.
_EXTERNAL_URL = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.IGNORECASE)
_IMPORT = re.compile(r"@import", re.IGNORECASE)


class InvalidAttachment(ToolError):
    """The bytes do not match the extension, or the SVG is not inert."""


def content_type(suffix: str) -> str:
    return CONTENT_TYPES.get(suffix.lower(), "application/octet-stream")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_base64(value: str) -> bytes:
    """Strict base64, tolerating whitespace and a `data:...;base64,` prefix."""
    text = value.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = re.sub(r"\s+", "", text)
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidAttachment(
            "content_base64 is not valid base64. Encode the file's raw bytes "
            "with standard base64 (A-Z a-z 0-9 + /, '=' padding)."
        ) from None


def validate(data: bytes, suffix: str) -> None:
    """Raise InvalidAttachment unless ``data`` is a safe file of type ``suffix``."""
    suffix = suffix.lower()
    if not data:
        raise InvalidAttachment("The decoded file is empty.")
    if suffix == ".png":
        ok = data.startswith(_PNG)
    elif suffix in {".jpg", ".jpeg"}:
        ok = data.startswith(_JPEG)
    elif suffix == ".gif":
        ok = data.startswith(_GIFS)
    elif suffix == ".webp":
        ok = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    elif suffix == ".svg":
        validate_svg(data)
        return
    else:
        raise InvalidAttachment(f"{suffix!r} is not an allowed attachment type.")
    if not ok:
        raise InvalidAttachment(
            f"The bytes are not a {suffix} file (its signature does not match). "
            "Check that the content was encoded from the right file, and that "
            "the path's extension matches the real format."
        )


def validate_svg(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidAttachment("SVG must be UTF-8 encoded text.") from None

    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise InvalidAttachment(
            "SVG must not contain a DOCTYPE or entity declarations."
        )
    if "<?xml-stylesheet" in lowered:
        raise InvalidAttachment("SVG must not reference an external stylesheet.")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise InvalidAttachment(f"SVG is not well-formed XML: {exc}.") from None

    if _local(root.tag) != "svg":
        raise InvalidAttachment(
            f"SVG root element is <{_local(root.tag)}>, not <svg>."
        )

    for element in root.iter():
        name = _local(element.tag).lower()
        if name in {"script", "foreignobject", "iframe", "embed", "object"}:
            raise InvalidAttachment(f"SVG must not contain <{_local(element.tag)}>.")
        if name == "style" and element.text and _is_external_css(element.text):
            raise InvalidAttachment(
                "SVG <style> must not load anything external (@import or url() "
                "other than url(#id))."
            )
        for attr, value in element.attrib.items():
            attr_name = _local(attr).lower()
            if attr_name.startswith("on"):
                raise InvalidAttachment(
                    f"SVG must not contain event handler attributes ({_local(attr)}=)."
                )
            if "javascript:" in value.lower().replace(" ", ""):
                raise InvalidAttachment("SVG must not contain javascript: URLs.")
            if attr_name == "href" and not value.strip().startswith("#"):
                raise InvalidAttachment(
                    f"SVG href {value[:80]!r} points outside the file; only "
                    "'#fragment' references are allowed."
                )
            if _is_external_css(value):
                raise InvalidAttachment(
                    f"SVG attribute {_local(attr)}= references an external URL; "
                    "only url(#id) is allowed."
                )


def _is_external_css(text: str) -> bool:
    return bool(_EXTERNAL_URL.search(text) or _IMPORT.search(text))


def _local(tag: str) -> str:
    """`{http://www.w3.org/2000/svg}svg` -> `svg`."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def sniff_suffix(data: bytes) -> str | None:
    """The attachment type ``data`` actually is, if recognisable."""
    if data.startswith(_PNG):
        return ".png"
    if data.startswith(_JPEG):
        return ".jpg"
    if data.startswith(_GIFS):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower()):
        return ".svg"
    return None
