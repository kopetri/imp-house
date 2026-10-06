# imp-house

Home-camera relay and snapshot-based character video generation. The FastAPI
service is the camera's only MJPEG client; it relays live video to the browser
and CYD, captures scheduled snapshots, and processes each snapshot through
Gemini scene analysis, FLUX Fill reference-image generation, and silent Seedance
video generation. Generated MP4s are transcoded to MJPEG for CYD playback. The
React single-page app is built into the same Docker image. Media lives under
`/data`; PostgreSQL is external.

Prompts contain only the character's visual description. Gemini chooses a
placement box and writes an action grounded in the captured room image; the app
turns that box into a feathered inpainting mask. Seedance generates a five-second
720p clip from the FLUX reference image. Existing video captures and their files
remain available as archived, read-only clips and are not reprocessed.

## Local development

Python commands use `uv`.

```sh
uv sync
export DATABASE_URL=sqlite+aiosqlite:///./local.db
export SECRET_KEY="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DATA_DIR=./local-data
uv run alembic upgrade head
uv run python -m imp_house
```

For the web UI, run `npm ci && npm run dev` in `frontend/`; Vite proxies API
requests to the backend on port 8000. Run backend checks with `uv run pytest`
and `uv run ruff check .`.

## Synology Docker

Copy `env.example` to `.env` and set `DATABASE_URL`, `SECRET_KEY`,
`REPLICATE_API_KEY`, and `CAMERA_URL`. Generate a strong `SECRET_KEY`; never
commit `.env`. The PostgreSQL database and role must already exist and accept
connections from the Docker host. For browser access through the DSM HTTPS
reverse proxy, set `COOKIE_SECURE=true`; use `false` only when browser access is
plain HTTP. The CYD can continue using plain HTTP on the trusted LAN. Set
`DATA_PATH` to the persistent clip-data directory. For the current NAS, use
`/volume1/imp-house`, not the project directory at `/volume1/docker/imp-house`.
Set `PUID` and `PGID` to the directory owner's numeric UID and GID (`1026:100`
for the current share). From the project directory run:

```sh
docker compose build
docker compose up -d
```

The container applies Alembic migrations at startup and stores clips in the
mounted data directory. Building on the Synology avoids needing a Docker Hub
image or a cross-architecture image push. Place it behind the DSM HTTPS reverse
proxy for browser access; the CYD currently uses plain HTTP on the trusted LAN.