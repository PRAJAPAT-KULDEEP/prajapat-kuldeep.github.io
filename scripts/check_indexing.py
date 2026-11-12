#!/usr/bin/env python3
"""Check that search engines can index the website. Uses only the Python standard library.

  python3 scripts/check_indexing.py site _site
      Checks the built site before it is published (the deploy workflow runs this).
  python3 scripts/check_indexing.py live https://prajapat-kuldeep.github.io
      Checks the live website (runs after every deploy). --warn-only reports without failing.

Every address in sitemap.xml must:
  - exist, and on the live site answer HTTP 200 at exactly that address (no redirect);
  - be open to Googlebot and Bingbot in robots.txt;
  - have no "noindex" or "none" in a robots/googlebot/bingbot meta tag or an X-Robots-Tag header;
  - have exactly one <link rel="canonical">, pointing to its own address, and a <title>.
Every old address that forwards to a new one (listed in redirects.json by jekyll-redirect-from) must:
  - have no noindex, so Google follows it instead of reporting "Excluded by 'noindex' tag";
  - forward with an instant meta refresh and a canonical link that both name the new address;
  - lead to a page that exists and doesn't forward again.
On the built site it also warns about indexable pages that are missing from the sitemap.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

CRAWLERS = ("Googlebot", "Bingbot")
ROBOTS_META_NAMES = {"robots", "googlebot", "bingbot"}
BLOCKING_DIRECTIVES = {"noindex", "none"}
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
USER_AGENT = (
    "Mozilla/5.0 (compatible; site-indexing-check/1.0; "
    "+https://github.com/PRAJAPAT-KULDEEP/prajapat-kuldeep.github.io)"
)
IN_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
MAX_ANNOTATIONS = 10  # GitHub shows at most 10 annotations of each kind per step


@dataclass
class Response:
    status: int
    text: str = ""
    final_url: str = ""
    x_robots: list[str] = field(default_factory=list)
    content_type: str = ""


class PageParser(HTMLParser):
    """Collects the tags that decide whether and where a page is indexed."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.robots: list[tuple[str, str]] = []
        self.canonicals: list[str] = []
        self.refresh: list[str] = []
        self.description: str | None = None
        self.title: str | None = None
        self._title_parts: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = a.get("name", "").strip().lower()
            if name in ROBOTS_META_NAMES:
                self.robots.append((name, a.get("content", "")))
            elif name == "description" and self.description is None:
                self.description = a.get("content", "")
            if a.get("http-equiv", "").strip().lower() == "refresh":
                self.refresh.append(a.get("content", ""))
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonicals.append(a.get("href", "").strip())
        elif tag == "title" and self._title_parts is None:
            self._title_parts = []  # only the first <title>; later ones may be inside SVG icons

    def handle_endtag(self, tag):
        if tag == "title" and self._title_parts is not None and self.title is None:
            self.title = " ".join("".join(self._title_parts).split())

    def handle_data(self, data):
        if self._title_parts is not None and self.title is None:
            self._title_parts.append(data)


def parse_page(text: str) -> PageParser:
    parser = PageParser()
    parser.feed(text)
    parser.close()
    return parser


def blocking(value: str) -> set[str]:
    """The directives in a robots meta content or X-Robots-Tag value that stop indexing."""
    found = set()
    for part in value.split(","):
        token = part.strip().lower()
        if ":" in token:  # "googlebot: noindex" (X-Robots-Tag), or "max-snippet:-1"
            token = token.split(":", 1)[1].strip()
        if token in BLOCKING_DIRECTIVES:
            found.add(token)
    return found


def parse_refresh(content: str) -> tuple[float | None, str]:
    """'0; url=https://example.org/' -> (0.0, 'https://example.org/')"""
    delay, _, rest = content.partition(";")
    rest = rest.strip()
    if rest[:4].lower() == "url=":
        rest = rest[4:].strip()
    try:
        seconds: float | None = float(delay.strip())
    except ValueError:
        seconds = None
    return seconds, rest.strip("'\"")


def without_query(url: str) -> str:
    return urllib.parse.urlunsplit(urllib.parse.urlsplit(url)._replace(query="", fragment=""))


