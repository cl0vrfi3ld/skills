---
name: brightspace
description: "Read D2L Brightspace deadlines and course announcements."
version: 0.2.0
author: Clover Levens (cl0vrfi3ld), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Productivity, Education, Brightspace, D2L, LMS, Assignments, Deadlines, Announcements, Calendar, iCal, RSS]
    related_skills: [weekly-review-planning, google-workspace, apple-reminders, notion, obsidian]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: BRIGHTSPACE_ICS_URL
    prompt: "Brightspace Calendar feed URL (.ics)"
    help: "Brightspace Calendar -> Settings (gear) -> Enable Calendar Feeds -> Save -> Subscribe (star) -> All Calendars and Tasks -> copy the link"
    required_for: "upcoming assignments, quizzes, and course events"
  - name: BRIGHTSPACE_RSS_URLS
    prompt: "Brightspace course Announcements RSS URLs, comma-separated"
    help: "In each course: Announcements widget -> dropdown arrow -> RSS -> copy the URL of the page that opens"
    required_for: "recent course announcements"
    optional: true
  - name: BRIGHTSPACE_TZ
    prompt: "Display timezone override, e.g. America/Chicago, UTC, or -05:00"
    help: "Only needed when Hermes runs on a machine whose clock isn't set to your timezone, such as a UTC server or container"
    required_for: "correct deadline times on UTC hosts"
    optional: true
---

# Brightspace

Read upcoming deadlines and recent course announcements from D2L Brightspace, using the personal Calendar (`.ics`) and Announcements (RSS) feeds that Brightspace gives every student. No browser automation, login cookies, or institutional API access needed. The bundled script is standard-library Python and never writes to Brightspace.

The calendar feed is the core of the skill. Announcement feeds are optional and set up per course.

## When to Use

- The user asks what's due, what's coming up this week, or when a specific assignment or quiz is.
- The user asks what their instructors have announced recently.
- Another workflow needs deadlines as input: weekly planning (`weekly-review-planning`), calendar events (`google-workspace`), reminders (`apple-reminders`), or notes (`notion`, `obsidian`). Use `--json` to get machine-readable output for handoff.
- Don't use for: submitting work, grades, course files, or discussions. This skill has no write access to Brightspace.

## Prerequisites

Python 3.9 or newer. Times are converted using the operating system's own clock rules.

Feed URLs are set in the Hermes `.env` file (`$HERMES_HOME/.env`, or `.env` inside `~/.hermes/`):

```dotenv
BRIGHTSPACE_ICS_URL="https://<your-brightspace-host>/d2l/le/calendar/feed/user/feed.ics?token=..."
BRIGHTSPACE_RSS_URLS="https://<your-brightspace-host>/d2l/le/news/rss/...,https://<your-brightspace-host>/d2l/le/news/rss/..."
# Only if Hermes runs somewhere not set to your timezone, such as a UTC server:
# BRIGHTSPACE_TZ="America/Chicago"
```

`<your-brightspace-host>` is the address you log in at: often `<school>.brightspace.com`, sometimes a custom domain such as `learn.<school>.edu`.

### Finding the feed URLs

Menu labels vary between institutions and Brightspace versions. These steps match the default layout.

- **Calendar (`BRIGHTSPACE_ICS_URL`):** open **Calendar** from the homepage navbar or Calendar widget. Click **Settings** (gear icon), enable **Calendar Feeds**, and **Save**. Click **Subscribe** (star icon), choose **All Calendars and Tasks** from the dropdown at the top, then copy the link. Choosing a single course gives a feed for that course only.
- **Announcements (`BRIGHTSPACE_RSS_URLS`):** each course has its own feed; the homepage Announcements widget carries only institution-wide notices. Inside a course, click the dropdown arrow next to the **Announcements** widget title, click **RSS**, and copy the address of the page that opens. Right-clicking the menu item won't give a usable link, because D2L menus run on JavaScript. Separate multiple course URLs with commas.

**Treat these URLs like passwords.** The `token=` part grants read access without logging in. Keep them in `.env` and out of chat, screenshots, and bug reports. The script never prints them: errors refer to feeds by name ("calendar feed", "announcement feed 2").

## How to Run

Always load the skill with `skill_view(name="brightspace")` first. Loading is what passes the `BRIGHTSPACE_*` variables into `terminal` and `execute_code`. The script reads configuration only from its environment and command-line flags, never from `.env`, so without the load it sees nothing.

To get a week-long briefing, run via `terminal`:

```bash
python3 ${HERMES_SKILL_DIR}/scripts/brightspace.py briefing --days 7
```

If `${HERMES_SKILL_DIR}` shows up literally (substitution is off via `skills.template_vars: false`), use `skill_dir` from the `skill_view` response. On Windows, use `python` or `py` instead of `python3`.

## Quick Reference

`brightspace.py` below is short for `python3 ${HERMES_SKILL_DIR}/scripts/brightspace.py`.

```bash
brightspace.py due                              # deadlines in the next 14 days
brightspace.py due --days 30 --course CHEM      # filter by course or title substring
brightspace.py due --from-days-ago 3            # also show the last 3 days
brightspace.py announcements --days 14 --limit 10
brightspace.py briefing --days 7                # deadlines ahead + announcements behind
brightspace.py briefing --full                  # untruncated descriptions
brightspace.py due --json                       # machine-readable output
brightspace.py due --tz Europe/London           # timezone override (also UTC, -05:00)
brightspace.py due --ics-file export.ics        # parse a local file, no network (also --rss-file)
brightspace.py feeds                            # which courses each feed covers; prints no URLs
brightspace.py --self-test                      # offline checks
```

