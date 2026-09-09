#!/usr/bin/env python3
"""Build and verify a byte-for-byte local archive of the ICPEAC 30 website."""

from __future__ import annotations

import argparse
import hashlib
from html import unescape
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen


ORIGIN = "https://www.icpeac30.edu.au"
MANIFEST = Path(__file__).with_name("icpeac30_manifest.txt")
EXPECTED_PAGES = 29
EXPECTED_ASSETS = 241
EXPECTED_TOTAL = 270
HTML_TYPES = {"text/html", "application/xhtml+xml"}
REFERENCE_ATTRIBUTE = re.compile(
    r"(?P<prefix>\b(?:href|src|action|poster|data)\s*=\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "javascript:", "data:")
MISSING_REFERENCE_FALLBACKS = {"iswamp/pics/iswampfavicon.ico": "favicon.ico"}


def load_manifest() -> list[str]:
    """Load and validate the authoritative path/query list."""
    entries = [line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines()]
    if any(not entry or not entry.startswith("/") for entry in entries):
        raise RuntimeError(f"invalid blank or non-absolute entry in {MANIFEST}")
    if len(entries) != EXPECTED_TOTAL:
        raise RuntimeError(
            f"manifest has {len(entries)} entries; expected {EXPECTED_TOTAL}"
        )
    if len(set(entries)) != len(entries):
        raise RuntimeError("manifest contains duplicate source entries")
    destinations = [output_path(Path("."), entry) for entry in entries]
    if len(set(destinations)) != len(destinations):
        raise RuntimeError("manifest source entries map to duplicate archive paths")
    return entries


def request_url(key: str) -> str:
    path, marker, query = key.partition("?")
    encoded = quote(path, safe="/%:@+~!$&'()*,;=-._")
    return ORIGIN + encoded + (marker + query if marker else "")


def output_path(root: Path, key: str) -> Path:
    path, marker, query = key.partition("?")
    if path.endswith("/"):
        path += "index.html"
    relative = PurePosixPath(path.lstrip("/"))
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError(f"unsafe archive path: {key!r}")
    filename = relative.name + (("?" + query) if marker else "")
    return root.joinpath(*relative.parent.parts, filename)


def fetch(key: str, attempts: int = 3) -> tuple[bytes, str]:
    """Fetch one manifest entry, retrying transient errors but never ignoring 404s."""
    url = request_url(key)
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers={"User-Agent": "ICPEAC30-archive-builder/1.0"})
            with urlopen(request, timeout=60, context=ssl.create_default_context()) as response:
                content_type = response.headers.get_content_type().lower()
                return response.read(), content_type
        except HTTPError as error:
            if error.code < 500 or attempt == attempts:
                raise RuntimeError(f"HTTP {error.code} while downloading {url}") from error
        except (URLError, TimeoutError, OSError) as error:
            if attempt == attempts:
                raise RuntimeError(f"failed to download {url}: {error}") from error
        time.sleep(attempt * 2)
    raise AssertionError("unreachable")


