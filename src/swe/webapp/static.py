"""Where the web app's files live, and which names the hub may serve."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"

# A flat name with one known extension: no slashes, no dot segments, so
# a request can never reach outside STATIC_DIR.
_ASSET_NAME = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.(?:html|js|css|svg|png|ico|webmanifest)$")


def is_asset_name(name: str) -> bool:
    return bool(_ASSET_NAME.match(name)) and (STATIC_DIR / name).is_file()


# The session view (app/web-session, built into static/session/ and
# committed). Its build writes build-info.json, whose ``outputs`` lists
# every file the build made; the hub serves those names and nothing else.
SESSION_DIR = STATIC_DIR / "session"
SESSION_INFO = "build-info.json"
# One path segment: a name with an extension, no leading dot.
_SESSION_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*\.[A-Za-z0-9]+$")
_SESSION_DIRS = frozenset({"assets"})
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
# Every /session/ response carries this policy in place of the hub's
# default one. The built page needs nothing more: its scripts and
# stylesheets are files on this origin (no inline code, no eval), it
# fetches only /v1 on this origin, and it frames only this origin.
# ``frame-ancestors 'self'`` keeps the default policy's framing rule,
# which a route's own policy would otherwise drop.
SESSION_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "frame-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self'"
)
# Hashed names never change: a browser keeps them for a year.
SESSION_IMMUTABLE = "public, max-age=31536000, immutable"
# What /session/ answers when the bundle is not in this install.
SESSION_MISSING = (
    b"<!doctype html>\n<meta charset=\"utf-8\">\n<title>SWE session</title>\n"
    b"<p>The session view is not built in this copy of SWE. Run "
    b"<code>pnpm install --frozen-lockfile &amp;&amp; pnpm build</code> in "
    b"<code>app/web-session</code>, or install a release that ships it.</p>\n"
)
# Each folder's build-info.json, read again only when it changes:
# folder -> (mtime_ns, size, names).
_outputs_cache: dict[Path, tuple[int, int, frozenset[str]]] = {}


def _session_name_ok(rel: str) -> bool:
    """``rel`` is ``name.ext`` or ``assets/name.ext``: no dot segment, no
    leading dot, no backslash or percent escape, nothing absolute."""
    folder, _sep, name = rel.rpartition("/")
    if folder and folder not in _SESSION_DIRS:
        return False
    return bool(_SESSION_SEGMENT.match(name)) and ".." not in name


def _session_outputs(base: Path) -> frozenset[str]:
    """The ``outputs`` names in ``base``'s build-info.json, or none."""
    info = base / SESSION_INFO
    try:
        st = info.stat()
    except OSError:
        return frozenset()
    cached = _outputs_cache.get(base)
    if cached and cached[:2] == (st.st_mtime_ns, st.st_size):
        return cached[2]
    try:
        data = json.loads(info.read_text(encoding="utf-8"))
        names = frozenset(n for n in data.get("outputs", []) if isinstance(n, str))
    except (OSError, ValueError, AttributeError):
        names = frozenset()
    _outputs_cache[base] = (st.st_mtime_ns, st.st_size, names)
    return names


def session_file(rel: str, base: Path | None = None) -> Path | None:
    """The session view file ``rel`` names (``""`` is ``index.html``), or
    None. Only a name the committed build lists in build-info.json, that is
    a regular file here and not a symlink, and that resolves inside the
    session folder, is ever returned."""
    base = SESSION_DIR if base is None else base
    rel = rel or "index.html"
    if not _session_name_ok(rel) or rel not in _session_outputs(base):
        return None
    target = base / rel
    try:
        # Neither the file nor the folder it sits in may be a link, and
        # what it resolves to must still be inside the bundle.
        if target.is_symlink() or (target.parent != base and target.parent.is_symlink()) or not target.is_file():
            return None
        if base.resolve() not in target.resolve().parents:
            return None
    except OSError:
        return None
    return target


def session_built(base: Path | None = None) -> bool:
    """Whether the session view's page is there to serve."""
    return session_file("index.html", base) is not None


def session_immutable(rel: str) -> bool:
    """Hashed build files (everything under ``assets/``) never change under
    their name; the page, the stable entry names and build-info.json do."""
    return rel.startswith("assets/")


def content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix, "application/octet-stream")


def etag_for(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
