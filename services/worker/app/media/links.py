"""Find the files a chat message refers to, and say what kind of link each is.

The production chat API does not send attachments as attachments. A media
message arrives as text: a caption or `[Attachment: name.ext]`, a newline, and
one URL. Four URL families have been seen in production (2026-09-21 probe):

  bitrix_short   https://travelgate.bitrix24.ae/~XXXXX          agent files; public;
                                                                lives as long as Bitrix keeps it
  bitrix_rest    https://travelgate.bitrix24.ae/rest/<u>/<TOKEN>/download/?token=disk|...
                                                                voice notes, both sides; carries a
                                                                REST webhook token in the path
  gupconnector   https://gupconnector.cultivbureau.com/connector/gupshup-media/<signed>
                                                                customer files; DEAD after ~20 min
  gupshup        https://filemanager.gupshup.io/wa/<app>/wa/media/<id>?download=false
                                                                stickers

Anything else is not fetched. The archive downloads on a server inside the
project, so the allow-list is what stops a message body from steering it at an
internal address (SSRF).

This module is pure: no network, no database. `scripts/sql` discovery in
workflow 09 uses the same patterns, and tests/test_media_links.py holds the
two to the same examples.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Exact host per family. Matching the host exactly (not "endswith") is the
# point: `travelgate.bitrix24.ae.evil.example` must not qualify.
FAMILIES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("bitrix_rest", "travelgate.bitrix24.ae", re.compile(r"^/rest/\d+/[^/]+/download/")),
    ("bitrix_short", "travelgate.bitrix24.ae", re.compile(r"^/~[A-Za-z0-9]+$")),
    ("gupconnector", "gupconnector.cultivbureau.com", re.compile(r"^/connector/gupshup-media/")),
    ("gupshup", "filemanager.gupshup.io", re.compile(r"^/wa/")),
)

# Families whose link stops working within minutes. The archive fetches these
# first and gives up on them early, because a retry after the deadline is a
# guaranteed 410.
SHORT_LIVED = frozenset({"gupconnector"})

URL_RE = re.compile(r"https?://[^\s<>\"']+")
ATTACHMENT_RE = re.compile(r"^\[Attachment:\s*(?P<name>[^\]\n]{1,255})\]\s*$")
# What the API puts in front of a voice note or a file with no caption.
GENERIC_LINES = re.compile(r"^\s*(\[Attachment:[^\]]*\]|Voice message|\[Voice message\])\s*$",
                           re.IGNORECASE)

MEDIA_TYPES = frozenset({"audio", "image", "document", "video", "sticker", "file"})


@dataclass(frozen=True)
class Link:
    url: str
    family: str
    url_hash: str          # sha256 of the URL: the dedupe key, and safe to log


def classify(url: str) -> str | None:
    """The family of a URL, or None if the archive must not fetch it."""
    # `.port` parses lazily and raises on `:bad`, so every property is read
    # inside the guard: a malformed URL in a chat is "not fetchable", never a 500.
    try:
        parts = urlsplit(url)
        port, user, host = parts.port, parts.username, (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme != "https" or port not in (None, 443) or user:
        return None
    for family, family_host, path_re in FAMILIES:
        if host == family_host and path_re.search(parts.path):
            return family
    return None


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _clean(url: str) -> str:
    # Trailing punctuation from prose ("see https://x/~a.") is not part of a link.
    return url.rstrip(".,;:!?)]}>'\"،")


def find_links(body: str) -> list[Link]:
    """Every fetchable link in a message body, in order, without repeats."""
    seen: set[str] = set()
    out: list[Link] = []
    for match in URL_RE.finditer(body or ""):
        url = _clean(match.group(0))
        family = classify(url)
        if family and url not in seen:
            seen.add(url)
            out.append(Link(url=url, family=family, url_hash=url_hash(url)))
    return out


def attachment_name(body: str) -> str | None:
    """The file name the API wrote as `[Attachment: name.ext]`, if any."""
    for line in (body or "").splitlines():
        m = ATTACHMENT_RE.match(line.strip())
        if m:
            return m.group("name").strip()
    return None


def caption(body: str) -> str:
    """The human text of a media message: the body minus every fetchable URL
    and minus the API's own placeholder lines. This is what a pulled
    conversation shows above the file, and it never contains a REST token."""
    kept: list[str] = []
    for line in (body or "").splitlines():
        stripped = URL_RE.sub(lambda m: "" if classify(_clean(m.group(0))) else m.group(0), line)
        stripped = stripped.strip()
        if not stripped or GENERIC_LINES.match(stripped):
            continue
        kept.append(stripped)
    # What survives the URL removal can still hold a credential (a truncated
    # voice-note link next to a real attachment), so the projection is redacted
    # as a whole, last.
    return redact("\n".join(kept))


# The credential, wherever it appears — NOT only inside a URL the archive would
# fetch. Since 2026-09-23 every message also arrives as a second copy cut at
# ~200 characters with "...", and a cut voice-note link
# (`…/rest/1/<TOKEN>/do...`) is no longer a fetchable `bitrix_rest` URL but
# still carries the whole token (Codex/astra review, round 3). So redaction
# keys on the `<portal>.bitrix24.<tld>/rest/<user>/` prefix alone, and also
# swallows a token cut short by the truncation.
REST_TOKEN_RE = re.compile(r"(\bbitrix24\.[a-z]{2,}(?:\.[a-z]{2,})?/rest/\d+/)([^/\s\"'<>]+)",
                           re.IGNORECASE)


def redact(text: str) -> str:
    """Remove every Bitrix REST webhook token from free text.

    The token is a working credential. Anything this service returns, logs,
    stores as an error, or sends to a model must never contain it.
    """
    return REST_TOKEN_RE.sub(r"\1[redacted]", text or "")
