import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Path as ApiPath, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
SERVICE_ACCOUNT_FILE = BASE_DIR / "service-account.json"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
EVENT_ID_PATTERN = r"^[a-zA-Z0-9_-]{5,128}$"
BOARD_ID_RE = re.compile(r"^[a-zA-Z0-9]{6,32}$")


def load_local_env() -> None:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_local_env()
bearer = HTTPBearer()


def require_token(creds: HTTPAuthorizationCredentials = Security(bearer)) -> None:
    expected = os.environ.get("PERSONAL_API_TOKEN", "")
    if not expected or not secrets.compare_digest(creds.credentials, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


app = FastAPI(dependencies=[Depends(require_token)], docs_url=None, redoc_url=None)


def get_calendar_service():
    if not SERVICE_ACCOUNT_FILE.exists():
        raise HTTPException(status_code=500, detail="service-account.json is missing")
    creds = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_FILE), scopes=GOOGLE_SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _calendar_id() -> str:
    cal_id = os.environ.get("CALENDAR_ID", "").strip()
    if not cal_id:
        raise HTTPException(status_code=500, detail="CALENDAR_ID is not configured")
    return cal_id


def _trello_board_id() -> str:
    board_id = os.environ.get("TRELLO_BOARD_ID", "").strip()
    if not BOARD_ID_RE.match(board_id):
        raise HTTPException(status_code=500, detail="TRELLO_BOARD_ID is missing or invalid")
    return board_id


def _gcal_time(value: str) -> dict[str, str]:
    return {"date": value} if len(value) == 10 else {"dateTime": value}


class EventCreate(BaseModel):
    summary: str
    start: str  # ISO-8601 datetime with offset (e.g. 2026-11-24T14:00:00-03:00) or YYYY-MM-DD
    end: str
    description: str = ""
    location: str = ""


class EventUpdate(BaseModel):
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    description: str | None = None
    location: str | None = None


@app.get("/trello")
def get_trello_board(include_archived: bool = False, member: str | None = None) -> dict[str, Any]:
    state = "all" if include_archived else "open"
    board_id = _trello_board_id()
    headers = {
        "Authorization": f'OAuth oauth_consumer_key="{os.environ["TRELLO_API_KEY"]}", oauth_token="{os.environ["TRELLO_TOKEN"]}"'
    }
    params = {
        "fields": "name,url",
        "lists": state,
        "list_fields": "name,pos,closed",
        "members": "all",
        "member_fields": "username,fullName",
        "cards": state,
        "card_fields": "name,desc,due,dueComplete,idList,idMembers,labels,shortUrl",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"https://api.trello.com/1/boards/{board_id}", headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="Trello API request failed") from None
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Could not reach Trello API") from None

    members_by_id = {
        m["id"]: {"username": m.get("username", ""), "fullName": m.get("fullName", "")}
        for m in data.get("members", [])
    }
    member_filter = member.strip().lower() if member else None

    lists = {lst["id"]: {"name": lst["name"], "pos": lst["pos"], "cards": []} for lst in data.get("lists", [])}
    for card in data.get("cards", []):
        card_members = [members_by_id[mid] for mid in card.get("idMembers", []) if mid in members_by_id]
        if member_filter and not any(
            member_filter in (m["username"].lower(), m["fullName"].lower()) for m in card_members
        ):
            continue
        if (target := lists.get(card["idList"])) is not None:
            target["cards"].append({
                "name": card["name"],
                "desc": card["desc"],
                "due": card["due"],
                "dueComplete": card["dueComplete"],
                "members": card_members,
                "labels": [lbl.get("name") or lbl.get("color") for lbl in card.get("labels", [])],
                "url": card["shortUrl"],
            })

    return {"board": data.get("name"), "url": data.get("url"), "lists": sorted(lists.values(), key=lambda x: x["pos"])}

@app.get("/calendar/events")
def list_events(days_ahead: int = 7, days_behind: int = 0, max_results: int = 30) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    try:
        res = (
            get_calendar_service()
            .events()
            .list(
                calendarId=_calendar_id(),
                timeMin=(now - timedelta(days=max(0, days_behind))).isoformat(),
                timeMax=(now + timedelta(days=max(1, min(days_ahead, 90)))).isoformat(),
                maxResults=min(max_results, 100),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError as exc:
        raise HTTPException(status_code=exc.resp.status, detail=exc._get_reason()) from None

    return [
        {
            "id": e.get("id"),
            "summary": e.get("summary", "(No title)"),
            "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
            "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
            "location": e.get("location", ""),
            "description": e.get("description", ""),
        }
        for e in res.get("items", [])
    ]


@app.post("/calendar/events")
def create_event(payload: EventCreate) -> dict[str, Any]:
    body = {
        "summary": payload.summary,
        "description": payload.description,
        "location": payload.location,
        "start": _gcal_time(payload.start),
        "end": _gcal_time(payload.end),
    }
    try:
        created = get_calendar_service().events().insert(calendarId=_calendar_id(), body=body).execute()
    except HttpError as exc:
        raise HTTPException(status_code=exc.resp.status, detail=exc._get_reason()) from None
    return {"id": created.get("id"), "summary": created.get("summary"), "htmlLink": created.get("htmlLink")}


@app.patch("/calendar/events/{event_id}")
def update_event(payload: EventUpdate, event_id: str = ApiPath(pattern=EVENT_ID_PATTERN)) -> dict[str, Any]:
    body: dict[str, Any] = payload.model_dump(exclude_none=True, exclude={"start", "end"})
    if payload.start is not None:
        body["start"] = _gcal_time(payload.start)
    if payload.end is not None:
        body["end"] = _gcal_time(payload.end)
    try:
        updated = get_calendar_service().events().patch(calendarId=_calendar_id(), eventId=event_id, body=body).execute()
    except HttpError as exc:
        raise HTTPException(status_code=exc.resp.status, detail=exc._get_reason()) from None
    return {"id": updated.get("id"), "summary": updated.get("summary"), "htmlLink": updated.get("htmlLink")}


@app.delete("/calendar/events/{event_id}")
def delete_event(event_id: str = ApiPath(pattern=EVENT_ID_PATTERN)) -> dict[str, str]:
    try:
        get_calendar_service().events().delete(calendarId=_calendar_id(), eventId=event_id).execute()
    except HttpError as exc:
        raise HTTPException(status_code=exc.resp.status, detail=exc._get_reason()) from None
    return {"status": "deleted", "id": event_id}
