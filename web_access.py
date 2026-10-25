"""Bounded public-web text fetch for explicit user URLs and approved agent calls."""

from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 512_000
MAX_TEXT = 14_000
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.I)


def public_url(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise ValueError("Provide one public http(s) URL of at most 2048 characters.")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public http(s) URLs without credentials are supported.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL port.") from exc
    if port not in (None, 80, 443):
        raise ValueError("Only standard HTTP and HTTPS ports are supported.")
    if parsed.hostname.casefold() in {"localhost", "localhost.localdomain"} or parsed.hostname.endswith(".local"):
        raise ValueError("Local and private URLs are not available to the web tool.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError(f"Could not resolve website: {parsed.hostname}") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Local and private network addresses are not available to the web tool.")
    return url


class SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        public_url(new_url)
        return super().redirect_request(request, fp, code, msg, headers, new_url)


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "svg", "noscript"}:
            self.skip += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "svg", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip() + " ")


def _fetch_text(url, accept="text/html, text/plain, application/json"):
    public_url(url)
    opener = build_opener(SafeRedirects)
    request = Request(url, headers={"User-Agent": "LocalWorkflow/1.0", "Accept": accept})
    try:
        with opener.open(request, timeout=20) as response:
            media = response.headers.get_content_type()
            if media not in {"text/html", "text/plain", "text/markdown", "application/json",
                             "application/vnd.github.raw+json", "application/xml", "text/xml",
                             "application/octet-stream"}:
                raise ValueError(f"Website returned unsupported content type: {media}")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("Website response exceeds the 512 KB reading limit.")
            charset = response.headers.get_content_charset() or "utf-8"
            text = data.decode(charset, errors="replace")
            final_url = response.geturl()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ValueError(f"Could not fetch {url}: {exc}") from exc
    if media == "text/html":
        parser = TextExtractor()
        parser.feed(text)
        text = re.sub(r"\n\s*\n+", "\n", "".join(parser.parts))
    return {"url": final_url, "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT}


def fetch_public_url(url):
    """Fetch a public page; GitHub repository roots include API metadata and README."""
    public_url(url)
    parsed = urlsplit(url)
    segments = [part for part in parsed.path.strip("/").split("/") if part]
    if parsed.hostname.casefold() == "github.com" and len(segments) == 2:
        owner, repo = segments
        if not re.fullmatch(r"[\w.-]+", owner) or not re.fullmatch(r"[\w.-]+", repo):
            raise ValueError("Invalid GitHub repository path.")
        api = f"https://api.github.com/repos/{owner}/{repo.removesuffix('.git')}"
        metadata = _fetch_text(api, "application/vnd.github+json")
        try:
            details = json.loads(metadata["text"])
            summary = {key: details.get(key) for key in ("full_name", "description", "language", "topics", "html_url", "default_branch")}
        except (ValueError, TypeError):
            summary = {"metadata": metadata["text"][:4000]}
        try:
            readme = _fetch_text(api + "/readme", "application/vnd.github.raw+json")
            summary["readme"] = readme["text"][:10000]
            summary["readme_url"] = readme["url"]
        except ValueError as exc:
            summary["readme_error"] = str(exc)
        return {"url": url, "repository": summary}
    return _fetch_text(url)


def urls_in_text(text, limit=2):
    return list(dict.fromkeys(match.group(0).rstrip(".,);]") for match in URL_PATTERN.finditer(text)))[:limit]
