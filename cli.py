#!/usr/bin/env python3
"""Zero-dependency CLI bridge for personal-api (Calendar, Trello, and Markdown Notes)."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = Path(os.environ.get("PERSONAL_API_ENV", "/opt/data/personal-api/.env"))
NOTES_DIR = Path(os.environ.get("NOTES_DIR", "/opt/data/notes"))
DEFAULT_SAMPLE_SIZE = int(os.environ.get("NOTES_TEMPLATE_SAMPLE", "5"))

TASK_OPEN_RE = re.compile(r"^(\s*[-*]\s*)\[\s\](?:\s+(.*))?$")
TASK_LINE_RE = re.compile(r"^(\s*[-*]\s*)\[([ xX>])\](?:\s+(.*))?$")
DONE_STAMP_RE = re.compile(r"(?:\s*(?:✅|@done\()\s*(\d{4}-\d{2}-\d{2})\)?)\s*$")
LEADING_CHECKBOX_RE = re.compile(r"^(?:[-*]\s*\[[ xX>]\]\s*|[-*]\s+)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
DATE_PREFIX_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")
ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
PURE_DATE_TITLE_RE = re.compile(r"^\d{4}[-/]?\d{2}[-/]?\d{2}(?:[T\s_-]?\d{2}:?\d{2})?$")


def _local_now() -> datetime:
    return datetime.now().astimezone()


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
    raw_base_url = (
        os.environ.get("PERSONAL_API_URL")
        or cfg.get("PERSONAL_API_URL")
        or os.environ.get("API_URL")
        or cfg.get("API_URL")
        or ""
    ).strip()
    if not raw_base_url:
        raise RuntimeError(f"PERSONAL_API_URL is not configured in environment or {ENV_PATH}")
    if not raw_base_url.startswith(("http://", "https://")):
        raw_base_url = f"http://{raw_base_url}"
    base_url = raw_base_url.rstrip("/")

    token = (
        os.environ.get("PERSONAL_API_TOKEN")
        or cfg.get("PERSONAL_API_TOKEN")
        or os.environ.get("API_TOKEN")
        or cfg.get("API_TOKEN")
        or ""
    ).strip()
    if not token:
        raise RuntimeError(f"PERSONAL_API_TOKEN is not configured in environment or {ENV_PATH}")

    url = f"{base_url}{endpoint}"
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None}
        if clean_params:
            url = f"{url}?{urllib.parse.urlencode(clean_params)}"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

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


def _note_date(path: Path) -> str:
    stem = path.stem
    m_iso = ISO_DATE_PREFIX_RE.match(stem)
    if m_iso:
        return m_iso.group(1)
    m_compact = DATE_PREFIX_RE.match(stem)
    if m_compact:
        return f"{m_compact.group(1)}-{m_compact.group(2)}-{m_compact.group(3)}"
    mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return mtime.strftime("%Y-%m-%d")


def _iter_markdown_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for p in root.rglob("*.md"):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        files.append(p)
    return sorted(files, key=lambda p: (_note_date(p), p.name))


def _find_today_notes(root: Path, now: datetime) -> list[Path]:
    today_compact = now.strftime("%Y%m%d")
    today_iso = now.strftime("%Y-%m-%d")
    return [
        p
        for p in _iter_markdown_files(root)
        if p.stem.startswith(today_compact) or p.stem.startswith(today_iso)
    ]


def _body_start_index(lines: list[str]) -> int:
    """Return the first line index after YAML frontmatter (if any)."""
    if lines and lines[0].strip() == "---":
        for idx in range(1, len(lines)):
            if lines[idx].strip() in ("---", "..."):
                return idx + 1
    return 0


def _normalize_task_text(raw_text: str | None) -> tuple[str, str | None]:
    """Strip checkbox/completion stamps and return (clean_text, stamped_done_date)."""
    if not raw_text:
        return "", None
    cleaned = LEADING_CHECKBOX_RE.sub("", raw_text.strip()).strip()
    done_date: str | None = None
    m_stamp = DONE_STAMP_RE.search(cleaned)
    if m_stamp:
        done_date = m_stamp.group(1)
        cleaned = DONE_STAMP_RE.sub("", cleaned).strip()
    return cleaned, done_date


def _match_heading_name(heading_text: str, target_text: str) -> int:
    """Return match quality score (0 = no match, 1 = prefix/colon match, 2 = exact case-insensitive match)."""
    h_clean = heading_text.strip().lower()
    t_clean = target_text.strip().lower()
    if not h_clean or not t_clean:
        return 0
    if h_clean == t_clean:
        return 2
    if (
        h_clean.startswith(f"{t_clean} ")
        or h_clean.startswith(f"{t_clean}(")
        or h_clean.startswith(f"{t_clean}:")
    ):
        return 1
    return 0


def infer_note_template(root: Path, sample_size: int = DEFAULT_SAMPLE_SIZE) -> dict[str, object]:
    """Inspect the N most recent notes to infer section hierarchy, task leaf headings, and layout."""
    all_files = _iter_markdown_files(root)
    sample_files = all_files[-max(1, sample_size) :] if all_files else []

    if not sample_files:
        return {
            "sample_size": 0,
            "sampled_files": [],
            "title_mode": "static",
            "title": "# Daily Notes",
            "top_section_level": 2,
            "sections": [],
            "section_children": {},
            "task_leaf_by_section": {},
            "default_task_leaf": None,
            "use_dividers": False,
            "template_markdown": "# Daily Notes\n",
        }

    top_level_counts: Counter[int] = Counter()
    all_heading_levels: Counter[int] = Counter()
    divider_files = 0
    parsed_files: list[dict[str, object]] = []

    for md_file in sample_files:
        lines = md_file.read_text(encoding="utf-8").splitlines()
        start = _body_start_index(lines)
        file_headings: list[tuple[int, str]] = []
        active_by_level: dict[int, str] = {}
        task_paths: list[tuple[str, ...]] = []
        has_divider = False

        for line in lines[start:]:
            if line.strip() == "---":
                has_divider = True
                continue
            h = HEADING_RE.match(line)
            if h:
                lvl = len(h.group(1))
                text = h.group(2).strip()
                file_headings.append((lvl, text))
                all_heading_levels[lvl] += 1
                if lvl == 1 and PURE_DATE_TITLE_RE.match(text):
                    continue
                active_by_level[lvl] = text
                for deeper in [k for k in active_by_level if k > lvl]:
                    del active_by_level[deeper]
                continue

            m_task = TASK_LINE_RE.match(line)
            if m_task and active_by_level:
                path = tuple(active_by_level[k] for k in sorted(active_by_level))
                task_paths.append(path)
                top_lvl = min(active_by_level.keys())
                top_level_counts[top_lvl] += 1

        if has_divider:
            divider_files += 1
        parsed_files.append({"headings": file_headings, "task_paths": task_paths})

    if top_level_counts:
        top_section_level = max(top_level_counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    elif all_heading_levels:
        non_date_h1 = [lvl for lvl in all_heading_levels if lvl >= 2]
        top_section_level = min(non_date_h1) if non_date_h1 else min(all_heading_levels.keys())
    else:
        top_section_level = 2

    # Detect H1 title style (date-stamped vs static vs none)
    date_h1_count = 0
    static_h1_counts: Counter[str] = Counter()
    static_h1_display: dict[str, str] = {}
    if top_section_level > 1:
        for pdata in parsed_files:
            f_headings = list(pdata["headings"])  # type: ignore[arg-type]
            for lvl, text in f_headings:
                if lvl == 1:
                    if PURE_DATE_TITLE_RE.match(text):
                        date_h1_count += 1
                    else:
                        static_h1_counts[text.lower()] += 1
                        static_h1_display[text.lower()] = text
                    break

    title_mode = "none"
    title_line: str | None = None
    if date_h1_count >= max(1, len(sample_files) // 2):
        title_mode = "date"
        title_line = "# {date}"
    elif static_h1_counts:
        top_k, top_c = static_h1_counts.most_common(1)[0]
        if top_c >= max(1, len(sample_files) // 2):
            title_mode = "static"
            title_line = f"# {static_h1_display[top_k]}"

    # Extract ordered recurring top-level sections and their child subheadings
    sec_counts: Counter[str] = Counter()
    sec_display: dict[str, str] = {}
    sec_positions: dict[str, list[float]] = defaultdict(list)
    sec_last_seen: dict[str, int] = {}

    child_counts_by_sec: dict[str, Counter[str]] = defaultdict(Counter)
    child_display_by_sec: dict[str, dict[str, str]] = defaultdict(dict)
    child_order_by_sec: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))

    task_leaf_counts_by_sec: dict[str, Counter[str]] = defaultdict(Counter)
    global_task_leaf_counts: Counter[str] = Counter()
    global_leaf_display: dict[str, str] = {}

    for file_idx, pdata in enumerate(parsed_files):
        f_headings: list[tuple[int, str]] = pdata["headings"]  # type: ignore[assignment]
        t_paths: list[tuple[str, ...]] = pdata["task_paths"]  # type: ignore[assignment]

        top_secs_in_file: list[str] = []
        current_top_key: str | None = None
        child_idx = 0

        for lvl, text in f_headings:
            if lvl == 1 and PURE_DATE_TITLE_RE.match(text):
                continue
            if lvl == top_section_level:
                key = text.lower()
                current_top_key = key
                child_idx = 0
                if key not in top_secs_in_file:
                    top_secs_in_file.append(text)
            elif lvl > top_section_level and current_top_key is not None:
                ckey = text.lower()
                child_counts_by_sec[current_top_key][ckey] += 1
                child_display_by_sec[current_top_key][ckey] = text
                child_order_by_sec[current_top_key][ckey].append(child_idx)
                child_idx += 1

        total = max(1, len(top_secs_in_file))
        for pos_idx, text in enumerate(top_secs_in_file):
            key = text.lower()
            sec_counts[key] += 1
            sec_display[key] = text
            sec_positions[key].append(pos_idx / total)
            sec_last_seen[key] = file_idx

        for path in t_paths:
            if len(path) >= 2:
                top_k = path[0].lower()
                leaf_text = path[-1]
                leaf_k = leaf_text.lower()
                task_leaf_counts_by_sec[top_k][leaf_k] += 1
                global_task_leaf_counts[leaf_k] += 1
                global_leaf_display[leaf_k] = leaf_text

    min_occurrences = 2 if (len(sample_files) >= 2 and any(c >= 2 for c in sec_counts.values())) else 1
    chosen_keys = [k for k, c in sec_counts.items() if c >= min_occurrences]
    chosen_keys.sort(key=lambda k: (sum(sec_positions[k]) / len(sec_positions[k]), -sec_last_seen[k]))
    ordered_sections = [sec_display[k] for k in chosen_keys]

    section_children: dict[str, list[str]] = {}
    task_leaf_by_section: dict[str, str] = {}
    default_task_leaf: str | None = None
    if global_task_leaf_counts:
        top_leaf_k = global_task_leaf_counts.most_common(1)[0][0]
        default_task_leaf = global_leaf_display[top_leaf_k]

    for k in chosen_keys:
        display_sec = sec_display[k]
        c_counts = child_counts_by_sec.get(k, Counter())
        if c_counts:
            c_min = 2 if (sec_counts[k] >= 2 and any(c >= 2 for c in c_counts.values())) else 1
            c_keys = [ck for ck, cnt in c_counts.items() if cnt >= c_min]
            c_keys.sort(key=lambda ck: sum(child_order_by_sec[k][ck]) / len(child_order_by_sec[k][ck]))
            section_children[display_sec] = [child_display_by_sec[k][ck] for ck in c_keys]
        else:
            section_children[display_sec] = []

        sec_leaf_counts = task_leaf_counts_by_sec.get(k, Counter())
        if sec_leaf_counts:
            best_leaf_k = sec_leaf_counts.most_common(1)[0][0]
            task_leaf_by_section[display_sec] = global_leaf_display.get(
                best_leaf_k, child_display_by_sec[k].get(best_leaf_k, best_leaf_k)
            )
        elif default_task_leaf:
            task_leaf_by_section[display_sec] = default_task_leaf

    use_dividers = divider_files >= max(1, len(sample_files) // 2)
    top_prefix = "#" * top_section_level
    child_prefix = "#" * (top_section_level + 1)

    tmpl_lines: list[str] = []
    if title_line:
        tmpl_lines.append(title_line)
    for idx, sec in enumerate(ordered_sections):
        if tmpl_lines:
            tmpl_lines.append("")
        if idx > 0 and use_dividers:
            tmpl_lines.extend(["---", ""])
        tmpl_lines.append(f"{top_prefix} {sec}")
        for child in section_children.get(sec, []):
            tmpl_lines.append(f"{child_prefix} {child}")
            tmpl_lines.append("")
        if tmpl_lines and tmpl_lines[-1] == "":
            tmpl_lines.pop()

    template_md = ("\n".join(tmpl_lines) + "\n") if tmpl_lines else ""

    return {
        "sample_size": len(sample_files),
        "sampled_files": [str(p.relative_to(root)) for p in sample_files],
        "title_mode": title_mode,
        "title": title_line,
        "top_section_level": top_section_level,
        "sections": ordered_sections,
        "section_children": section_children,
        "task_leaf_by_section": task_leaf_by_section,
        "default_task_leaf": default_task_leaf,
        "use_dividers": use_dividers,
        "template_markdown": template_md,
    }


def _iter_parsed_tasks(lines: list[str]) -> list[tuple[int, str, str, tuple[str, ...]]]:
    """Yield (1-based line_no, state_char, raw_task_body, heading_path) for every task in lines."""
    start = _body_start_index(lines)
    active: dict[int, str] = {}
    parsed: list[tuple[int, str, str, tuple[str, ...]]] = []

    for idx in range(start, len(lines)):
        line = lines[idx]
        h = HEADING_RE.match(line)
        if h:
            lvl = len(h.group(1))
            text = h.group(2).strip()
            if lvl == 1 and PURE_DATE_TITLE_RE.match(text):
                continue
            active[lvl] = text
            for deeper in [k for k in active if k > lvl]:
                del active[deeper]
            continue

        m = TASK_LINE_RE.match(line)
        if m:
            path = tuple(active[k] for k in sorted(active))
            parsed.append((idx + 1, m.group(2), m.group(3) or "", path))

    return parsed


def scan_open_tasks(root: Path, today_str: str) -> list[dict[str, object]]:
    """Return deduplicated open tasks with first_seen date, full heading path, and in_today_note flag."""
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    merged: dict[str, dict[str, object]] = {}

    for md_file in _iter_markdown_files(root):
        rel = str(md_file.relative_to(root))
        file_date = _note_date(md_file)
        lines = md_file.read_text(encoding="utf-8").splitlines()

        for line_no, state_char, raw_body, path in _iter_parsed_tasks(lines):
            if state_char != " ":
                continue
            text, _ = _normalize_task_text(raw_body)
            if not text:
                continue

            key = text.lower()
            is_today = file_date >= today_str
            top_section = path[0] if path else "Uncategorized"
            sub_section = path[-1] if len(path) >= 2 else None

            if key not in merged:
                merged[key] = {
                    "text": text,
                    "section": top_section,
                    "sub_section": sub_section,
                    "section_path": list(path),
                    "first_seen": file_date,
                    "first_file": rel,
                    "latest_file": rel,
                    "line": line_no,
                    "in_today_note": is_today,
                }
            else:
                entry = merged[key]
                entry["latest_file"] = rel
                entry["line"] = line_no
                if path:
                    entry["section"] = top_section
                    entry["sub_section"] = sub_section
                    entry["section_path"] = list(path)
                if file_date < str(entry["first_seen"]):
                    entry["first_seen"] = file_date
                    entry["first_file"] = rel
                if is_today:
                    entry["in_today_note"] = True

    results: list[dict[str, object]] = []
    for entry in merged.values():
        first_dt = datetime.strptime(str(entry["first_seen"]), "%Y-%m-%d").date()
        entry["overdue"] = first_dt < today_dt
        entry["days_open"] = max(0, (today_dt - first_dt).days)
        results.append(entry)

    results.sort(key=lambda r: (str(r["first_seen"]), str(r["text"]).lower()))
    return results


def build_task_lifecycle(root: Path, today_str: str) -> list[dict[str, object]]:
    """Aggregate every task ([ ], [x], [>]) across all notes to track first_seen and done_date."""
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    registry: dict[str, dict[str, object]] = {}

    for md_file in _iter_markdown_files(root):
        rel = str(md_file.relative_to(root))
        file_date = _note_date(md_file)
        lines = md_file.read_text(encoding="utf-8").splitlines()

        for _, state_char, raw_body, path in _iter_parsed_tasks(lines):
            clean_text, stamped_done = _normalize_task_text(raw_body)
            if not clean_text:
                continue

            key = clean_text.lower()
            is_done = state_char in ("x", "X")
            is_open = state_char == " "
            resolved_done_date = (stamped_done or file_date) if is_done else None
            top_section = path[0] if path else "Uncategorized"
            sub_section = path[-1] if len(path) >= 2 else None

            if key not in registry:
                registry[key] = {
                    "task": clean_text,
                    "section": top_section,
                    "sub_section": sub_section,
                    "status": "done" if is_done else "open",
                    "first_seen": file_date,
                    "first_file": rel,
                    "latest_file": rel,
                    "done_date": resolved_done_date,
                    "in_today_note": is_open and (file_date >= today_str),
                }
            else:
                entry = registry[key]
                entry["latest_file"] = rel
                if path:
                    entry["section"] = top_section
                    entry["sub_section"] = sub_section
                if file_date < str(entry["first_seen"]):
                    entry["first_seen"] = file_date
                    entry["first_file"] = rel
                if is_open and file_date >= today_str:
                    entry["in_today_note"] = True
                if is_done:
                    entry["status"] = "done"
                    entry["in_today_note"] = False
                    prev_done = entry["done_date"]
                    if not prev_done or (resolved_done_date and resolved_done_date > str(prev_done)):
                        entry["done_date"] = resolved_done_date

    results: list[dict[str, object]] = []
    for entry in registry.values():
        start_dt = datetime.strptime(str(entry["first_seen"]), "%Y-%m-%d").date()
        if entry["status"] == "done" and entry["done_date"]:
            end_dt = datetime.strptime(str(entry["done_date"]), "%Y-%m-%d").date()
            entry["days_elapsed"] = max(0, (end_dt - start_dt).days)
            entry["overdue"] = False
        else:
            entry["days_elapsed"] = max(0, (today_dt - start_dt).days)
            entry["overdue"] = start_dt < today_dt
        results.append(entry)

    results.sort(key=lambda r: (0 if r["status"] == "open" else 1, str(r["first_seen"]), str(r["task"]).lower()))
    return results


def _resolve_target_note(root: Path, now: datetime, sample_size: int = DEFAULT_SAMPLE_SIZE) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    today_matches = _find_today_notes(root, now)
    if today_matches:
        return sorted(today_matches)[-1]

    tmpl = infer_note_template(root, sample_size)
    raw_md = str(tmpl["template_markdown"]).replace("{date}", now.strftime("%Y-%m-%d"))
    new_note = root / f"{now.strftime('%Y%m%d%H%M')}.md"
    new_note.write_text(raw_md, encoding="utf-8")
    return new_note


def _find_heading_span(
    lines: list[str],
    start_idx: int,
    end_idx: int,
    target_name: str,
    preferred_level: int | None = None,
) -> tuple[int, int, int] | None:
    """Find best matching heading in [start_idx, end_idx); return (heading_idx, level, span_end_idx)."""
    best_idx: int | None = None
    best_level = preferred_level or 2
    best_score = 0

    for idx in range(start_idx, end_idx):
        h = HEADING_RE.match(lines[idx])
        if not h:
            continue
        lvl = len(h.group(1))
        text = h.group(2).strip()
        if lvl == 1 and PURE_DATE_TITLE_RE.match(text):
            continue
        score = _match_heading_name(text, target_name)
        if score == 0:
            continue
        if preferred_level is not None and lvl == preferred_level:
            score += 1
        if score > best_score:
            best_score = score
            best_idx = idx
            best_level = lvl

    if best_idx is None:
        return None

    span_end = best_idx + 1
    while span_end < end_idx:
        if lines[span_end].strip() == "---":
            break
        h = HEADING_RE.match(lines[span_end])
        if h and len(h.group(1)) <= best_level:
            break
        span_end += 1

    return best_idx, best_level, span_end


def _insert_into_span(lines: list[str], span_start: int, span_end: int, new_line: str) -> None:
    """Insert new_line at the end of [span_start, span_end), replacing an empty '- [ ]' placeholder if present."""
    for idx in range(span_start, span_end):
        m = TASK_OPEN_RE.match(lines[idx])
        if m and not (m.group(2) or "").strip():
            lines[idx] = new_line
            return

    insert_at = span_end
    while insert_at > span_start and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    lines.insert(insert_at, new_line)


def _insert_task_into_note(
    target: Path,
    task_text: str,
    section: str | None,
    sub_section: str | None,
    tmpl: dict[str, object],
) -> tuple[str, str]:
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    rel_target = str(target.relative_to(NOTES_DIR)) if target.is_relative_to(NOTES_DIR) else str(target)
    body_start = _body_start_index(lines)

    for line in lines[body_start:]:
        m_any = TASK_LINE_RE.match(line)
        if m_any:
            existing_clean, _ = _normalize_task_text(m_any.group(3))
            if existing_clean.lower() == task_text.lower():
                return "skipped_duplicate_in_target_note", rel_target

    new_line = f"- [ ] {task_text}"
    top_level = int(tmpl["top_section_level"])
    template_sections = [str(s) for s in list(tmpl["sections"])]
    section_children: dict[str, list[str]] = tmpl["section_children"]  # type: ignore[assignment]
    task_leaf_by_section: dict[str, str] = tmpl["task_leaf_by_section"]  # type: ignore[assignment]
    default_task_leaf = str(tmpl["default_task_leaf"]) if tmpl["default_task_leaf"] else None
    use_dividers = bool(tmpl["use_dividers"])

    if section and section != "Uncategorized":
        canonical_sec = next(
            (s for s in template_sections if _match_heading_name(s, section) > 0),
            section.strip(),
        )
        desired_leaf = sub_section or task_leaf_by_section.get(canonical_sec) or default_task_leaf

        top_span = _find_heading_span(lines, body_start, len(lines), canonical_sec, preferred_level=top_level)
        if top_span is not None:
            top_idx, found_top_lvl, top_end = top_span
            if desired_leaf:
                leaf_span = _find_heading_span(
                    lines, top_idx + 1, top_end, desired_leaf, preferred_level=found_top_lvl + 1
                )
                if leaf_span is not None:
                    leaf_idx, _, leaf_end = leaf_span
                    _insert_into_span(lines, leaf_idx + 1, leaf_end, new_line)
                else:
                    # Parent section exists in target, but task subheading does not yet
                    leaf_heading = f"{'#' * (found_top_lvl + 1)} {desired_leaf}"
                    insert_at = top_end
                    while insert_at > top_idx + 1 and lines[insert_at - 1].strip() == "":
                        insert_at -= 1
                    block = (["", leaf_heading, new_line] if insert_at > top_idx + 1 else [leaf_heading, new_line])
                    lines[insert_at:insert_at] = block
            else:
                _insert_into_span(lines, top_idx + 1, top_end, new_line)
        else:
            # Top-level section is missing from target: build full section skeleton in template order
            top_heading = f"{'#' * top_level} {canonical_sec}"
            child_heading_lvl = top_level + 1
            children = list(section_children.get(canonical_sec, []))
            if desired_leaf and not any(_match_heading_name(c, desired_leaf) > 0 for c in children):
                children.append(desired_leaf)

            sec_block: list[str] = [top_heading]
            if children:
                for child in children:
                    sec_block.append(f"{'#' * child_heading_lvl} {child}")
                    if desired_leaf and _match_heading_name(child, desired_leaf) > 0:
                        sec_block.append(new_line)
                    sec_block.append("")
                if sec_block[-1] == "":
                    sec_block.pop()
            else:
                sec_block.append(new_line)

            after_sections: list[str] = []
            for i, s in enumerate(template_sections):
                if s.lower() == canonical_sec.lower():
                    after_sections = template_sections[i + 1 :]
                    break

            insert_heading_at: int | None = None
            if after_sections:
                for idx in range(body_start, len(lines)):
                    h = HEADING_RE.match(lines[idx])
                    if h and len(h.group(1)) == top_level:
                        if any(_match_heading_name(h.group(2), nxt) > 0 for nxt in after_sections):
                            insert_heading_at = idx
                            if use_dividers and insert_heading_at >= 2 and lines[insert_heading_at - 2].strip() == "---":
                                insert_heading_at -= 2
                            break

            if insert_heading_at is not None:
                prefix = ["---", ""] if (use_dividers and insert_heading_at > body_start) else []
                suffix = ["", "---", ""] if use_dividers else [""]
                lines[insert_heading_at:insert_heading_at] = prefix + sec_block + suffix
            else:
                if lines and lines[-1].strip() != "":
                    lines.append("")
                if use_dividers and any(HEADING_RE.match(l) for l in lines[body_start:]):
                    lines.extend(["---", ""])
                lines.extend(sec_block)
    else:
        _insert_into_span(lines, body_start, len(lines), new_line)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "added", rel_target


def _mark_forwarded_in_older_notes(root: Path, task_lower: str, exclude_path: Path) -> list[str]:
    forwarded_files: list[str] = []
    for md_file in _iter_markdown_files(root):
        if md_file == exclude_path:
            continue
        lines = md_file.read_text(encoding="utf-8").splitlines()
        changed = False
        for idx, line in enumerate(lines):
            m = TASK_OPEN_RE.match(line)
            if m:
                clean_body, _ = _normalize_task_text(m.group(2))
                if clean_body.lower() == task_lower:
                    lines[idx] = f"{m.group(1)}[>] {clean_body}"
                    changed = True
        if changed:
            md_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            forwarded_files.append(str(md_file.relative_to(root)))
    return forwarded_files


def cmd_audit(args: argparse.Namespace) -> None:
    now = _local_now()
    today_str = now.strftime("%Y-%m-%d")
    today_notes = _find_today_notes(NOTES_DIR, now)
    tmpl = infer_note_template(NOTES_DIR, args.sample_notes)

    cfg = load_env(ENV_PATH)
    member = args.member or os.environ.get("TRELLO_MEMBER") or cfg.get("TRELLO_MEMBER") or None

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_trello = pool.submit(
            api_request,
            "GET",
            "/trello/cards",
            {"member": member} if member else None,
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
        "today_note_exists": bool(today_notes),
        "today_note": str(sorted(today_notes)[-1].relative_to(NOTES_DIR)) if today_notes else None,
        "inferred_sections": tmpl["sections"],
        "notes_open": open_notes,
        "trello_cards": trello_cards,
        "calendar_events": cal_events,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_notes_template(args: argparse.Namespace) -> None:
    tmpl = infer_note_template(NOTES_DIR, args.sample_notes)
    print(json.dumps(tmpl, ensure_ascii=False, indent=2))


def cmd_summarize(args: argparse.Namespace) -> None:
    now = _local_now()
    today_str = now.strftime("%Y-%m-%d")
    today_dt = now.date()

    items = build_task_lifecycle(NOTES_DIR, today_str)

    if args.status != "all":
        items = [i for i in items if i["status"] == args.status]

    if args.section:
        items = [i for i in items if _match_heading_name(str(i["section"]), args.section) > 0]

    if args.days is not None:
        cutoff = (today_dt - timedelta(days=args.days)).strftime("%Y-%m-%d")
        items = [
            i
            for i in items
            if str(i["first_seen"]) >= cutoff or (i["done_date"] and str(i["done_date"]) >= cutoff)
        ]

    open_items = [i for i in items if i["status"] == "open"]
    done_items = [i for i in items if i["status"] == "done"]

    if args.md:
        lines = [f"# Task Summary ({today_str})", ""]
        lines.append(f"## Open Tasks ({len(open_items)})")
        if not open_items:
            lines.append("- None")
        else:
            for it in open_items:
                flags: list[str] = []
                if it["overdue"]:
                    flags.append(f"OVERDUE {it['days_elapsed']}d")
                if not it["in_today_note"]:
                    flags.append("NOT IN TODAY'S NOTE")
                badge = f" [{', '.join(flags)}]" if flags else ""
                lines.append(
                    f"- [ ] **{it['task']}** ({it['section']}) — First seen: `{it['first_seen']}`{badge}"
                )
        lines.append("")
        lines.append(f"## Completed Tasks ({len(done_items)})")
        if not done_items:
            lines.append("- None")
        else:
            for it in done_items:
                lines.append(
                    f"- [x] **{it['task']}** ({it['section']}) — First seen: `{it['first_seen']}` | "
                    f"Done: `{it['done_date']}` ({it['days_elapsed']}d)"
                )
        print("\n".join(lines))
        return

    payload = {
        "today": today_str,
        "counts": {"open": len(open_items), "done": len(done_items), "total": len(items)},
        "open_tasks": open_items,
        "done_tasks": done_items,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_notes_add(args: argparse.Namespace) -> None:
    now = _local_now()
    task_text, _ = _normalize_task_text(args.text)
    if not task_text:
        raise RuntimeError("Task text cannot be empty")

    tmpl = infer_note_template(NOTES_DIR, args.sample_notes)
    target = Path(args.file) if args.file else _resolve_target_note(NOTES_DIR, now, args.sample_notes)
    if not target.is_absolute():
        target = NOTES_DIR / target

    status, rel_target = _insert_task_into_note(target, task_text, args.section, args.sub_section, tmpl)
    print(
        json.dumps(
            {
                "status": status,
                "file": rel_target,
                "section": args.section or "End of file",
                "sub_section": args.sub_section,
                "line": f"- [ ] {task_text}",
            },
            ensure_ascii=False,
        )
    )


def cmd_notes_rollover(args: argparse.Namespace) -> None:
    """Copy an open task from an earlier note into today's note and mark the old line - [>]."""
    now = _local_now()
    today_str = now.strftime("%Y-%m-%d")
    needle, _ = _normalize_task_text(args.match)
    needle_lower = needle.lower()

    tmpl = infer_note_template(NOTES_DIR, args.sample_notes)
    target = _resolve_target_note(NOTES_DIR, now, args.sample_notes)

    open_tasks = scan_open_tasks(NOTES_DIR, today_str)
    matches = [
        t
        for t in open_tasks
        if needle_lower in str(t["text"]).lower() and not bool(t["in_today_note"])
    ]

    if not matches:
        raise RuntimeError(f"No open task missing from today's note matched: {args.match!r}")
    if len(matches) > 1:
        preview = [f"{t['first_file']}: {t['text']}" for t in matches]
        raise RuntimeError(f"Multiple tasks matched {args.match!r}; be more specific:\n" + "\n".join(preview))

    item = matches[0]
    task_text = str(item["text"])
    section = args.section or (str(item["section"]) if item["section"] != "Uncategorized" else None)
    sub_section = str(item["sub_section"]) if item["sub_section"] else None
    status, rel_target = _insert_task_into_note(target, task_text, section, sub_section, tmpl)
    forwarded_from = _mark_forwarded_in_older_notes(NOTES_DIR, task_text.lower(), target)

    print(
        json.dumps(
            {
                "status": "rolled_over" if status == "added" else status,
                "to_file": rel_target,
                "section": section or "End of file",
                "sub_section": sub_section,
                "task": task_text,
                "first_seen": item["first_seen"],
                "forwarded_from": forwarded_from,
            },
            ensure_ascii=False,
        )
    )


