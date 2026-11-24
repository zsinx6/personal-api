# Personal Bridge API (`personal-api`)

Lightweight FastAPI bridge and CLI wrapper that connects a local **Hermes Agent** instance to **Google Calendar** (multi-calendar read + default-calendar write) and **Trello** (read-only) without external SaaS connectors or OAuth browser flows.

## Architecture

- **`server.py`**: FastAPI service running (`0.0.0.0:8765` via `systemd` user service).
  - Authenticates against Google Calendar using a local Google Cloud **Service Account** (`service-account.json`).
  - Merges events across all calendars defined in `CALENDAR_IDS` on `GET /calendar/events`, tagging each event with its calendar label.
  - Creates, updates, and deletes events on `CALENDAR_ID` (or the first calendar in `CALENDAR_IDS` if `CALENDAR_ID` is omitted).
  - Fetches open cards, lists, and member mappings from a configured Trello board on `GET /trello/cards`.
- **`cli.py`**: Zero-dependency standard-library Python CLI mounted into the Hermes container (`/opt/data/personal-api/cli.py`) for sub-second terminal execution.

## Prerequisites & Setup

### 1. Install Dependencies (`uv`)

    uv sync
    # Or add core packages explicitly:
    uv add fastapi uvicorn httpx python-dotenv google-api-python-client google-auth pydantic

### 2. Google Service Account Setup

1. Place your Google Cloud Service Account JSON key at `~/gits/personal-api/service-account.json` (permissions `600`).
2. Find the service account email:

    jq -r '.client_email' ~/gits/personal-api/service-account.json

3. In Google Calendar web settings (**Settings and sharing**):
   - Share your **primary/personal calendar** with the `client_email` using **Make changes to events**.
   - Share any **secondary/work calendars** with the `client_email` using **See all event details** (or **See only free/busy** if restricted by Workspace policy).
   - **Public calendars** (e.g., national holidays) require no explicit sharing—just copy their Calendar ID from **Integrate calendar**.

### 3. Configure `.env`

Create `~/gits/personal-api/.env` (`chmod 600`):

    API_TOKEN=your-strong-random-bearer-token

    # Google Calendar
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/personal-api/service-account.json
    CALENDAR_ID=your.personal@gmail.com
    CALENDAR_IDS=Personal:your.personal@gmail.com,Work:your.work@company.com,Holidays:pt.brazilian#holiday@group.v.calendar.google.com

    # Trello (Read-Only)
    TRELLO_API_KEY=your-trello-api-key
    TRELLO_TOKEN=your-trello-token
    TRELLO_BOARD_ID=your-trello-board-id

> **Note:** `CALENDAR_IDS` accepts comma-separated `Label:calendar_id` entries. `CALENDAR_ID` sets the default target calendar for creating, updating, and deleting events.

### 4. Run as a `systemd` User Service

Unit file (`~/.config/systemd/user/personal-api.service`):

    [Unit]
    Description=Personal Calendar & Trello FastAPI Service
    After=network.target

    [Service]
    Type=simple
    WorkingDirectory=/path/to/personal-api
    ExecStart=/home/user/.local/bin/uv run uvicorn server:app --host 0.0.0.0 --port 8765
    Restart=always
    RestartSec=3

    [Install]
    WantedBy=default.target

Enable and verify:

    systemctl --user daemon-reload
    systemctl --user enable --now personal-api.service
    curl -s http://localhost:8765/health

## CLI Usage (`cli.py`)

`cli.py` reads `API_URL` (e.g., `http://192.168.1.10:8765`) and `API_TOKEN` from `/opt/data/personal-api/.env`.

### Google Calendar

    # List events across all configured calendars for the next 7 days
    python3 /opt/data/personal-api/cli.py cal list --days 7

    # List past 7 days + next 14 days (used in productivity audits)
    python3 /opt/data/personal-api/cli.py cal list --behind 7 --days 14

    # Create a timed event (-03:00 São Paulo timezone)
    python3 /opt/data/personal-api/cli.py cal create \
      --summary "Update APyB instance" \
      --start "2026-11-24T14:30:00-03:00" \
      --end "2026-11-24T15:00:00-03:00"

    # Create an all-day event
    python3 /opt/data/personal-api/cli.py cal create \
      --summary "Renew domain" \
      --start "2026-11-25" \
      --end "2026-11-26"

    # Update an existing event (get ID from `cal list` first)
    python3 /opt/data/personal-api/cli.py cal update \
      --id "EVENT_ID" \
      --summary "Updated title"

    # Delete an event
    python3 /opt/data/personal-api/cli.py cal delete --id "EVENT_ID"

### Trello (Read-Only)

    # List open cards assigned to a specific member
    python3 /opt/data/personal-api/cli.py trello --member zsinx6

    # List all open cards on the board
    python3 /opt/data/personal-api/cli.py trello

## API Endpoints

All endpoints except `/health` require `Authorization: Bearer API_TOKEN`.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Health check (`status: ok`) |
| `GET` | `/calendar/events` | Query `days_ahead`, `days_behind`, `max_results`; returns merged events labeled by `calendar` |
| `POST` | `/calendar/events` | Create event in `CALENDAR_ID` (optional `?calendar_id=...` override) |
| `PATCH` | `/calendar/events/{event_id}` | Update summary, start/end, description, or location |
| `DELETE` | `/calendar/events/{event_id}` | Delete an event by ID |
| `GET` | `/trello/cards` | Query optional `?member=username`; returns open cards with list names and labels |

## Hermes Agent Integration

1. **Timezone:** Ensure `TZ=America/Sao_Paulo` is set in the Hermes container environment (`docker-compose.yml`).
2. **Toolset Pruning (`~/.hermes/config.yaml`):** Restrict `toolsets` to `[terminal, file, skills, memory]`, disable `tools.connectors.enabled` and `tools.tool_search.enabled`, and set `skills.auto_load: [personal-bridge]`.
3. **Skill & Memory:** Keep `~/.hermes/skills/productivity/personal-bridge/SKILL.md` (with a description under 60 characters) and routing/audit rules in `~/.hermes/memories/USER.md`.
