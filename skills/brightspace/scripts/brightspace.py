#!/usr/bin/env python3
"""
Brightspace read-only feed client (stdlib only).

Reads token-authenticated D2L Brightspace feeds:
  - BRIGHTSPACE_ICS_URL: Calendar + Tasks iCal (.ics) feed
  - BRIGHTSPACE_RSS_URLS: Announcements RSS 2.0 / Atom XML feed(s), comma-separated
  - BRIGHTSPACE_TZ (optional): display timezone override; default is system local time

Configuration comes only from the process environment and command-line flags;
the script never reads .env or other secrets files itself.

Exit codes: 0 success, 1 fetch/parse failure, 2 missing configuration.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import http.client
import json
import os
import pathlib
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


USER_AGENT = "Hermes-Brightspace-Skill/0.2"
MAX_FEED_BYTES = 10 * 1024 * 1024


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2}?)(?::?(\d{2}))?$", re.I)


def resolve_tz(tz_name: str | None) -> dt.tzinfo | None:
    """Return the override timezone from --tz or $BRIGHTSPACE_TZ, or None.

    None (the default) means "system local time". Every timestamp is then
    converted with a bare datetime.astimezone(), which asks the OS for the
    offset in force at that instant: DST-correct on Linux, macOS and Windows,
    no timezone database needed. On POSIX, libc already honours $TZ there.

    Overrides accept "UTC", fixed offsets like "-05:00" (no DST, but no
    database either), or IANA names like "Europe/London", which need the
    system zoneinfo database (Linux/macOS) or the `tzdata` Python package (Windows).
    """
    name = (tz_name or os.environ.get("BRIGHTSPACE_TZ") or "").strip()
    if not name:
        return None
    if name.upper() in ("UTC", "GMT", "Z"):
        return dt.timezone.utc
    m = _OFFSET_RE.match(name)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        if hours < 24 and minutes < 60:
            delta = dt.timedelta(hours=hours, minutes=minutes)
            return dt.timezone(sign * delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        print(
            f"Warning: timezone {name!r} not found; using system local time instead. "
            "IANA names need a timezone database (on Windows, the `tzdata` Python package); "
            "a fixed offset such as -05:00 always works but ignores DST.",
            file=sys.stderr,
        )
        return None


def local_now(target_tz: dt.tzinfo | None) -> dt.datetime:
    # Note: astimezone(None) means "system local time"; that is the whole trick.
    return dt.datetime.now(dt.timezone.utc).astimezone(target_tz)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disallow scheme downgrades (https -> http) and non-HTTP(S) redirect targets."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target_scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        orig_scheme = urllib.parse.urlsplit(req.full_url).scheme.lower()
        if target_scheme not in ("http", "https") or (orig_scheme == "https" and target_scheme != "https"):
            raise urllib.error.HTTPError(req.full_url, code, "unsafe redirect scheme", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, label: str = "feed", timeout: int = 20) -> str:
    """Fetch a feed. Errors name the feed by *label*, never by any part of its URL."""
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        raise ValueError(f"{label}: invalid URL (contains whitespace or control characters).")
    # urllib also speaks file:// and ftp://; refuse them so a feed URL can never
    # be used to read local files such as the Hermes .env.
    parts = urllib.parse.urlsplit(cleaned)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"{label}: refusing a non-HTTP(S) URL; Brightspace feeds are https://.")
    if not parts.hostname:
        raise ValueError(f"{label}: invalid URL (missing host).")
    try:
        _ = parts.port
    except ValueError:
        raise ValueError(f"{label}: invalid port in URL.") from None

    req = urllib.request.Request(cleaned, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_FEED_BYTES + 1)
            if len(raw) > MAX_FEED_BYTES:
                raise RuntimeError(f"{label}: feed exceeds {MAX_FEED_BYTES // (1024 * 1024)} MB limit")
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(charset, errors="replace")
            except LookupError:
                return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{label}: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            reason = "SSL certificate verification failed"
        else:
            reason = str(exc.reason)
            if parts.hostname:
                reason = re.sub(re.escape(parts.hostname), "<host>", reason, flags=re.I)
        raise RuntimeError(f"{label}: network error ({reason})") from None
    except (http.client.HTTPException, OSError) as exc:
        # Never embed InvalidURL's message, which includes the URL path and token.
        raise RuntimeError(f"{label}: network error ({type(exc).__name__})") from None


def unescape_ics_text(val: str) -> str:
    # RFC 5545 text escapes: \n, \N, \,, \;, \\
    out = []
    i = 0
    n = len(val)
    while i < n:
        ch = val[i]
        if ch == "\\" and i + 1 < n:
            nxt = val[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out).strip()


def parse_ics_datetime(raw_val: str, params: dict[str, str], target_tz: dt.tzinfo | None) -> tuple[dt.datetime, bool]:
    """Return (aware datetime in target_tz or system local time, is_all_day)."""
    val = raw_val.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(val) == 8 and val.isdigit()):
        d = dt.datetime(int(val[0:4]), int(val[4:6]), int(val[6:8]))
        # All-day dates have no zone: midnight wherever the reader is.
        return (d.replace(tzinfo=target_tz) if target_tz is not None else d.astimezone()), True

    source_tz: dt.tzinfo | None = None  # None = floating/unknown, read as display-local
    if val.endswith("Z"):
        val = val[:-1]
        source_tz = dt.timezone.utc
    elif "TZID" in params:
        try:
            source_tz = ZoneInfo(params["TZID"])
        except (ZoneInfoNotFoundError, ValueError):
            source_tz = None  # Windows-style or custom TZID; Brightspace normally emits UTC

    fmt = "%Y%m%dT%H%M%S" if len(val) >= 15 else "%Y%m%dT%H%M"
    parsed = dt.datetime.strptime(val[:15] if len(val) >= 15 else val[:13], fmt)
    if source_tz is None:
        # A naive datetime's astimezone() treats it as system local time.
        aware = parsed.replace(tzinfo=target_tz) if target_tz is not None else parsed.astimezone()
    else:
        aware = parsed.replace(tzinfo=source_tz)
    return aware.astimezone(target_tz), False


def _looks_like_html(text: str) -> bool:
    head = text.lstrip("\ufeff \t\r\n")[:512].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head


EXPIRED_HINT = (
    "Brightspace returned an HTML page instead of a feed, which usually means the feed token "
    "was regenerated or feeds were disabled. Copy a fresh URL from Brightspace."
)


def parse_ics(ics_text: str, target_tz: dt.tzinfo | None) -> list[dict]:
    if "BEGIN:VCALENDAR" not in ics_text:
        if _looks_like_html(ics_text):
            raise ValueError(EXPIRED_HINT)
        raise ValueError("Response is not a valid iCal (.ics) feed (missing BEGIN:VCALENDAR). Check BRIGHTSPACE_ICS_URL.")

    # RFC 5545 line unfolding: CRLF or LF followed by space or tab
    unfolded = re.sub(r"\r?\n[ \t]", "", ics_text)
    lines = unfolded.splitlines()

    events: list[dict] = []
    current: dict[str, tuple[str, dict[str, str]]] | None = None
    subcomponent_depth = 0

    for line in lines:
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
            subcomponent_depth = 0
            continue
        if stripped == "END:VEVENT":
            if current:
                event = _build_event(current, target_tz)
                if event:
                    events.append(event)
            current = None
            subcomponent_depth = 0
            continue
        if current is None:
            continue
        # Ignore nested sub-components such as BEGIN:VALARM ... END:VALARM so
        # their DESCRIPTION/SUMMARY properties never overwrite the parent VEVENT.
        if stripped.startswith("BEGIN:"):
            subcomponent_depth += 1
            continue
        if stripped.startswith("END:"):
            if subcomponent_depth > 0:
                subcomponent_depth -= 1
            continue
        if subcomponent_depth > 0 or ":" not in line:
            continue

        prop_part, val_part = line.split(":", 1)
        parts = prop_part.split(";")
        prop_name = parts[0].upper().strip()
        params: dict[str, str] = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper().strip()] = v.strip().strip('"')
        current[prop_name] = (val_part, params)

    events.sort(key=lambda e: e["sort_dt"])
    return events


def _build_event(raw: dict[str, tuple[str, dict[str, str]]], target_tz: dt.tzinfo | None) -> dict | None:
    dt_raw = raw.get("DUE") or raw.get("DTSTART") or raw.get("DTEND")
    if not dt_raw:
        return None
    try:
        start_dt, all_day = parse_ics_datetime(dt_raw[0], dt_raw[1], target_tz)
    except ValueError:
        return None

    end_dt = None
    if "DTEND" in raw:
        try:
            end_dt, _ = parse_ics_datetime(raw["DTEND"][0], raw["DTEND"][1], target_tz)
        except Exception:
            end_dt = None

    # In D2L Brightspace, a deadline event often has DTEND as the actual due timestamp
    effective_dt = end_dt if (end_dt and end_dt > start_dt and (end_dt - start_dt).total_seconds() <= 3600) else start_dt

    summary = unescape_ics_text(raw.get("SUMMARY", ("Untitled", {}))[0])
    location = unescape_ics_text(raw.get("LOCATION", ("", {}))[0])
    categories = unescape_ics_text(raw.get("CATEGORIES", ("", {}))[0])
    description = unescape_ics_text(raw.get("DESCRIPTION", ("", {}))[0])
    uid = unescape_ics_text(raw.get("UID", ("", {}))[0])

    course = location or categories
    return {
        "uid": uid,
        "title": summary,
        "course": course,
        "due_iso": effective_dt.isoformat(),
        "due_display": effective_dt.strftime("%a %d %b %Y") if all_day else effective_dt.strftime("%a %d %b %Y, %H:%M %Z"),
        "all_day": all_day,
        "description": strip_html(description),
        "sort_dt": effective_dt,
    }


def strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = raw_html
    # Only unescape before tag removal if the whole string was entity-encoded HTML
    # with no literal tags; otherwise unescaping first turns `x &lt; y` into a tag.
    if "<" not in text and "&lt;" in text:
        text = html.unescape(text)
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/tr)\b[^>]*>", "\n", text)
    text = re.sub(r"(?i)<\s*li\b[^>]*>", "- ", text)
    text = re.sub(r"</?[A-Za-z!/?][^>]*>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)
    return cleaned.strip()


def parse_rss_datetime(raw_date: str, target_tz: dt.tzinfo | None) -> dt.datetime:
    raw = (raw_date or "").strip()
    if not raw:
        return _EPOCH.astimezone(target_tz)
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(target_tz)
    except Exception:
        pass
    try:
        # ISO-8601 fallback for Atom feeds
        iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = dt.datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(target_tz)
    except Exception:
        return _EPOCH.astimezone(target_tz)


def parse_rss(xml_text: str, target_tz: dt.tzinfo | None) -> list[dict]:
    cleaned_xml = xml_text.lstrip("\ufeff \t\r\n")
    if _looks_like_html(cleaned_xml):
        raise ValueError(EXPIRED_HINT)
    try:
        root = ET.fromstring(cleaned_xml)
    except ET.ParseError as exc:
        raise ValueError(f"Response is not valid RSS/Atom XML ({exc}). Check BRIGHTSPACE_RSS_URLS.") from exc

    items: list[dict] = []
    # RSS 2.0
    channel = root.find("channel")
    if channel is not None:
        feed_title = (channel.findtext("title") or "").strip()
        course = re.sub(r"(?:^Announcements\s*[-:|]\s*|\s*[-:|]\s*Announcements$)", "", feed_title, flags=re.I).strip()
        for item in channel.findall("item"):
            title = strip_html(item.findtext("title") or "Untitled")
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link or title).strip()
            pub_raw = item.findtext("pubDate") or ""
            pub_dt = parse_rss_datetime(pub_raw, target_tz)
            body = strip_html(item.findtext("description") or "")
            item_cat = (item.findtext("category") or "").strip()
            items.append({
                "id": guid,
                "title": title,
                "course": item_cat or course,
                "published_iso": pub_dt.isoformat(),
                "published_display": pub_dt.strftime("%a %d %b %Y, %H:%M %Z"),
                "link": link,
                "body": body,
                "sort_dt": pub_dt,
            })
        return items

    # Atom fallback
    ns = {"a": "http://www.w3.org/2005/Atom"}
    feed_title = (root.findtext("a:title", default="", namespaces=ns) or "").strip()
    course = re.sub(r"(?:^Announcements\s*[-:|]\s*|\s*[-:|]\s*Announcements$)", "", feed_title, flags=re.I).strip()
    for entry in root.findall("a:entry", ns):
        title = strip_html(entry.findtext("a:title", default="Untitled", namespaces=ns))
        guid = (entry.findtext("a:id", default=title, namespaces=ns) or title).strip()
        links = entry.findall("a:link", ns)
        link_el = next(
            (el for el in links if el.get("rel", "alternate") in ("", "alternate")),
            links[0] if links else None,
        )
        link = link_el.get("href", "").strip() if link_el is not None else ""
        pub_raw = (
            entry.findtext("a:updated", default="", namespaces=ns)
            or entry.findtext("a:published", default="", namespaces=ns)
        )
        pub_dt = parse_rss_datetime(pub_raw, target_tz)
        body = strip_html(
            entry.findtext("a:content", default="", namespaces=ns)
            or entry.findtext("a:summary", default="", namespaces=ns)
        )
        items.append({
            "id": guid,
            "title": title,
            "course": course,
            "published_iso": pub_dt.isoformat(),
            "published_display": pub_dt.strftime("%a %d %b %Y, %H:%M %Z"),
            "link": link,
            "body": body,
            "sort_dt": pub_dt,
        })
    return items


def filter_events(
    events: list[dict],
    now: dt.datetime,
    days: int,
    from_days_ago: int = 0,
    course_query: str | None = None,
) -> list[dict]:
    start_bound = (now - dt.timedelta(days=from_days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_bound = (now + dt.timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=999999)
    q = course_query.lower().strip() if course_query else None
    result = []
    for ev in events:
        if ev["sort_dt"] < start_bound or ev["sort_dt"] > end_bound:
            continue
        if q and q not in ev["course"].lower() and q not in ev["title"].lower():
            continue
        result.append(ev)
    return result


def filter_announcements(
    items: list[dict],
    now: dt.datetime,
    days: int,
    limit: int,
    course_query: str | None = None,
) -> list[dict]:
    cutoff = now - dt.timedelta(days=days)
    q = course_query.lower().strip() if course_query else None
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in sorted(items, key=lambda x: x["sort_dt"], reverse=True):
        key = f"{item['id']}|{item['title']}|{item['published_iso']}"
        if key in seen:
            continue
        seen.add(key)
        if item["sort_dt"] < cutoff:
            continue
        if q and q not in item["course"].lower() and q not in item["title"].lower():
            continue
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def serialize_clean(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "sort_dt"} for r in rows]


def split_urls(raw: str | None) -> list[str]:
    return [u.strip() for u in re.split(r"[,;\s]+", raw or "") if u.strip()]


def _labelled(label: str, exc: Exception) -> str:
    """Format a feed error as "<label>: <reason>" whether or not the reason already names it."""
    msg = str(exc)
    return msg if msg.startswith(f"{label}:") else f"{label}: {msg}"


def collect_announcements(urls: list[str], target_tz: dt.tzinfo | None) -> tuple[list[dict], list[str]]:
    """Fetch every RSS feed; one broken course feed should not hide the others."""
    items: list[dict] = []
    errors: list[str] = []
    for i, url in enumerate(urls, 1):
        label = f"announcement feed {i}"
        try:
            items.extend(parse_rss(fetch_url(url, label), target_tz))
        except (RuntimeError, ValueError) as exc:
            errors.append(_labelled(label, exc))
    return items, errors


def format_events_text(events: list[dict], days: int, full: bool = False) -> str:
    if not events:
        return f"No upcoming Brightspace events found in the next {days} days."
    lines = [f"Upcoming Brightspace deadlines & events ({len(events)}):"]
    for ev in events:
        course_prefix = f"[{ev['course']}] " if ev["course"] else ""
        lines.append(f"- {ev['due_display']} | {course_prefix}{ev['title']}")
        if ev["description"]:
            if full:
                for ln in ev["description"].splitlines():
                    lines.append(f"    {ln}")
            else:
                snippet = ev["description"].replace("\n", " ")
                if len(snippet) > 220:
                    snippet = snippet[:217] + "..."
                lines.append(f"    {snippet}")
    return "\n".join(lines)


def format_announcements_text(items: list[dict], days: int, full: bool = False) -> str:
    if not items:
        return f"No Brightspace announcements found from the last {days} days."
    lines = [f"Recent Brightspace announcements ({len(items)}):"]
    for item in items:
        course_prefix = f"[{item['course']}] " if item["course"] else ""
        lines.append(f"- {item['published_display']} | {course_prefix}{item['title']}")
        if item["body"]:
            if full:
                for ln in item["body"].splitlines():
                    lines.append(f"    {ln}")
            else:
                snippet = item["body"].replace("\n", " ")
                if len(snippet) > 320:
                    snippet = snippet[:317] + "..."
                lines.append(f"    {snippet}")
    return "\n".join(lines)


def _check(cond: bool, msg: str) -> None:
    # Explicit check rather than `assert`, which `python -O` silently strips.
    if not cond:
        raise AssertionError(msg)


class _TestEastern(dt.tzinfo):
    """US Eastern with post-2007 DST rules, so the self-test needs no tz database."""

    _STD = dt.timedelta(hours=-5)

    def _is_dst(self, d: dt.datetime) -> bool:
        y = d.year
        start = dt.datetime(y, 3, 8 + (6 - dt.date(y, 3, 8).weekday()) % 7, 2)   # 2nd Sun Mar
        end = dt.datetime(y, 11, 1 + (6 - dt.date(y, 11, 1).weekday()) % 7, 1)  # 1st Sun Nov
        return start <= d.replace(tzinfo=None) < end

    def utcoffset(self, d):
        return self._STD + self.dst(d)

    def dst(self, d):
        return dt.timedelta(hours=1) if d is not None and self._is_dst(d) else dt.timedelta(0)

    def tzname(self, d):
        return "EDT" if d is not None and self._is_dst(d) else "EST"


def run_self_test() -> int:
    tz = _TestEastern()
    now = dt.datetime(2026, 10, 22, 12, 0, tzinfo=tz)

    # 1. RFC 5545 folded lines, text escapes, HTML stripping, VALARM ignoring, UTC -> local.
    #    2026-10-25T03:59:00Z is 2026-10-24 23:59 EDT: the classic "midnight shift".
    sample_ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:1001@example.brightspace.com\r\n"
        "DTSTART:20261025T035900Z\r\n"
        "SUMMARY:Lab Report 4 - Due\r\n"
        "LOCATION:CHEM 1010 A - General Chemistry -\r\n"
        "  Fall 2026\r\n"
        "DESCRIPTION:<p>Upload your report as a <b>single PDF</b>\\, and\r\n"
        "  include raw data where x &lt; y and y &gt; 0.</p>\r\n"
        "BEGIN:VALARM\r\n"
        "ACTION:DISPLAY\r\n"
        "DESCRIPTION:Reminder\r\n"
        "TRIGGER:-PT30M\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    events = parse_ics(sample_ics, tz)
    _check(len(events) == 1, f"Expected 1 event, got {len(events)}")
    ev = events[0]
    _check(ev["course"] == "CHEM 1010 A - General Chemistry - Fall 2026", f"Unfolding failed: {ev['course']!r}")
    _check(ev["due_display"] == "Sat 24 Oct 2026, 23:59 EDT", f"TZ conversion failed: {ev['due_display']!r}")
    _check("single PDF, and include raw data where x < y and y > 0." in ev["description"],
           f"Escapes/HTML/VALARM failed: {ev['description']!r}")
    _check(len(filter_events(events, now=now, days=7, course_query="chem")) == 1, "Course filter failed")
    _check(len(filter_events(events, now=now, days=2)) == 1, "End-of-day window failed")
    _check(len(filter_events(events, now=now, days=1)) == 0, "Day window failed")

    # 2. A deadline across the DST boundary must keep its wall-clock hour.
    #    2026-11-03T04:59:00Z is 23:59 EST on 2 Nov (US DST ends 1 Nov 2026).
    dst_dt, _ = parse_ics_datetime("20261103T045900Z", {}, tz)
    _check(dst_dt.strftime("%d %H:%M %Z") == "02 23:59 EST", f"DST handling failed: {dst_dt!r}")

    # 3. RSS 2.0 parsing, course name from channel title, HTML stripping.
    sample_rss = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0">
      <channel>
        <title>Announcements - CHEM 1010 A - General Chemistry</title>
        <item>
          <guid>news-42</guid>
          <title>Quiz 3 moved to Monday</title>
          <pubDate>Thu, 22 Oct 2026 14:30:00 GMT</pubDate>
          <description>&lt;p&gt;Hello all,&lt;/p&gt;&lt;p&gt;Quiz 3 is now on &lt;strong&gt;Monday 26 October&lt;/strong&gt; (for sections where n &amp;lt; 5 &amp;amp; m &amp;gt; 1).&lt;/p&gt;</description>
        </item>
      </channel>
    </rss>
    """
    rss_items = parse_rss(sample_rss, tz)
    _check(len(rss_items) == 1, f"Expected 1 RSS item, got {len(rss_items)}")
    it = rss_items[0]
    _check(it["course"] == "CHEM 1010 A - General Chemistry", f"Unexpected course: {it['course']!r}")
    _check(it["published_display"] == "Thu 22 Oct 2026, 10:30 EDT", f"Unexpected pub date: {it['published_display']!r}")
    _check(it["body"] == "Hello all,\nQuiz 3 is now on Monday 26 October (for sections where n < 5 & m > 1).",
           f"Unexpected body: {it['body']!r}")

    # 4. An expired token returns a login page; the error must say so.
    login_page = "<!DOCTYPE html><html><head><title>Login</title></head><body></body></html>"
    for parser_fn in (parse_ics, parse_rss):
        try:
            parser_fn(login_page, tz)
        except ValueError as exc:
            _check("token" in str(exc), f"{parser_fn.__name__} gave an unhelpful error: {exc}")
        else:
            raise AssertionError(f"{parser_fn.__name__} accepted an HTML login page")

    # 5. Feed URLs (tokens included) never appear in error messages, and can't
    #    be turned into local file reads.
    for bad in (
        "file:///etc/hostname?token=SECRET",
        "ftp://example.com/x?token=SECRET",
        "http://127.0.0.1:9/feed.ics?token=SECRET",  # port 9: refused locally, no network
        "https://example.com/feed.ics?token=SECRET extra",
        "https://example.com/feed.ics?token=SECRET\nmore",
        "https://example.com:badport/feed.ics?token=SECRET",
        "https:///feed.ics?token=SECRET",
    ):
        try:
            fetch_url(bad, "test feed", timeout=2)
        except (ValueError, RuntimeError) as exc:
            _check("SECRET" not in str(exc) and "/feed" not in str(exc) and "hostname" not in str(exc),
                   f"fetch_url leaked its URL: {exc}")
            _check(str(exc).startswith("test feed"), f"fetch_url error lacks its label: {exc}")
        else:
            raise AssertionError(f"fetch_url accepted {bad}")

    # 6. Override parsing that needs no tz database.
    _check(resolve_tz("UTC") is dt.timezone.utc, "UTC override failed")
    _check(resolve_tz("-05:00") == dt.timezone(dt.timedelta(hours=-5)), "Offset override failed")
    _check(resolve_tz("UTC+5:30") == dt.timezone(dt.timedelta(hours=5, minutes=30)), "UTC+h:mm override failed")
    _check(resolve_tz("+530") == dt.timezone(dt.timedelta(hours=5, minutes=30)), "+hmm override failed")

    # 7. The default path (target_tz=None) uses the OS clock rules for UTC,
    #    floating, and all-day timestamps without needing a timezone database.
    utc_ref = dt.datetime(2026, 10, 25, 3, 59, tzinfo=dt.timezone.utc).astimezone()
    sys_utc, sys_all_day = parse_ics_datetime("20261025T035900Z", {}, None)
    _check(not sys_all_day and sys_utc == utc_ref and sys_utc.tzinfo is not None,
           f"System-local UTC conversion failed: {sys_utc!r}")
    sys_float, _ = parse_ics_datetime("20261025T235900", {}, None)
    _check(sys_float.tzinfo is not None and (sys_float.hour, sys_float.minute) == (23, 59),
           f"System-local floating time failed: {sys_float!r}")
    sys_date, is_ad = parse_ics_datetime("20261025", {"VALUE": "DATE"}, None)
    _check(is_ad and sys_date.tzinfo is not None and (sys_date.year, sys_date.month, sys_date.day) == (2026, 10, 25),
           f"System-local all-day date failed: {sys_date!r}")
    _check(local_now(None).tzinfo is not None, "local_now(None) did not return an aware datetime")

    print("OK: brightspace.py self-test passed (iCal unfolding, UTC->local, DST, system-local tz, "
          "HTML stripping, RSS 2.0, expired-token detection, tz overrides).")
    return 0


