"""Google News RSS collector. Never authorizes or submits trades."""
import hashlib
import json
import os
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path("/var/lib/si-equity-buy-worker")
DEST = ROOT / "google-news.json"
URL = "https://news.google.com/rss/search?" + urlencode({
    "q": "US stocks company earnings news when:1d",
    "hl": "en-US",
    "gl": "US",
    "ceid": "US:en",
})

def now():
    return datetime.now(timezone.utc).isoformat()

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("RSS_REDIRECT_REQUIRES_REVIEW")

def atomic(data):
    fd, name = tempfile.mkstemp(prefix=".google-news-", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, DEST)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def parse(raw, reference=None):
    if len(raw) > 1_000_000:
        raise ValueError("RSS_TOO_LARGE")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("XML_DECLARATION_REFUSED")

    tree = ET.fromstring(raw)
    if tree.tag != "rss" or tree.find("channel") is None:
        raise ValueError("RSS_CHANNEL_REQUIRED")

    current = reference or datetime.now(timezone.utc)
    articles = []
    seen = set()

    for item in tree.findall("./channel/item")[:100]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = item.find("source")
        publisher = (
            (source.text or "").strip()
            if source is not None else ""
        )
        parsed = urlsplit(link)
        if (
            not title
            or not publisher
            or parsed.scheme != "https"
            or parsed.hostname != "news.google.com"
        ):
            continue

        try:
            published = parsedate_to_datetime(
                item.findtext("pubDate") or ""
            )
            if published.tzinfo is None:
                continue
            published = published.astimezone(timezone.utc)
            age = (current - published).total_seconds()
        except (ValueError, TypeError, OverflowError):
            continue

        if not -300 <= age <= 86400:
            continue

        identity = (item.findtext("guid") or link).strip()
        digest = hashlib.sha256(identity.encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)

        articles.append({
            "id": digest,
            "title": title[:1000],
            "google_news_url": link,
            "publisher": publisher[:200],
            "published_at": published.isoformat(),
        })

    return sorted(
        articles, key=lambda article: article["published_at"],
        reverse=True,
    )

def collect():
    previous = {}
    try:
        previous = json.loads(DEST.read_text())
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        pass

    if time.time() < previous.get("retry_not_before_epoch", 0):
        print(json.dumps({
            "state": "BACKOFF",
            "news_risk_verified": False,
            "orders_submitted": False,
        }))
        return 0

    report = {
        **previous,
        "source": "GOOGLE_NEWS_RSS",
        "feed_url": URL,
        "attempted_at": now(),
        "news_risk_verified": False,
        "entry_authorized": False,
        "orders_submitted": False,
    }

    try:
        request = Request(URL, headers={
            "User-Agent": "SuperIntelligence-NewsCollector/1.0",
            "Accept": "application/rss+xml, application/xml;q=0.9",
        })
        with build_opener(NoRedirect()).open(
            request, timeout=25
        ) as response:
            raw = response.read(1_000_001)

        articles = parse(raw)
        report.update(
            state=(
                "HEADLINES_COLLECTED_NOT_RISK_SCORED"
                if articles else "NO_RECENT_VALID_HEADLINES"
            ),
            last_successful_fetch_at=now(),
            articles=articles,
            retry_not_before_epoch=0,
            error_type=None,
            http_status=None,
        )
        atomic(report)
        print(json.dumps({
            "state": report["state"],
            "article_count": len(articles),
            "news_risk_verified": False,
            "entry_authorized": False,
            "orders_submitted": False,
        }))
        return 0

    except Exception as error:
        delay = 900
        code = None
        if isinstance(error, HTTPError):
            code = error.code
            if code in (403, 429):
                delay = 3600
            retry = error.headers.get("Retry-After")
            if retry:
                try:
                    seconds = float(retry)
                    if not 0 <= seconds <= 604800:
                        raise ValueError("INVALID_RETRY_AFTER")
                    delay = max(delay, seconds)
                except ValueError:
                    try:
                        delay = max(
                            delay,
                            parsedate_to_datetime(retry).timestamp()
                            - time.time(),
                        )
                    except (ValueError, TypeError, OverflowError):
                        pass

        report.update(
            state="FETCH_FAILED_PREVIOUS_ARTICLES_NOT_CURRENT",
            error_type=type(error).__name__,
            http_status=code,
            retry_not_before_epoch=time.time() + delay,
        )
        atomic(report)
        print(json.dumps({
            "state": report["state"],
            "error_type": type(error).__name__,
            "http_status": code,
            "news_risk_verified": False,
            "orders_submitted": False,
        }))
        return 1

if __name__ == "__main__":
    os.umask(0o077)
    if not ROOT.is_dir():
        raise SystemExit("STATE_DIRECTORY_MISSING")
    sys.exit(collect())
