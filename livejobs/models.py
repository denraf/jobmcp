from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass
class Job:
    vendor: str
    board: str
    title: str
    company: str
    location: str
    url: str
    posted_at: str = ""
    description: str = ""

    @property
    def uid(self) -> str:
        """Stable across runs even if the vendor's own id churns."""
        basis = (
            f"{self.vendor}|{self.board.lower()}|"
            f"{self.title.lower().strip()}|{self.url.strip()}"
        )
        return hashlib.sha1(basis.encode()).hexdigest()[:16]


# Limits, applied in one place (when a posting is stored). They are far above
# any real posting, and if one is ever exceeded the text says so instead of
# stopping mid-sentence.
#   MAX_DESCRIPTION: characters of TEXT (embedded images are not counted here).
#   MAX_IMAGE / MAX_IMAGES: one embedded picture, and all of them together.
#     Database columns hold 16 MB, and a picture bigger than a couple of
#     megabytes is not a screenshot in a job post.
MAX_DESCRIPTION = 200_000
MAX_IMAGE = 2_000_000
MAX_IMAGES = 8_000_000
TRUNCATION_NOTE = (
    "\n\n[livejobs: this description was longer than 200,000 characters and "
    "has been shortened.]"
)

_TAG = re.compile(r"<[^>]+>")
_PARA_END = re.compile(
    r"</\s*(p|h[1-6]|ul|ol|table|section|blockquote)\s*>", re.I
)  # paragraph break: a blank line
_LINE_END = re.compile(
    r"</\s*(div|tr)\s*>|<\s*br\s*/?\s*>", re.I
)  # line break (list items break at their start)
_LI_START = re.compile(r"<\s*li\b[^>]*>", re.I)
_INLINE_WS = re.compile(r"[ \t\r\f\v\u00a0]+")
_BLANK_RUNS = re.compile(r"\n[ ]*(?:\n[ ]*)+")


# A picture pasted into a posting can arrive inline as a data URI. Ashby's
# plain text carries them as `[data:image/png;base64,iVBOR...]`, one of which
# was 298,000 characters. They are kept (the website draws them), but only
# real picture formats: an SVG or HTML "image" is replaced by a marker.
IMAGE_TYPES = ("png", "jpeg", "jpg", "gif", "webp")
_DATA_URI = re.compile(
    r"(?:\[[ \t]*)?(data:([\w.+/-]+);base64,[A-Za-z0-9+/=_-]+)(?:[ \t]*\])?"
)
_IMG_DATA = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*[\"']?(data:[\w.+/-]+;base64,[A-Za-z0-9+/=_-]+)"
    r"[\"']?[^>]*>",
    re.I,
)


def _keep_image(m: re.Match) -> str:
    uri, mime = m.group(1), m.group(2).lower()
    ok = mime.startswith("image/") and mime[6:] in IMAGE_TYPES
    return f"[{uri}]" if ok and len(uri) <= MAX_IMAGE else "[image]"


def strip_data_uris(text: str | None) -> str:
    """The text without its embedded pictures (each becomes '[image]'). What
    agents are given, and what search looks through: a picture is hundreds of
    kilobytes of base64 that is neither readable nor searchable."""
    t = text or ""
    return _DATA_URI.sub("[image]", t) if "data:" in t else t


def split_images(text: str | None) -> list[tuple[str, str]]:
    """[('text', ...), ('image', 'data:image/png;base64,...'), ...]"""
    t, out, pos = text or "", [], 0
    for m in _DATA_URI.finditer(t):
        if m.start() > pos:
            out.append(("text", t[pos : m.start()]))
        out.append(("image", m.group(1)))
        pos = m.end()
    if pos < len(t):
        out.append(("text", t[pos:]))
    return out


def normalize_text(text: str | None) -> str:
    """Tidy plain text without destroying its structure: trim each line,
    collapse runs of spaces, and allow at most
    one blank line between paragraphs."""
    text = text or ""
    if "data:" in text:
        text = _DATA_URI.sub(_keep_image, text)
    text = _INLINE_WS.sub(" ", text.replace("\r\n", "\n"))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUNS.sub("\n\n", text).strip()


def clean_html(html: str | None) -> str:
    """HTML to readable plain text. Paragraphs and headings become separate
    lines and list items become '- ' bullets,
    so a list of qualifications still reads as a list. Never shortens the
    text."""
    import html as _html

    text = _html.unescape(
        html or ""
        # Greenhouse sends the HTML itself escaped (&lt;p&gt;), so unescape
        # first
    )
    if "data:" in text:  # keep embedded pictures: <img src="data:..."> -> [..]
        text = _IMG_DATA.sub(r"\n[\1]\n", text)
    text = _LI_START.sub(
        "\n- ", _LINE_END.sub("\n", _PARA_END.sub("\n\n", text))
    )
    text = _TAG.sub(" ", text)
    text = _TAG.sub(
        " ", _html.unescape(text)
    )  # entities that were escaped twice (&amp;nbsp;)
    return normalize_text(text)


def fit(description: str | None) -> str:
    """The one place a description may be shortened. Text beyond
    MAX_DESCRIPTION is cut, visibly; pictures beyond MAX_IMAGES in total are
    replaced by '[image]'. A picture is never cut in half."""
    d = description or ""
    if "data:" not in d:
        return _fit_text(d)
    pieces, total, text_len = [], 0, 0
    for kind, value in split_images(d):
        if kind == "image":
            if total + len(value) > MAX_IMAGES:
                value = None
            else:
                total += len(value)
            pieces.append(("image", value))
        else:
            text_len += len(value)
            pieces.append(("text", value))
    if text_len > MAX_DESCRIPTION:  # absurdly long text: drop the pictures
        return _fit_text(strip_data_uris(d))
    return "".join(
        f"[{v}]" if k == "image" and v else "[image]" if k == "image" else v
        for k, v in pieces
    )


def _fit_text(d: str) -> str:
    if len(d) <= MAX_DESCRIPTION:
        return d
    return (
        d[: MAX_DESCRIPTION - len(TRUNCATION_NOTE)].rstrip() + TRUNCATION_NOTE
    )