def _missing(var: str, how: str) -> int:
    print(
        f"Error: {var} is not set in this process's environment. Store it in your Hermes .env and load "
        f"the skill (skill_view) so Hermes passes it through. To get the URL: {how}",
        file=sys.stderr,
    )
    return 2


ICS_HOW = (
    "In Brightspace: Calendar -> Settings (gear) -> Enable Calendar Feeds -> Save -> "
    "Subscribe -> All Calendars and Tasks -> copy the link."
)
RSS_HOW = (
    "In each course: Announcements widget -> dropdown arrow -> RSS -> copy the URL of the "
    "page that opens. Comma-separate multiple courses."
)


def _load_ics(args: argparse.Namespace, target_tz: dt.tzinfo | None) -> list[dict]:
    if getattr(args, "ics_file", None):
        return parse_ics(pathlib.Path(args.ics_file).read_text(encoding="utf-8", errors="replace"), target_tz)
    url = (args.ics_url or os.environ.get("BRIGHTSPACE_ICS_URL") or "").strip()
    return parse_ics(fetch_url(url, "calendar feed"), target_tz)


def _load_rss(args: argparse.Namespace, urls: list[str], target_tz: dt.tzinfo | None) -> tuple[list[dict], list[str]]:
    if getattr(args, "rss_file", None):
        xml_text = pathlib.Path(args.rss_file).read_text(encoding="utf-8", errors="replace")
        return parse_rss(xml_text, target_tz), []
    return collect_announcements(urls, target_tz)


