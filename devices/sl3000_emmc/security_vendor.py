"""Read Tailscale's official RSS advisories without GitHub credentials."""

from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


TAILSCALE_FEED = "https://tailscale.com/security-bulletins/index.xml"


class BulletinHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sections = {"summary": []}
        self.section = "summary"
        self.heading = None
        self.heading_tag = None

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3", "h4", "h5", "strong") and self.heading is None:
            self.heading = []
            self.heading_tag = tag

    def handle_data(self, data):
        (self.heading if self.heading is not None else self.sections[self.section]).append(data)

    def handle_endtag(self, tag):
        if tag == self.heading_tag and self.heading is not None:
            title = " ".join("".join(self.heading).split())
            if title in ("What happened?", "What was the impact?", "What is the impact?", "Who was affected?",
                         "Who is affected?", "What do I need to do?", "Credits"):
                self.section = title
                self.sections.setdefault(self.section, [])
            else:
                self.sections[self.section].extend(self.heading)
            self.heading = None
            self.heading_tag = None
        elif tag in ("p", "li", "br"):
            self.sections[self.section].append(" ")

    def text(self, section):
        return " ".join("".join(self.sections.get(section, [])).split())


def parse_tailscale_bulletins(data):
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise ValueError("Invalid Tailscale security RSS") from error
    items = root.findall("./channel/item")
    if root.tag != "rss" or not items:
        raise ValueError("Tailscale security RSS contains no bulletins")
    results, seen = [], set()
    for item in items:
        identifier = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        parsed = urlparse(url)
        if not re.fullmatch(r"TS-\d{4}-\d{3}", identifier) or identifier in seen:
            raise ValueError("Invalid or duplicate Tailscale bulletin ID")
        if parsed.scheme != "https" or parsed.netloc != "tailscale.com" or parsed.path.rstrip("/") != "/security-bulletins":
            raise ValueError("Unexpected Tailscale bulletin URL")
        seen.add(identifier)
        html = BulletinHTML()
        html.feed(item.findtext("description") or "")
        title = html.text("summary").removeprefix("Description:").strip()
        affected = html.text("Who was affected?") or html.text("Who is affected?")
        if not title or not affected:
            raise ValueError(f"Tailscale bulletin lacks its description or affected scope: {identifier}")
        fixes = set(re.findall(r"This vulnerability is fixed in Tailscale version (\d+\.\d+\.\d+)\b",
                               html.text("What happened?"), re.I))
        patched = next(iter(fixes)) if len(fixes) == 1 else None
        try:
            date = parsedate_to_datetime(item.findtext("pubDate") or "")
            if date.tzinfo is None:
                raise ValueError("Missing publication timezone")
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid Tailscale bulletin publication date") from error
        results.append({"id": identifier, "repository": "tailscale/tailscale", "vendor": "tailscale",
                        "severity": "unknown", "title": title, "url": url, "status": "needs_review",
                        "updated_at": date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "condition": affected, "remediation": html.text("What do I need to do?"),
                        "vulnerabilities": [{"package": {"name": "tailscale"}, "patched_versions": patched}]})
    return results


def tailscale_bulletins():
    for attempt in range(3):
        try:
            request = Request(TAILSCALE_FEED, headers={"User-Agent": "sl3000-security-monitor",
                                                       "Accept": "application/rss+xml, application/xml"})
            with urlopen(request, timeout=25) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("Tailscale security RSS exceeds the size limit")
            return parse_tailscale_bulletins(data)
        except HTTPError as error:
            if (error.code != 429 and error.code < 500) or attempt == 2:
                raise ValueError(f"Tailscale security RSS HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError):
            if attempt == 2:
                raise ValueError("Tailscale security RSS request failed") from None
        time.sleep(2 ** (attempt + 1))
