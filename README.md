# personal-bridge

Self-hosted FastAPI bridge (`server.py`) and zero-dependency Python CLI (`cli.py`) for read-only Trello and read/write Google Calendar access via a Google Cloud Service Account.

## Server Setup (Desktop)
1. `cp .env.example .env && chmod 600 .env` and fill in `PERSONAL_API_TOKEN`, `TRELLO_API_KEY`, `TRELLO_TOKEN`, `TRELLO_BOARD_ID`, and `CALENDAR_ID`.
2. Place your Google Cloud Service Account JSON key at `./service-account.json` (`chmod 600 service-account.json`) and share your calendar with its `client_email` ("Make changes to events").
3. `uv sync`
4. `uv run uvicorn server:app --host <SERVER_LAN_IP> --port 8765`

## Client Setup (Raspberry Pi / Hermes)
1. Clone this repository into `~/.hermes/personal-bridge` (mounted at `/opt/data/personal-bridge`).
2. `cp .env.example .env && chmod 600 .env` and set `PERSONAL_API_URL` and `PERSONAL_API_TOKEN`.
3. Run `/opt/data/personal-bridge/cli.py trello` or `/opt/data/personal-bridge/cli.py cal list --days 7`.