def _rss_feed_course(xml_text: str) -> list[str]:
    """Course name from an RSS/Atom feed's own title, for feeds with no items yet."""
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff \t\r\n"))
    except ET.ParseError:
        return []
    channel = root.find("channel")
    title = (
        channel.findtext("title") if channel is not None
        else root.findtext("{http://www.w3.org/2005/Atom}title")
    ) or ""
    name = re.sub(r"(?:^Announcements\s*[-:|]\s*|\s*[-:|]\s*Announcements$)", "", title.strip(), flags=re.I).strip()
    return [name] if name else []


def run_feeds_report(target_tz: dt.tzinfo | None) -> int:
    """Summarise feed coverage by course name. Never prints a URL or token.

    Lets an agent check "is this course's announcement feed configured?"
    without reading the environment variables, which hold the secret tokens.
    """
    ics_url = os.environ.get("BRIGHTSPACE_ICS_URL", "").strip()
    rss_urls = split_urls(os.environ.get("BRIGHTSPACE_RSS_URLS"))
    if not ics_url and not rss_urls:
        print("Error: neither BRIGHTSPACE_ICS_URL nor BRIGHTSPACE_RSS_URLS is set.", file=sys.stderr)
        return 2

    print("Calendar feed:")
    if not ics_url:
        print("  not configured")
    else:
        try:
            events = parse_ics(fetch_url(ics_url, "calendar feed"), target_tz)
            courses = sorted({e["course"] for e in events if e["course"]})
            print(f"  ok, {len(events)} events across {len(courses)} named courses")
            for c in courses:
                print(f"    - {c}")
        except (RuntimeError, ValueError) as exc:
            print(f"  FAILED: {_labelled('calendar feed', exc)}")

    print(f"Announcement feeds ({len(rss_urls)} configured):")
    if not rss_urls:
        print("  none configured")
    for i, url in enumerate(rss_urls, 1):
        label = f"announcement feed {i}"
        try:
            xml_text = fetch_url(url, label)
            items = parse_rss(xml_text, target_tz)
            courses = sorted({it["course"] for it in items if it["course"]}) or _rss_feed_course(xml_text)
            name = ", ".join(courses) if courses else "(course name not given)"
            latest = max((it["sort_dt"] for it in items), default=None)
            when = f", latest {latest.strftime('%d %b %Y')}" if latest else ", no announcements yet"
            print(f"  {label}: {name}, {len(items)} items{when}")
        except (RuntimeError, ValueError) as exc:
            print(f"  FAILED: {_labelled(label, exc)}")
    return 0


