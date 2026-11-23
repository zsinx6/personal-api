#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_local_env() -> None:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def request(method: str, path: str, query: dict | None = None, body: dict | None = None):
    url = os.environ["PERSONAL_API_URL"].rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {os.environ['PERSONAL_API_TOKEN']}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None


def main() -> None:
    load_local_env()
    parser = argparse.ArgumentParser(prog="cli.py", description="Personal Trello & Google Calendar CLI")
    sub = parser.add_subparsers(dest="service", required=True)

    tr = sub.add_parser("trello", help="Read configured Trello board")
    tr.add_argument("--archived", action="store_true")

    cal = sub.add_parser("cal", help="Manage configured Google Calendar")
    cal_sub = cal.add_subparsers(dest="action", required=True)

    ls_p = cal_sub.add_parser("list")
    ls_p.add_argument("--days", type=int, default=7)
    ls_p.add_argument("--behind", type=int, default=0)

    add_p = cal_sub.add_parser("create")
    add_p.add_argument("--summary", required=True)
    add_p.add_argument("--start", required=True)
    add_p.add_argument("--end", required=True)
    add_p.add_argument("--desc", dest="description", default="")
    add_p.add_argument("--location", default="")

    upd_p = cal_sub.add_parser("update")
    upd_p.add_argument("--id", required=True)
    upd_p.add_argument("--summary")
    upd_p.add_argument("--start")
    upd_p.add_argument("--end")
    upd_p.add_argument("--desc", dest="description")
    upd_p.add_argument("--location")

    del_p = cal_sub.add_parser("delete")
    del_p.add_argument("--id", required=True)

    args = parser.parse_args()

    if args.service == "trello":
        out = request("GET", "/trello", query={"include_archived": str(args.archived).lower()})
    elif args.action == "list":
        out = request("GET", "/calendar/events", query={"days_ahead": args.days, "days_behind": args.behind})
    elif args.action == "create":
        payload = {k: v for k, v in vars(args).items() if k not in {"service", "action"}}
        out = request("POST", "/calendar/events", body=payload)
    elif args.action == "update":
        payload = {k: v for k, v in vars(args).items() if k not in {"service", "action", "id"} and v is not None}
        out = request("PATCH", f"/calendar/events/{urllib.parse.quote(args.id)}", body=payload)
    elif args.action == "delete":
        out = request("DELETE", f"/calendar/events/{urllib.parse.quote(args.id)}")

    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