def restore(root: Path) -> tuple[list[Path], int]:
    """Download every manifest entry without modifying response bodies."""
    pages: list[Path] = []
    asset_count = 0
    for key in load_manifest():
        data, content_type = fetch(key)
        destination = output_path(root, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if content_type in HTML_TYPES:
            pages.append(destination)
        else:
            asset_count += 1
    return pages, asset_count


def verify_source(root: Path, pages: list[Path], assets: int) -> None:
    """Verify the authoritative source set before adding deployment helpers."""
    manifest_paths = [output_path(root, key) for key in load_manifest()]
    missing = [path.relative_to(root) for path in manifest_paths if not path.is_file()]
    files = [path for path in root.rglob("*") if path.is_file()]
    total = len(files)
    required = root / "_files" / "icpeacabstract.tar.gz"
    if missing:
        raise RuntimeError(f"archive verification failed: missing source paths: {missing}")
    if (len(pages), assets, len(manifest_paths), total) != (
        EXPECTED_PAGES,
        EXPECTED_ASSETS,
        EXPECTED_TOTAL,
        EXPECTED_TOTAL,
    ):
        raise RuntimeError(
            "archive verification failed: "
            f"observed {len(pages)} rendered pages, {assets} assets, "
            f"{len(manifest_paths)} manifest paths, {total} files; expected "
            f"{EXPECTED_PAGES}, {EXPECTED_ASSETS}, {EXPECTED_TOTAL}, {EXPECTED_TOTAL}"
        )
    if not required.is_file():
        raise RuntimeError(f"archive verification failed: missing {required.relative_to(root)}")
    print(
        f"Verified source archive: {len(pages)} rendered source responses, "
        f"{assets} assets, {total} manifest/source files"
    )


def asset_digests(root: Path, pages: list[Path]) -> dict[Path, str]:
    """Record source asset digests so deployment cannot silently alter binaries."""
    page_set = set(pages)
    return {
        path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
        for key in load_manifest()
        if (path := output_path(root, key)) not in page_set
    }


def split_reference(reference: str) -> tuple[str, str]:
    """Split a reference without normalizing or discarding its query/fragment."""
    indices = [index for marker in "?#" if (index := reference.find(marker)) >= 0]
    split_at = min(indices) if indices else len(reference)
    return reference[:split_at], reference[split_at:]


def deploy_reference(reference: str, page: Path, root: Path) -> str:
    """Make an internal reference suitable for the static browser view."""
    lower = reference.lower()
    if reference.startswith("//") or lower.startswith(EXTERNAL_SCHEMES):
        return reference
    path, suffix = split_reference(reference)
    if not path:
        return reference
    if path.lower().endswith(".php"):
        path = path[:-4] + ".html"
    if path.startswith("/"):
        archive_path = path.lstrip("/")
        if not archive_path or archive_path.endswith("/"):
            archive_path += "index.html"
    else:
        page_directory = page.parent.relative_to(root).as_posix()
        archive_path = posixpath.normpath(posixpath.join(page_directory, path))
        if archive_path not in MISSING_REFERENCE_FALLBACKS:
            return path + suffix
    archive_path = MISSING_REFERENCE_FALLBACKS.get(archive_path, archive_path)
    relative = posixpath.relpath(archive_path, page.parent.relative_to(root).as_posix())
    return relative + suffix


def create_static_view(root: Path, source_pages: list[Path]) -> list[Path]:
    """Create and rewrite text-only pages for GitHub Pages deployment."""
    browser_pages: list[Path] = []
    for source in source_pages:
        relative = source.relative_to(root)
        if source.suffix.lower() == ".php" and source.name.lower() != "index.php":
            page = source.with_suffix(".html")
            page.write_bytes(source.read_bytes())
        elif relative in {Path("index.html"), Path("iswamp/index.html")}:
            page = source
        else:
            # index.php is retained as source; the matching index.html is the view.
            continue

        text = page.read_text(encoding="utf-8")

        def replace(match: re.Match[str]) -> str:
            value = deploy_reference(match.group("value"), page, root)
            return match.group("prefix") + match.group("quote") + value + match.group("quote")

        page.write_text(REFERENCE_ATTRIBUTE.sub(replace, text), encoding="utf-8")
        browser_pages.append(page)
    return browser_pages


def validate_static_view(
    root: Path, pages: list[Path], source_pages: list[Path], before: dict[Path, str]
) -> None:
    """Fail with an explicit list of local page references absent from the archive."""
    unresolved: list[str] = []
    php_references: list[str] = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for match in REFERENCE_ATTRIBUTE.finditer(text):
            reference = unescape(match.group("value").strip())
            lower = reference.lower()
            if (
                not reference
                or reference.startswith("#")
                or reference.startswith("//")
                or lower.startswith(EXTERNAL_SCHEMES)
            ):
                continue
            path, _ = split_reference(reference)
            if not path:
                continue
            if path.lower().endswith(".php"):
                php_references.append(f"{page.relative_to(root)}: {reference}")
            candidate = Path(posixpath.normpath((page.parent / unquote(path)).as_posix()))
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            try:
                candidate.relative_to(root)
            except ValueError:
                unresolved.append(f"{page.relative_to(root)}: {reference} (outside archive)")
                continue
            if path.endswith("/"):
                candidate /= "index.html"
            if not candidate.is_file():
                unresolved.append(f"{page.relative_to(root)}: {reference}")
    if unresolved:
        details = "\n  ".join(unresolved)
        raise RuntimeError(f"unresolved internal references ({len(unresolved)}):\n  {details}")
    if php_references:
        details = "\n  ".join(php_references)
        raise RuntimeError(
            f"internal browser references to PHP ({len(php_references)}):\n  {details}"
        )

    manifest_paths = [output_path(root, key) for key in load_manifest()]
    missing = [path.relative_to(root) for path in manifest_paths if not path.is_file()]
    if missing:
        raise RuntimeError(f"deployment removed source paths: {missing}")
    after = asset_digests(root, source_pages)
    if before != after:
        raise RuntimeError("binary/source asset bytes changed during deployment preparation")

    required_views = [
        "scope.html",
        "posterlist.html",
        "speakers.html",
        "registration.html",
        "iswamp/program.html",
    ]
    absent_views = [name for name in required_views if not (root / name).is_file()]
    if absent_views:
        raise RuntimeError(f"required browser pages were not generated: {absent_views}")
    empty_views = [
        name
        for name in required_views
        if not (root / name).read_text(encoding="utf-8").strip()
    ]
    if empty_views:
        raise RuntimeError(f"required browser pages have no displayable content: {empty_views}")
    root_index = (root / "index.html").read_text(encoding="utf-8")
    if "scope.html" not in root_index or "scope.php" in root_index:
        raise RuntimeError("index.html navigation was not rewritten to scope.html")
    for name in ("scope.html", "iswamp/program.html"):
        text = (root / name).read_text(encoding="utf-8")
        if not re.search(r"href\s*=\s*(['\"])index\.html\1", text, re.IGNORECASE):
            raise RuntimeError(f"{name} WELCOME navigation does not use index.html")

    alias_count = sum(
        source.suffix.lower() == ".php" and source.name.lower() != "index.php"
        for source in source_pages
    )
    print(
        f"Validated static view: {alias_count} HTML aliases, {len(pages)} browser pages, "
        "zero internal PHP navigation links, all source asset bytes unchanged"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("icpeac30-archive"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary = Path(tempfile.mkdtemp(prefix=".icpeac30-", dir=output.parent))
    try:
        pages, assets = restore(temporary)
        verify_source(temporary, pages, assets)
        digests = asset_digests(temporary, pages)
        browser_pages = create_static_view(temporary, pages)
        validate_static_view(temporary, browser_pages, pages, digests)
        if output.exists():
            shutil.rmtree(output)
        os.replace(temporary, output)
    except Exception as error:
        shutil.rmtree(temporary, ignore_errors=True)
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