def _report_feed_errors(errors: list[str], total: int) -> None:
    for err in errors:
        print(f"Warning: skipped {err}", file=sys.stderr)
    if errors and len(errors) < total:
        print(f"Warning: {len(errors)} of {total} announcement feeds failed; results are partial.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Brightspace Calendar (.ics) and Announcements (RSS) feeds.")
    parser.add_argument("--self-test", action="store_true", help="Run offline parser checks and exit.")
    parser.add_argument("--tz", default=None, help="Override display timezone: UTC, an offset like -05:00, or an IANA name like Europe/London (default: $BRIGHTSPACE_TZ, else system local time).")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--course", default=None, help="Case-insensitive course or title substring filter.")
    common.add_argument("--full", action="store_true", help="Show full untruncated text.")
    common.add_argument("--json", action="store_true", help="Output JSON.")
    # Also accept --tz after the subcommand. SUPPRESS keeps the subparser from
    # overwriting a top-level --tz with its own default.
    common.add_argument("--tz", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    ics_src = argparse.ArgumentParser(add_help=False)
    ics_src.add_argument("--ics-url", default=None, help="Override BRIGHTSPACE_ICS_URL.")
    ics_src.add_argument("--ics-file", default=None, help="Read a local .ics file instead of fetching.")

    rss_src = argparse.ArgumentParser(add_help=False)
    rss_src.add_argument("--rss-urls", default=None, help="Override BRIGHTSPACE_RSS_URLS (comma-separated).")
    rss_src.add_argument("--rss-file", default=None, help="Read a local RSS/Atom file instead of fetching.")

    sub = parser.add_subparsers(dest="cmd")

    p_due = sub.add_parser("due", parents=[common, ics_src], help="List upcoming assignments and calendar events.")
    p_due.add_argument("--days", type=int, default=14, help="Days ahead to include (default: 14).")
    p_due.add_argument("--from-days-ago", type=int, default=0, help="Also include items from N days ago (default: 0 = today).")

    p_ann = sub.add_parser("announcements", parents=[common, rss_src], help="List recent course announcements.")
    p_ann.add_argument("--days", type=int, default=14, help="Days back to include (default: 14).")
    p_ann.add_argument("--limit", type=int, default=10, help="Max announcements to return (default: 10).")

    p_brief = sub.add_parser("briefing", parents=[common, ics_src, rss_src], help="Upcoming deadlines plus recent announcements.")
    p_brief.add_argument("--days", type=int, default=7, help="Window in days, both directions (default: 7).")
    p_brief.add_argument("--limit", type=int, default=10, help="Max announcements (default: 10).")

    p_feeds = sub.add_parser(
        "feeds",
        help="List which courses the configured feeds cover, without printing any URL.",
    )
    p_feeds.add_argument("--tz", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.self_test:
        try:
            return run_self_test()
        except AssertionError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1

    if not args.cmd:
        parser.print_help()
        return 2

    target_tz = resolve_tz(getattr(args, "tz", None))
    now = local_now(target_tz)

    if args.cmd == "feeds":
        return run_feeds_report(target_tz)

    try:
        if args.cmd == "due":
            if not (args.ics_file or args.ics_url or os.environ.get("BRIGHTSPACE_ICS_URL", "").strip()):
                return _missing("BRIGHTSPACE_ICS_URL", ICS_HOW)
            events = filter_events(
                _load_ics(args, target_tz),
                now=now,
                days=args.days,
                from_days_ago=args.from_days_ago,
                course_query=args.course,
            )
            if args.json:
                print(json.dumps(serialize_clean(events), indent=2))
            else:
                print(format_events_text(events, args.days, full=args.full))
            return 0

        if args.cmd == "announcements":
            urls = split_urls(args.rss_urls or os.environ.get("BRIGHTSPACE_RSS_URLS"))
            if not (args.rss_file or urls):
                return _missing("BRIGHTSPACE_RSS_URLS", RSS_HOW)
            all_items, errors = _load_rss(args, urls, target_tz)
            _report_feed_errors(errors, len(urls))
            if errors and len(errors) == len(urls):
                print("Error: every announcement feed failed.", file=sys.stderr)
                return 1
            items = filter_announcements(all_items, now=now, days=args.days, limit=args.limit, course_query=args.course)
            if args.json:
                print(json.dumps(serialize_clean(items), indent=2))
            else:
                print(format_announcements_text(items, args.days, full=args.full))
            return 0

        if args.cmd == "briefing":
            have_ics = bool(args.ics_file or args.ics_url or os.environ.get("BRIGHTSPACE_ICS_URL", "").strip())
            urls = split_urls(args.rss_urls or os.environ.get("BRIGHTSPACE_RSS_URLS"))
            have_rss = bool(args.rss_file or urls)
            if not (have_ics or have_rss):
                print("Error: neither BRIGHTSPACE_ICS_URL nor BRIGHTSPACE_RSS_URLS is set.", file=sys.stderr)
                print(f"  Calendar: {ICS_HOW}", file=sys.stderr)
                print(f"  Announcements: {RSS_HOW}", file=sys.stderr)
                return 2

            events: list[dict] = []
            ics_failed = False
            if have_ics:
                try:
                    events = filter_events(_load_ics(args, target_tz), now=now, days=args.days, course_query=args.course)
                except (RuntimeError, ValueError, OSError) as exc:
                    ics_failed = True
                    print(f"Warning: skipped {_labelled('calendar feed', exc)}", file=sys.stderr)

            announcements: list[dict] = []
            rss_all_failed = False
            if have_rss:
                try:
                    raw_items, errors = _load_rss(args, urls, target_tz)
                except (RuntimeError, ValueError, OSError) as exc:
                    raw_items, errors = [], [_labelled("announcement feed", exc)]
                total_rss = len(urls) or 1
                _report_feed_errors(errors, total_rss)
                if len(errors) == total_rss:
                    rss_all_failed = True
                    print("Error: every announcement feed failed.", file=sys.stderr)
                else:
                    announcements = filter_announcements(
                        raw_items, now=now, days=args.days, limit=args.limit, course_query=args.course
                    )

            if (not have_ics or ics_failed) and (not have_rss or rss_all_failed):
                return 1

            if args.json:
                print(json.dumps({
                    "due": serialize_clean(events),
                    "announcements": serialize_clean(announcements),
                }, indent=2))
            else:
                sections = []
                if have_ics and not ics_failed:
                    sections.append(format_events_text(events, args.days, full=args.full))
                if have_rss and not rss_all_failed:
                    sections.append(format_announcements_text(announcements, args.days, full=args.full))
                if sections:
                    print("\n\n".join(sections))
            return 1 if (ics_failed or rss_all_failed) else 0

    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