def cmd_notes_rollover_all(args: argparse.Namespace) -> None:
    """Roll over every open task missing from today's note into its original section path."""
    now = _local_now()
    today_str = now.strftime("%Y-%m-%d")
    today_notes = _find_today_notes(NOTES_DIR, now)

    if args.only_if_today_exists and not today_notes:
        print(
            json.dumps(
                {
                    "status": "skipped_no_today_note",
                    "today": today_str,
                    "rolled_over": [],
                },
                ensure_ascii=False,
            )
        )
        return

    tmpl = infer_note_template(NOTES_DIR, args.sample_notes)
    open_tasks = scan_open_tasks(NOTES_DIR, today_str)
    missing = [t for t in open_tasks if not bool(t["in_today_note"])]
    if not missing:
        print(
            json.dumps(
                {
                    "status": "nothing_to_rollover",
                    "today_note": str(sorted(today_notes)[-1].relative_to(NOTES_DIR)) if today_notes else None,
                    "rolled_over": [],
                },
                ensure_ascii=False,
            )
        )
        return

    target = _resolve_target_note(NOTES_DIR, now, args.sample_notes)
    rolled: list[dict[str, object]] = []
    for item in missing:
        task_text = str(item["text"])
        section = str(item["section"]) if item["section"] != "Uncategorized" else None
        sub_section = str(item["sub_section"]) if item["sub_section"] else None
        status, _ = _insert_task_into_note(target, task_text, section, sub_section, tmpl)
        forwarded_from = _mark_forwarded_in_older_notes(NOTES_DIR, task_text.lower(), target)
        rolled.append(
            {
                "status": "rolled_over" if status == "added" else status,
                "task": task_text,
                "section": section or "End of file",
                "sub_section": sub_section,
                "first_seen": item["first_seen"],
                "forwarded_from": forwarded_from,
            }
        )

    print(
        json.dumps(
            {
                "status": "completed",
                "to_file": str(target.relative_to(NOTES_DIR)),
                "count": len(rolled),
                "rolled_over": rolled,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_notes_done(args: argparse.Namespace) -> None:
    now = _local_now()
    today_str = now.strftime("%Y-%m-%d")
    needle, _ = _normalize_task_text(args.match)
    needle_lower = needle.lower()

    grouped: dict[str, list[tuple[Path, int, str, str]]] = {}
    for md_file in _iter_markdown_files(NOTES_DIR):
        file_lines = md_file.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(file_lines):
            m = TASK_OPEN_RE.match(line)
            if not m:
                continue
            clean_body, _ = _normalize_task_text(m.group(2))
            if clean_body and needle_lower in clean_body.lower():
                grouped.setdefault(clean_body.lower(), []).append((md_file, idx, line, clean_body))

    if not grouped:
        raise RuntimeError(f"No open task matched: {args.match!r}")
    if len(grouped) > 1:
        preview = [entries[0][3] for entries in grouped.values()]
        raise RuntimeError(f"Multiple distinct tasks matched {args.match!r}; be more specific:\n" + "\n".join(preview))

    occurrences = next(iter(grouped.values()))
    latest_file, latest_idx, latest_line, clean_body = occurrences[-1]
    for md_file, line_idx, old_line, _ in occurrences[:-1]:
        flines = md_file.read_text(encoding="utf-8").splitlines()
        m_old = TASK_OPEN_RE.match(old_line)
        assert m_old is not None
        flines[line_idx] = f"{m_old.group(1)}[>] {clean_body}"
        md_file.write_text("\n".join(flines) + "\n", encoding="utf-8")

    latest_lines = latest_file.read_text(encoding="utf-8").splitlines()
    m_lat = TASK_OPEN_RE.match(latest_line)
    assert m_lat is not None
    updated_line = f"{m_lat.group(1)}[x] {clean_body} ✅ {today_str}"
    latest_lines[latest_idx] = updated_line
    latest_file.write_text("\n".join(latest_lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "completed",
                "file": str(latest_file.relative_to(NOTES_DIR)),
                "line_number": latest_idx + 1,
                "done_date": today_str,
                "updated": updated_line.strip(),
            },
            ensure_ascii=False,
        )
    )


def cmd_trello(args: argparse.Namespace) -> None:
    cfg = load_env(ENV_PATH)
    member = args.member or os.environ.get("TRELLO_MEMBER") or cfg.get("TRELLO_MEMBER") or None
    res = api_request("GET", "/trello/cards", {"member": member} if member else None)
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
    p_audit.add_argument("--member", default=None, help="Trello username filter (or set TRELLO_MEMBER)")
    p_audit.add_argument("--behind", type=int, default=7, help="Calendar days behind (default: 7)")
    p_audit.add_argument("--days", type=int, default=14, help="Calendar days ahead (default: 14)")
    p_audit.add_argument("--sample-notes", type=int, default=DEFAULT_SAMPLE_SIZE, help="Recent notes to infer sections")
    p_audit.set_defaults(func=cmd_audit)

    # summarize
    p_sum = sub.add_parser("summarize", help="Lifecycle summary of open & done tasks with first_seen and done_date")
    p_sum.add_argument("--status", choices=["all", "open", "done"], default="all", help="Filter by status")
    p_sum.add_argument("--section", default=None, help="Filter by section heading")
    p_sum.add_argument("--days", type=int, default=None, help="Only tasks active/done in the last N days")
    p_sum.add_argument("--md", action="store_true", help="Output formatted Markdown instead of JSON")
    p_sum.set_defaults(func=cmd_summarize)

    # notes
    p_notes = sub.add_parser("notes", help="Manage Markdown tasks")
    notes_sub = p_notes.add_subparsers(dest="notes_cmd", required=True)

    p_n_tmpl = notes_sub.add_parser("template", help="Inspect the note template inferred from the N most recent notes")
    p_n_tmpl.add_argument("--sample-notes", type=int, default=DEFAULT_SAMPLE_SIZE, help="Number of recent notes to inspect")
    p_n_tmpl.set_defaults(func=cmd_notes_template)

    p_n_add = notes_sub.add_parser("add", help="Add an open task (- [ ]) to today's note")
    p_n_add.add_argument("text", help="Task description")
    p_n_add.add_argument("--section", help="Target top-level section heading (e.g. Work, Personal, APyB)")
    p_n_add.add_argument("--sub-section", default=None, help="Optional nested task heading (defaults to inferred leaf)")
    p_n_add.add_argument("--file", help="Specific target note relative to NOTES_DIR")
    p_n_add.add_argument("--sample-notes", type=int, default=DEFAULT_SAMPLE_SIZE, help="Recent notes to infer template")
    p_n_add.set_defaults(func=cmd_notes_add)

    p_n_roll = notes_sub.add_parser("rollover", help="Bring a single open task from an earlier note into today's note")
    p_n_roll.add_argument("match", help="Unique substring matching the task text")
    p_n_roll.add_argument("--section", help="Target top-level section heading (defaults to original section)")
    p_n_roll.add_argument("--sample-notes", type=int, default=DEFAULT_SAMPLE_SIZE, help="Recent notes to infer template")
    p_n_roll.set_defaults(func=cmd_notes_rollover)

    p_n_roll_all = notes_sub.add_parser(
        "rollover-all",
        help="Roll over all open tasks missing from today's note into their original section paths",
    )
    p_n_roll_all.add_argument(
        "--only-if-today-exists",
        action="store_true",
        help="Skip unless today's YYYYMMDD*.md / YYYY-MM-DD*.md note already exists",
    )
    p_n_roll_all.add_argument("--sample-notes", type=int, default=DEFAULT_SAMPLE_SIZE, help="Recent notes to infer template")
    p_n_roll_all.set_defaults(func=cmd_notes_rollover_all)

    p_n_done = notes_sub.add_parser("done", help="Mark an open task (- [ ]) done and stamp ✅ YYYY-MM-DD")
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