### JSON Output (`--json`)

`due --json` and `announcements --json` return a JSON list of objects. `briefing --json` wraps both lists as `{"due": [...], "announcements": [...]}`.

| Feed | Field | Type | Meaning |
|---|---|---|---|
| `due` | `uid` | string | iCal `UID` of the event (stable identifier for deduplication). |
| `due` | `title` | string | Event or assignment title (`SUMMARY`), e.g. `"Lab Report 4 - Due"`. |
| `due` | `course` | string | Course name from `LOCATION` (or `CATEGORIES`), `""` if the feed omits both. |
| `due` | `due_iso` | string | ISO-8601 timestamp converted to the display timezone. |
| `due` | `due_display` | string | Formatted local time (`"Sat 24 Oct 2026, 23:59 EDT"`, or date-only if `all_day`). |
| `due` | `all_day` | boolean | `true` for date-only events (`VALUE=DATE`), `false` for timed deadlines. |
| `due` | `description` | string | Plain-text description with HTML stripped. |
| `announcements` | `id` | string | RSS `guid` or Atom `id` (falls back to `link`, then `title`). |
| `announcements` | `title` | string | Announcement headline with HTML stripped. |
| `announcements` | `course` | string | Course name from item `category` or the feed's channel title. |
| `announcements` | `published_iso` | string | ISO-8601 publication timestamp in the display timezone. |
| `announcements` | `published_display` | string | Formatted local publication time (`"Thu 22 Oct 2026, 10:30 EDT"`). |
| `announcements` | `link` | string | Link to the announcement page in Brightspace (contains no feed token). |
| `announcements` | `body` | string | Plain-text announcement body with HTML stripped. |

Exit codes: `0` success, `1` fetch or parse failure, `2` missing configuration.

## Procedure

1. Choose the subcommand: `due` for deadlines, `announcements` for news, `briefing` for a general "what's coming up" (default: `briefing --days 7`). Done when it exits `0`.
2. On exit `2`, load the skill with `skill_view` if you haven't this session, then rerun. If it still exits `2`, the variable isn't set: tell the user which one and give them the matching steps from "Finding the feed URLs".
3. On exit `1`, or `Warning: skipped` lines on stderr, match the message against Pitfalls. Errors name feeds as "calendar feed" or "announcement feed N" (in `BRIGHTSPACE_RSS_URLS` order), never by URL. A partial announcements result is still usable: report what came back and which feed failed.
4. Present items nearest first, each with its course. Times are already converted to local time; if the zone shown is `UTC` and the user isn't in UTC, rerun with `--tz <their zone>` and suggest setting `BRIGHTSPACE_TZ`.

The `BRIGHTSPACE_*` values contain access tokens. Never print, echo, log, or inspect them (never echo the variables or dump the environment), never ask for them in chat, and never read or edit `.env`. To diagnose configuration, use the `feeds` subcommand and the script's error messages, neither of which contains any URL.

## Pitfalls

- **HTML page instead of a feed, or HTTP 401/403:** the feed token was regenerated or Calendar Feeds were switched off. The user needs to copy a fresh URL into `.env`.
- **Deadline shown on the wrong day:** Brightspace stores every time in UTC, so a deadline of 11:59 PM on Friday in New York is saved as 03:59 on Saturday. The script converts to local time; always report its `due_display` / `due_iso`, never the raw times in the feed. On a server or container whose clock is set to UTC, that conversion has nothing to correct, so set `BRIGHTSPACE_TZ`. Hermes's own `timezone` setting doesn't reach `terminal`.
- **"timezone not found" warning:** zone names like `Europe/London` need a timezone database, which Windows Python and slim containers often lack. The script falls back to system local time. Fix it with a fixed offset such as `-05:00` (ignores daylight saving), by installing the `tzdata` Python package, or by removing the override on a machine already set to the right zone.
- **A course's announcements are missing:** each course needs its own RSS feed. Run `brightspace.py feeds` to see which courses the configured feeds cover, and compare that against the courses in the calendar feed. Ask the user to add a feed for any course that's missing.
- **New term:** the calendar feed picks up new courses automatically; announcement feeds don't. Use `feeds` to spot old and missing courses, then ask the user to update their feed list.
- **Not every event is a deadline:** the calendar includes everything on it, such as class meetings, office hours, and availability windows. D2L usually marks real deadlines and windows with title suffixes such as `- Due`, `- Availability Ends`, `- Availability Begins`, or `- Start`; judge by title rather than treating every row as due work.
- **Blank course names:** the course comes from the event's `LOCATION`, falling back to `CATEGORIES`. Some institutions leave both empty, so `--course` can only match titles.

## Verification

Run the offline self-test. It needs no feed URLs, network, or timezone database:

```bash
python3 ${HERMES_SKILL_DIR}/scripts/brightspace.py --self-test
```

Expect `OK: brightspace.py self-test passed (...)` and exit `0`. Then run `due --days 30` against the real feed and check that a deadline you know appears at the same time Brightspace shows.