class BuiltSite:
    """Reads pages from the _site folder that Jekyll built, as GitHub Pages would serve them."""

    live = False

    def __init__(self, root: Path, base: str) -> None:
        self.root = root
        self.base = base.rstrip("/")
        self.base_path = urllib.parse.urlsplit(self.base).path.rstrip("/")

    def path_of(self, url: str) -> str:
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path or "/")
        if self.base_path and (path == self.base_path or path.startswith(self.base_path + "/")):
            path = path[len(self.base_path):] or "/"
        return path

    def file_for(self, url: str) -> Path | None:
        rel = self.path_of(url).lstrip("/")
        if rel == "" or rel.endswith("/"):
            candidates = [rel + "index.html"]
        else:  # GitHub Pages also serves /resume from resume.html
            candidates = [rel, rel + ".html", rel + "/index.html"]
        for candidate in candidates:
            path = self.root / candidate
            if path.is_file():
                return path
        return None

    def fetch(self, url: str) -> Response:
        path = self.file_for(url)
        if path is None:
            return Response(404, final_url=url)
        is_text = path.suffix in (".html", ".htm", ".xml", ".txt", ".json")
        text = path.read_text(encoding="utf-8", errors="replace") if is_text else ""
        content_type = "text/html" if path.suffix in (".html", ".htm") else ""
        return Response(200, text, url, [], content_type)


