"""The reference base: Wikipedia articles, other web pages and local files, read once and kept."""
import hashlib
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .llm import LLMError
from .project import ProjectError

# Wikimedia refuses anonymous-looking clients with a 403 unless the agent points somewhere and the Accept is explicit.
HEADERS = {"User-Agent": "NiftyTranslator/0.1 (+https://github.com/dokluch/nifty-translator)",
           "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
SKIPPED_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "svg"}


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        self.skipping += tag in SKIPPED_TAGS

    def handle_endtag(self, tag):
        self.skipping -= tag in SKIPPED_TAGS and self.skipping > 0

    def handle_data(self, data):
        if not self.skipping and data.strip():
            self.parts.append(data.strip())


def fetch(url: str) -> str:
    """The article text through MediaWiki's API for Wikipedia; the page's visible text for anything else."""
    parsed = urlparse(url)
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60) as http:
        if parsed.netloc.endswith("wikipedia.org") and parsed.path.startswith("/wiki/"):
            r = http.get(f"https://{parsed.netloc}/w/api.php", params={
                "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "format": "json",
                "formatversion": 2, "titles": unquote(parsed.path.removeprefix("/wiki/")),
            })
            r.raise_for_status()
            return r.json()["query"]["pages"][0].get("extract", "")
        r = http.get(url)
        r.raise_for_status()
    if "html" not in r.headers.get("content-type", "html"):
        return r.text
    parser = TextOnly()
    parser.feed(r.text)
    return "\n".join(parser.parts)


def read_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True).stdout
    return path.read_text()


def load(entries: list[str], base: Path, cache_dir: Path) -> list[tuple[str, str]]:
    """Returns (label, text) pairs. A reference that cannot be read stops the run:
    the glossary would otherwise look complete without what was asked of it."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    found = []
    for entry in entries:
        if entry.startswith(("http://", "https://")):
            kept = cache_dir / f"{hashlib.sha256(entry.encode()).hexdigest()[:16]}.txt"
            if not kept.exists():
                try:
                    text = fetch(entry)
                except httpx.HTTPStatusError as e:
                    raise LLMError(f"could not read the reference {entry}: HTTP {e.response.status_code}") from e
                except (httpx.HTTPError, KeyError, ValueError) as e:
                    raise LLMError(f"could not read the reference {entry}: {e}") from e
                if not text.strip():
                    raise ProjectError(f"reference {unquote(entry)} has no text; check the address or the title")
                kept.write_text(text)
            found.append((unquote(entry), kept.read_text()))
            continue
        paths = [base / entry] if (base / entry).is_file() else sorted(p for p in base.glob(entry) if p.is_file())
        if not paths:
            raise ProjectError(f"reference not found: {entry!r} in {base}")
        found += [(str(path.relative_to(base)), read_file(path)) for path in paths]
    for label, text in found:
        if not text.strip():
            raise ProjectError(f"reference {label} has no text; check the address or the title")
    return found
