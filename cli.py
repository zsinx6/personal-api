#!/usr/bin/env python3
"""Zero-dependency CLI bridge for personal-api (Calendar, Trello, and SilverBullet Notes)."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TZ_SP = timezone(timedelta(hours=-3))
ENV_PATH = Path(os.environ.get("PERSONAL_API_ENV", "/opt/data/personal-api/.env"))
NOTES_DIR = Path(os.environ.get("NOTES_DIR", "/opt/data/notes"))

TASK_OPEN_RE = re.compile(r"^(\s*-\s*)\[\s\](\s+.*)$")
TASK_ANY_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s+(.*)$")
LEADING_CHECKBOX_RE = re.compile(r"^(?:[-*]\s*\[[ xX]\]\s*|[-*]\s+)")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
DATE_PREFIX_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")
ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def api_request(
    method: str,
    endpoint: str,
    params: dict[str, str | int] | None = None,
    payload: dict[str, object] | None = None,
) -> object:
    cfg = load_env(ENV_PATH)
    base_url = (
        os.environ.get("API_URL") or cfg.get("API_URL")
    ).rstrip("/")
    token = (
        os.environ.get("API_TOKEN")
        or cfg.get("API_TOKEN")
        or cfg.get("PERSONAL_API_TOKEN")
        or cfg.get("BRIDGE_TOKEN")
        or ""
    ).strip()

    url = f"{base_url}{endpoint}"
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None}
        if clean_params:
            url = f"{url}?{urllib.parse.urlencode(clean_params)}"

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} on {endpoint}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection error on {endpoint}: {exc.reason}") from None


def _note_date(path: Path) -> str | None:
    stem = path.stem
    m_iso = ISO_DATE_PREFIX_RE.match(stem)
    if m_iso:
        return m_iso.group(1)
    m_compact = DATE_PREFIX_RE.match(stem)
    if m_compact:
        return f"{m_compact.group(1)}-{m_compact.group(2)}-{m_compact.group(3)}"
    return None


def _iter_markdown_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for p in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        files.append(p)
    return files


def scan_open_tasks(root: Path, today_str: str) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for md_file in _iter_markdown_files(root):
        rel = str(md_file.relative_to(root))
        file_date = _note_date(md_file)
        current_section = "Uncategorized"
        for idx, line in enumerate(md_file.read_text(encoding="utf-8").splitlines(), start=1):
            h = HEADING_RE.match(line)
            if h:
                current_section = h.group(2)
                continue
            m = TASK_OPEN_RE.match(line)
            if m:
                text = m.group(2).strip()
                overdue = bool(file_date and file_date < today_str)
                tasks.append(
                    {
                        "file": rel,
                        "line": idx,
                        "section": current_section,
                        "note_date": file_date,
                        "overdue": overdue,
                        "text": text,
                    }
                )
    return tasks


def _resolve_target_note(root: Path, now: datetime) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    today_compact = now.strftime("%Y%m%d")
    today_iso = now.strftime("%Y-%m-%d")
    candidates = _iter_markdown_files(root)

    today_matches = [
        p for p in candidates if p.stem.startswith(today_compact) or p.stem.startswith(today_iso)
    ]
    if today_matches:
        return sorted(today_matches)[-1]

    new_note = root / f"{now.strftime('%Y%m%d%H%M')}.md"
    new_note.write_text("# Daily Notes\n\n## Work\n\n## APyB\n\n## Personal\n", encoding="utf-8")
    return new_note


def _is_section_boundary(line: str, section_level: int) -> bool:
    if line.strip() == "---":
        return True
    h = HEADING_RE.match(line)
    if h and len(h.group(1)) <= section_level:
        return True
    return False


def _matches_section(heading_text: str, target_lower: str) -> bool:
    clean = heading_text.strip().lower()
    return clean == target_lower or clean.startswith(f"{target_lower} ") or clean.startswith(f"{target_lower}(")


def cmd_audit(args: argparse.Namespace) -> None:
    now = datetime.now(TZ_SP)
    today_str = now.strftime("%Y-%m-%d")

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_trello = pool.submit(
            api_request,
            "GET",
            "/trello/cards",
            {"member": args.member} if args.member else None,
        )
        fut_cal = pool.submit(
            api_request,
            "GET",
            "/calendar/events",
            {"days_behind": args.behind, "days_ahead": args.days},
        )
        open_notes = scan_open_tasks(NOTES_DIR, today_str)
        trello_cards = fut_trello.result()
        cal_events = fut_cal.result()

    output = {
        "now": now.isoformat(timespec="seconds"),
        "today": today_str,
        "notes_open": open_notes,
        "trello_cards": trello_cards,
        "calendar_events": cal_events,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_notes_add(args: argparse.Namespace) -> None:
    now = datetime.now(TZ_SP)
    task_text = LEADING_CHECKBOX_RE.sub("", args.text.strip()).strip()
    if not task_text:
        raise RuntimeError("Task text cannot be empty")

    existing_open = scan_open_tasks(NOTES_DIR, now.strftime("%Y-%m-%d"))
    if any(str(t["text"]).lower() == task_text.lower() for t in existing_open):
        print(json.dumps({"status": "skipped_duplicate", "text": task_text}, ensure_ascii=False))
        return

    target = Path(args.file) if args.file else _resolve_target_note(NOTES_DIR, now)
    if not target.is_absolute():
        target = NOTES_DIR / target

    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []

    for line in lines:
        m_any = TASK_ANY_RE.match(line)
        if m_any and m_any.group(1).strip().lower() == task_text.lower():
            print(json.dumps({"status": "skipped_duplicate_in_note", "text": task_text}, ensure_ascii=False))
            return

    new_line = f"- [ ] {task_text}"

    if args.section:
        section_lower = args.section.strip().lower()
        insert_idx: int | None = None
        for idx, line in enumerate(lines):
            h = HEADING_RE.match(line)
            if h and len(h.group(1)) >= 2 and _matches_section(h.group(2), section_lower):
                sec_level = len(h.group(1))
                insert_idx = idx + 1
                while insert_idx < len(lines) and not _is_section_boundary(lines[insert_idx], sec_level):
                    insert_idx += 1
                while insert_idx > idx + 1 and lines[insert_idx - 1].strip() == "":
                    insert_idx -= 1
                break
        if insert_idx is not None:
            lines.insert(insert_idx, new_line)
        else:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.extend([f"## {args.section.strip()}", new_line])
    else:
        if lines and lines[-1].strip() == "---":
            lines.insert(len(lines) - 1, new_line)
        else:
            lines.append(new_line)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "added",
                "file": str(target.relative_to(NOTES_DIR)) if target.is_relative_to(NOTES_DIR) else str(target),
                "section": args.section or "End of file",
                "line": new_line,
            },
            ensure_ascii=False,
        )
    )


def cmd_notes_done(args: argparse.Namespace) -> None:
    needle = LEADING_CHECKBOX_RE.sub("", args.match.strip()).strip().lower()
    matches: list[tuple[Path, int, str]] = []

    for md_file in _iter_markdown_files(NOTES_DIR):
        file_lines = md_file.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(file_lines):
            m = TASK_OPEN_RE.match(line)
            if m and needle in m.group(2).lower():
                matches.append((md_file, idx, line))

    if not matches:
        raise RuntimeError(f"No open task matched: {args.match!r}")
    if len(matches) > 1:
        preview = [f"{p.name}:{i + 1}: {ln.strip()}" for p, i, ln in matches]
        raise RuntimeError(f"Multiple tasks matched {args.match!r}; be more specific:\n" + "\n".join(preview))

    md_file, line_idx, old_line = matches[0]
    file_lines = md_file.read_text(encoding="utf-8").splitlines()
    updated_line = TASK_OPEN_RE.sub(r"\1[x]\2", old_line, count=1)
    file_lines[line_idx] = updated_line
    md_file.write_text("\n".join(file_lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "completed",
                "file": str(md_file.relative_to(NOTES_DIR)),
                "line_number": line_idx + 1,
                "updated": updated_line.strip(),
            },
            ensure_ascii=False,
        )
    )


def cmd_trello(args: argparse.Namespace) -> None:
    res = api_request("GET", "/trello/cards", {"member": args.member} if args.member else None)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_cal_list(args: argparse.Namespace) -> None:
    res = api_request(
        "GET",
        "/calendar/events",
        {"days_behind": args.behind, "days_ahead": args.days},
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_cal_create(args: argparse.Namespace) -> None:
    end = args.end
    if not end:
        if len(args.start) == 10:
            dt = datetime.strptime(args.start, "%Y-%m-%d") + timedelta(days=1)
            end = dt.strftime("%Y-%m-%d")
        else:
            dt = datetime.fromisoformat(args.start) + timedelta(hours=1)
            end = dt.isoformat()

    payload = {
        "summary": args.summary,
        "start": args.start,
        "end": end,
        "description": args.description or "",
        "location": args.location or "",
    }
    res = api_request("POST", "/calendar/events", payload=payload)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_cal_update(args: argparse.Namespace) -> None:
    payload: dict[str, object] = {}
    if args.summary is not None:
        payload["summary"] = args.summary
    if args.start is not None:
        payload["start"] = args.start
    if args.end is not None:
        payload["end"] = args.end
    if args.description is not None:
        payload["description"] = args.description
    if args.location is not None:
        payload["location"] = args.location

    res = api_request("PATCH", f"/calendar/events/{args.id}", payload=payload)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_cal_delete(args: argparse.Namespace) -> None:
    res = api_request("DELETE", f"/calendar/events/{args.id}")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal Bridge CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # audit
    p_audit = sub.add_parser("audit", help="Single-call state dump of notes, Trello, and Calendar")
    p_audit.add_argument("--member", default="zsinx6", help="Trello username filter (default: zsinx6)")
    p_audit.add_argument("--behind", type=int, default=7, help="Calendar days behind (default: 7)")
    p_audit.add_argument("--days", type=int, default=14, help="Calendar days ahead (default: 14)")
    p_audit.set_defaults(func=cmd_audit)

    # notes
    p_notes = sub.add_parser("notes", help="Manage SilverBullet Markdown tasks")
    notes_sub = p_notes.add_subparsers(dest="notes_cmd", required=True)

    p_n_add = notes_sub.add_parser("add", help="Add an open task (- [ ]) if not already present")
    p_n_add.add_argument("text", help="Task description")
    p_n_add.add_argument("--section", help="Target Markdown heading (e.g. Work, APyB, Personal)")
    p_n_add.add_argument("--file", help="Specific note filename relative to /opt/data/notes")
    p_n_add.set_defaults(func=cmd_notes_add)

    p_n_done = notes_sub.add_parser("done", help="Mark an open task (- [ ]) as completed (- [x])")
    p_n_done.add_argument("match", help="Unique substring matching the task text")
    p_n_done.set_defaults(func=cmd_notes_done)

    # trello
    p_trello = sub.add_parser("trello", help="List open Trello cards")
    p_trello.add_argument("--member", default=None, help="Filter by Trello username")
    p_trello.set_defaults(func=cmd_trello)

    # cal
    p_cal = sub.add_parser("cal", help="Google Calendar operations")
    cal_sub = p_cal.add_subparsers(dest="cal_cmd", required=True)

    p_c_list = cal_sub.add_parser("list", help="List merged calendar events")
    p_c_list.add_argument("--behind", type=int, default=0, help="Days in the past")
    p_c_list.add_argument("--days", type=int, default=7, help="Days ahead")
    p_c_list.set_defaults(func=cmd_cal_list)

    p_c_create = cal_sub.add_parser("create", help="Create event in Personal calendar")
    p_c_create.add_argument("--summary", required=True)
    p_c_create.add_argument("--start", required=True, help="ISO datetime or YYYY-MM-DD")
    p_c_create.add_argument("--end", default=None, help="ISO datetime or YYYY-MM-DD (defaults to +1h / +1d)")
    p_c_create.add_argument("--description", default="")
    p_c_create.add_argument("--location", default="")
    p_c_create.set_defaults(func=cmd_cal_create)

    p_c_update = cal_sub.add_parser("update", help="Update an event by ID")
    p_c_update.add_argument("--id", required=True)
    p_c_update.add_argument("--summary", default=None)
    p_c_update.add_argument("--start", default=None)
    p_c_update.add_argument("--end", default=None)
    p_c_update.add_argument("--description", default=None)
    p_c_update.add_argument("--location", default=None)
    p_c_update.set_defaults(func=cmd_cal_update)

    p_c_del = cal_sub.add_parser("delete", help="Delete an event by ID")
    p_c_del.add_argument("--id", required=True)
    p_c_del.set_defaults(func=cmd_cal_delete)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
