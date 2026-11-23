from datetime import datetime, timedelta, timezone
import hmac
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path as ApiPath, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import httpx
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
EVENT_ID_PATTERN = r"^[a-zA-Z0-9_-]{5,1024}$"

app = FastAPI(title="Personal Bridge API", version="1.1.0")
bearer_scheme = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> None:
    expected = (
        os.environ.get("API_TOKEN")
        or os.environ.get("PERSONAL_API_TOKEN")
        or os.environ.get("BRIDGE_TOKEN")
        or ""
    ).strip()
    if not expected:
        raise HTTPException(status_code=500, detail="Server API token is not configured")
    if credentials is None or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def get_calendar_service():
    sa_path = Path(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", str(BASE_DIR / "service-account.json"))
    )
    if not sa_path.is_absolute():
        sa_path = BASE_DIR / sa_path
    if not sa_path.exists():
        raise HTTPException(status_code=500, detail=f"Service account file not found: {sa_path}")
    creds = service_account.Credentials.from_service_account_file(str(sa_path), scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _calendar_id() -> str:
    cal_id = os.environ.get("CALENDAR_ID", "").strip()
    if not cal_id:
        raise HTTPException(status_code=500, detail="CALENDAR_ID is not configured")
    return cal_id


def _configured_calendars() -> list[tuple[str, str]]:
    raw = os.environ.get("CALENDAR_IDS", "").strip()
    if not raw:
        return [("Primary", _calendar_id())]
    calendars: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            label, cid = entry.split(":", 1)
            calendars.append((label.strip(), cid.strip()))
        else:
            calendars.append((entry, entry))
    return calendars


def _gcal_time(value: str) -> dict[str, str]:
    return {"date": value} if len(value) == 10 else {"dateTime": value}


class EventCreate(BaseModel):
    summary: str
    start: str
    end: str
    description: str = ""
    location: str = ""


class EventUpdate(BaseModel):
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    description: str | None = None
    location: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/calendar/events", dependencies=[Depends(verify_token)])
def list_events(
    days_ahead: int = 7,
    days_behind: int = 0,
    max_results: int = 30,
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=max(0, days_behind))).isoformat()
    time_max = (now + timedelta(days=max(1, min(days_ahead, 90)))).isoformat()
    service = get_calendar_service()

    merged: list[dict[str, Any]] = []
    for label, cal_id in _configured_calendars():
        try:
            res = (
                service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=min(max_results, 100),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except HttpError as exc:
            if cal_id == _calendar_id():
                raise HTTPException(status_code=exc.resp.status, detail=exc._get_reason()) from None
            continue

        for e in res.get("items", []):
            merged.append(
                {
                    "id": e.get("id"),
                    "calendar": label,
                    "summary": e.get("summary", "(No title)"),
                    "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
                    "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
                    "location": e.get("location", ""),
                    "description": e.get("description", ""),
                }
            )

    merged.sort(key=lambda item: item["start"] or "")
    return merged


@app.post("/calendar/events", dependencies=[Depends(verify_token)])
def create_event(payload: EventCreate, calendar_id: str | None = None) -> dict[str, Any]:
    body = {
        "summary": payload.summary,
        "description": payload.description,
        "location": payload.location,
        "start": _gcal_time(payload.start),
        "end": _gcal_time(payload.end),
    }
    try:
        created = (
            get_calendar_service()
            .events()
            .insert(calendarId=calendar_id or _calendar_id(), body=body)
            .execute()
        )
    except HttpError as exc:
        raise HTTPException(status_code=exc.resp.status, detail=exc._get_reason()) from None
    return {"id": created.get("id"), "summary": created.get("summary"), "htmlLink": created.get("htmlLink")}


@app.patch("/calendar/events/{event_id}", dependencies=[Depends(verify_token)])
def update_event(
    payload: EventUpdate,
    event_id: str = ApiPath(pattern=EVENT_ID_PATTERN),
    calendar_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = payload.model_dump(exclude_none=True, exclude={"start", "end"})
    if payload.start is not None:
        body["start"] = _gcal_time(payload.start)
    if payload.end is not None:
        body["end"] = _gcal_time(payload.end)
    try:
        updated = (
            get_calendar_service()
            .events()
            .patch(calendarId=calendar_id or _calendar_id(), eventId=event_id, body=body)
            .execute()
        )
    except HttpError as exc:
        raise HTTPException(status_code=exc.resp.status, detail=exc._get_reason()) from None
    return {"id": updated.get("id"), "summary": updated.get("summary"), "htmlLink": updated.get("htmlLink")}


@app.delete("/calendar/events/{event_id}", dependencies=[Depends(verify_token)])
def delete_event(
    event_id: str = ApiPath(pattern=EVENT_ID_PATTERN),
    calendar_id: str | None = None,
) -> dict[str, str]:
    try:
        get_calendar_service().events().delete(
            calendarId=calendar_id or _calendar_id(),
            eventId=event_id,
        ).execute()
    except HttpError as exc:
        raise HTTPException(status_code=exc.resp.status, detail=exc._get_reason()) from None
    return {"status": "deleted", "id": event_id}


@app.get("/trello/cards", dependencies=[Depends(verify_token)])
def list_trello_cards(member: str | None = None) -> list[dict[str, Any]]:
    api_key = os.environ.get("TRELLO_API_KEY", "").strip()
    token = os.environ.get("TRELLO_TOKEN", "").strip()
    board_id = os.environ.get("TRELLO_BOARD_ID", "").strip()
    if not (api_key and token and board_id):
        raise HTTPException(status_code=500, detail="Trello credentials or TRELLO_BOARD_ID missing")

    auth = {"key": api_key, "token": token}
    base = f"https://api.trello.com/1/boards/{board_id}"

    try:
        with httpx.Client(timeout=15.0) as client:
            cards_resp = client.get(
                f"{base}/cards/open",
                params={**auth, "fields": "name,desc,due,dueComplete,idList,idMembers,url,labels"},
            )
            cards_resp.raise_for_status()
            lists_resp = client.get(f"{base}/lists/open", params={**auth, "fields": "name"})
            lists_resp.raise_for_status()
            members_resp = client.get(f"{base}/members", params={**auth, "fields": "username,fullName"})
            members_resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="Trello API error") from None
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Trello request failed: {exc}") from None

    list_map = {item["id"]: item["name"] for item in lists_resp.json()}
    member_map = {item["id"]: item.get("username", "") for item in members_resp.json()}

    target_member = member.strip().lstrip("@").lower() if member else None
    results: list[dict[str, Any]] = []

    for card in cards_resp.json():
        usernames = [member_map[mid] for mid in card.get("idMembers", []) if mid in member_map]
        if target_member and not any(u.lower() == target_member for u in usernames):
            continue
        results.append(
            {
                "id": card.get("id"),
                "name": card.get("name"),
                "list": list_map.get(card.get("idList", ""), "Unknown"),
                "due": card.get("due"),
                "dueComplete": card.get("dueComplete", False),
                "members": usernames,
                "labels": [lbl.get("name") or lbl.get("color") for lbl in card.get("labels", [])],
                "url": card.get("url"),
            }
        )

    return results