class LiveSite:
    """Fetches pages from the live website, the way a crawler does."""

    live = True

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        # A unique query string skips cached copies, so a check right after a deploy sees new pages.
        self.token = str(int(time.time()))
        self.cache: dict[str, Response] = {}

    def fetch(self, url: str) -> Response:
        if url in self.cache:
            return self.cache[url]
        separator = "&" if "?" in url else "?"
        request = urllib.request.Request(
            f"{url}{separator}indexing-check={self.token}", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:
                content_type = reply.headers.get("Content-Type", "")
                wanted = content_type.startswith(("text/", "application/json", "application/xml"))
                text = reply.read().decode("utf-8", "replace") if wanted else ""
                response = Response(
                    reply.status,
                    text,
                    without_query(reply.geturl()),
                    reply.headers.get_all("X-Robots-Tag") or [],
                    content_type,
                )
        except urllib.error.HTTPError as error:
            headers = error.headers.get_all("X-Robots-Tag") if error.headers else None
            response = Response(error.code, "", url, headers or [])
        except (urllib.error.URLError, OSError) as error:
            response = Response(0, str(error), url)
        self.cache[url] = response
        return response


def is_html(url: str, response: Response) -> bool:
    if response.content_type:
        return response.content_type.startswith("text/html")
    path = urllib.parse.urlsplit(url).path
    return path.endswith(("/", ".html", ".htm"))


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.lines: list[str] = []

    def say(self, line: str = "") -> None:
        self.lines.append(line)


def check(source: BuiltSite | LiveSite, base: str) -> Report:
    report = Report()
    host = urllib.parse.urlsplit(base)[:2]

    def short(url: str) -> str:
        return (url[len(base):] or "/") if url.startswith(base) else url

    # robots.txt
    robots = source.fetch(base + "/robots.txt")
    rules = urllib.robotparser.RobotFileParser()
    if robots.status == 200:
        rules.parse(robots.text.splitlines())
        sitemaps = rules.site_maps() or []
        report.say(f"robots.txt: HTTP 200; Sitemap: {', '.join(sitemaps) or 'not listed'}")
        if base + "/sitemap.xml" not in sitemaps:
            report.warnings.append(f"robots.txt doesn't list {base}/sitemap.xml")
    elif robots.status in (404, 410):
        rules.parse([])
        report.warnings.append("robots.txt is missing: crawlers may fetch everything, but the Sitemap line is lost")
    else:
        rules.parse([])
        report.errors.append(f"robots.txt answers HTTP {robots.status}: Google pauses crawling while robots.txt fails")

    # sitemap.xml
    sitemap = source.fetch(base + "/sitemap.xml")
    if sitemap.status != 200:
        report.errors.append(f"sitemap.xml answers HTTP {sitemap.status}")
        return report
    try:
        root = ET.fromstring(sitemap.text)
    except ET.ParseError as error:
        report.errors.append(f"sitemap.xml is not valid XML: {error}")
        return report
    entries = []
    for node in root.iter(SITEMAP_NS + "url"):
        loc = (node.findtext(SITEMAP_NS + "loc") or "").strip()
        lastmod = (node.findtext(SITEMAP_NS + "lastmod") or "").strip()
        if loc:
            entries.append((loc, lastmod))
    listed = [loc for loc, _ in entries]
    report.say(f"sitemap.xml: {len(listed)} addresses")
    for loc in sorted({u for u in listed if listed.count(u) > 1}):
        report.errors.append(f"{short(loc)} is listed more than once in sitemap.xml")

    titles: dict[str, list[str]] = {}
    descriptions: dict[str, list[str]] = {}
    for url, lastmod in entries:
        problems = []
        if urllib.parse.urlsplit(url)[:2] != host:
            problems.append(f"is not on {base}")
        for bot in CRAWLERS:
            if not rules.can_fetch(bot, url):
                problems.append(f"is blocked for {bot} by robots.txt")
        response = source.fetch(url)
        detail = ""
        if response.status != 200:
            problems.append(f"answers HTTP {response.status}")
        elif source.live and response.final_url != url:
            problems.append(f"redirects to {response.final_url}; the sitemap should list the final address")
        for value in response.x_robots:
            if blocking(value):
                problems.append(f"sends the header X-Robots-Tag: {value}")
        if response.status == 200 and is_html(url, response):
            page = parse_page(response.text)
            for name, content in page.robots:
                if blocking(content):
                    problems.append(f'has <meta name="{name}" content="{content}">')
            if page.refresh:
                problems.append("forwards to another page (meta refresh), so it shouldn't be in the sitemap")
            if len(page.canonicals) != 1:
                problems.append(f"has {len(page.canonicals)} canonical links instead of 1")
            elif page.canonicals[0] != url:
                problems.append(f"has a canonical link to {page.canonicals[0]} instead of itself")
            if not page.title:
                problems.append("has no <title>")
            description = (page.description or "").strip()
            if not description:
                report.warnings.append(f"{short(url)} has no meta description")
            titles.setdefault(page.title or "", []).append(short(url))
            if description:
                descriptions.setdefault(description, []).append(short(url))
            robots_value = " / ".join(content for _, content in page.robots) or "no robots tag (indexable)"
            canonical = "self" if page.canonicals == [url] else ", ".join(page.canonicals) or "missing"
            detail = (
                f"robots: {robots_value} | canonical: {canonical} | "
                f"title {len(page.title or '')} chars | description {len(description)} chars"
            )
        elif response.status == 200:
            detail = response.content_type or "file"
        if lastmod:
            detail += f" | lastmod {lastmod}"
        report.say(f"  {'OK   ' if not problems else 'ERROR'} {short(url)}  {detail}")
        report.errors.extend(f"{short(url)} {problem}" for problem in problems)

    for title, urls in titles.items():
        if title and len(urls) > 1:
            report.warnings.append(f"same <title> on {', '.join(urls)}: {title}")
    for description, urls in descriptions.items():
        if len(urls) > 1:
            report.warnings.append(f"same meta description on {', '.join(urls)}")

    # Old addresses that forward to new ones (jekyll-redirect-from writes redirects.json).
    forwarding_paths: set[str] = set()
    redirects_file = source.fetch(base + "/redirects.json")
    redirects: dict[str, str] = {}
    if redirects_file.status == 200:
        try:
            redirects = json.loads(redirects_file.text)
        except json.JSONDecodeError:
            report.errors.append("redirects.json is not valid JSON")
    report.say(f"Old addresses that forward to new ones: {len(redirects)}")
    for old_path, target in sorted(redirects.items()):
        forwarding_paths.add(old_path)
        old_url = base + old_path
        problems = []
        response = source.fetch(old_url)
        detail = ""
        if response.status != 200:
            problems.append(f"answers HTTP {response.status}")
        elif source.live and response.final_url != old_url:
            detail = "HTTP redirect"
            if response.final_url != target:
                problems.append(f"redirects to {response.final_url}, not {target}")
        else:
            page = parse_page(response.text)
            for name, content in page.robots:
                if blocking(content):
                    problems.append(
                        f'has <meta name="{name}" content="{content}">, so Google reports it as '
                        "\"Excluded by 'noindex' tag\" instead of following it"
                    )
            for value in response.x_robots:
                if blocking(value):
                    problems.append(f"sends the header X-Robots-Tag: {value}")
            if not page.refresh:
                problems.append("has no meta refresh")
            else:
                seconds, destination = parse_refresh(page.refresh[0])
                if seconds != 0:
                    problems.append(f"waits {seconds} s before forwarding (only an instant refresh counts as permanent)")
                if destination != target:
                    problems.append(f"forwards to {destination}, not {target}")
            if page.canonicals != [target]:
                problems.append(f"has canonical {', '.join(page.canonicals) or 'missing'} instead of {target}")
            robots_value = " / ".join(content for _, content in page.robots) or "no robots tag"
            detail = f"{robots_value} | refresh {'0 s' if page.refresh else 'missing'}"
        landing = source.fetch(target)
        if landing.status != 200:
            problems.append(f"leads to {target}, which answers HTTP {landing.status}")
        elif is_html(target, landing) and parse_page(landing.text).refresh:
            problems.append(f"leads to {target}, which forwards again (a redirect chain)")
        note = "" if target in listed else "  (target not in sitemap)"
        report.say(f"  {'OK   ' if not problems else 'ERROR'} {old_path} -> {short(target)}  {detail}{note}")
        report.errors.extend(f"old address {old_path} {problem}" for problem in problems)

    if source.live:
        # http:// should switch to https:// (Settings > Pages > Enforce HTTPS).
        plain = source.fetch("http://" + urllib.parse.urlsplit(base).netloc + "/")
        if plain.status == 200 and not plain.final_url.startswith("https://"):
            report.warnings.append("http:// pages are not redirected to https:// (turn on Enforce HTTPS)")
    else:
        # Pages that can be indexed but are missing from the sitemap.
        listed_paths = {source.path_of(u) for u in listed}
        for path in sorted(source.root.rglob("*.html")):
            rel = "/" + path.relative_to(source.root).as_posix()
            url_path = rel[: -len("index.html")] if rel.endswith("/index.html") else rel
            if url_path in listed_paths or url_path in forwarding_paths:
                continue
            if re.fullmatch(r"/google[0-9a-f]+\.html", url_path):
                continue  # Search Console verification file
            page = parse_page(path.read_text(encoding="utf-8", errors="replace"))
            if page.refresh or any(blocking(content) for _, content in page.robots):
                continue
            if page.canonicals and page.canonicals[0] != base + url_path:
                continue  # a duplicate that points search engines at its main address
            report.warnings.append(f"{url_path} can be indexed but is not in sitemap.xml (add it, or set noindex: true)")

    return report


def annotate(kind: str, messages: list[str]) -> None:
    if not IN_GITHUB_ACTIONS or not messages:
        return
    shown = messages[: MAX_ANNOTATIONS - 1] if len(messages) > MAX_ANNOTATIONS else messages
    for message in shown:
        print(f"::{kind} title=Indexing check::{message}")
    if len(messages) > len(shown):
        print(f"::{kind} title=Indexing check::...and {len(messages) - len(shown)} more (see the log)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["site", "live"])
    parser.add_argument("target", help="the built _site folder (site) or the website address (live)")
    parser.add_argument("--base-url", help="site mode: the site's address (default: taken from sitemap.xml)")
    parser.add_argument("--warn-only", action="store_true", help="report problems without failing")
    parser.add_argument("--retries", type=int, default=2, help="live mode: checks to repeat if problems are found")
    parser.add_argument("--retry-delay", type=int, default=45, help="live mode: seconds between checks")
    args = parser.parse_args()

    if args.mode == "site":
        root = Path(args.target)
        base = args.base_url
        if not base:
            try:
                first = next(ET.parse(root / "sitemap.xml").getroot().iter(SITEMAP_NS + "loc")).text or ""
            except (OSError, ET.ParseError, StopIteration):
                print(f"Can't read {root / 'sitemap.xml'}; pass --base-url")
                return 1
            parts = urllib.parse.urlsplit(first.strip())
            base = f"{parts.scheme}://{parts.netloc}"
        base = base.rstrip("/")
        print(f"Indexing check of the built site in {root} (address {base})")
        report = check(BuiltSite(root, base), base)
    else:
        base = args.target.rstrip("/")
        attempts = 1 if args.warn_only else args.retries + 1
        for attempt in range(1, attempts + 1):
            report = check(LiveSite(base), base)
            if not report.errors or attempt == attempts:
                break
            print(f"{len(report.errors)} problem(s) found; checking again in {args.retry_delay} s "
                  "in case the site was still updating...")
            time.sleep(args.retry_delay)
        print(f"Indexing check of the live site {base}")

    print("\n".join(report.lines))
    print()
    for message in report.errors:
        print(f"{'WARNING' if args.warn_only else 'ERROR'}: {message}")
    for message in report.warnings:
        print(f"WARNING: {message}")
    print(f"\nResult: {len(report.errors)} error(s), {len(report.warnings)} warning(s)"
          + (" (reported only, not failing)" if args.warn_only and report.errors else ""))
    annotate("warning" if args.warn_only else "error", report.errors)
    annotate("warning", report.warnings)
    return 1 if report.errors and not args.warn_only else 0


if __name__ == "__main__":
    sys.exit(main())
