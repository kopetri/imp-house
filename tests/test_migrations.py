import sqlite3
import uuid
from pathlib import Path

from alembic.config import Config

from alembic import command


def test_snapshot_migration_preserves_legacy_rows(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "10636aa1b671")

    clip_id = str(uuid.uuid4())
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO prompts "
            "(id, name, text, model, video_input_key, prompt_input_key, extra_input, is_active, "
            "created_at, updated_at) "
            "VALUES (1, 'Archive', 'old character', 'owner/model', 'video', 'prompt', '{}', 1, "
            "'2026-10-04 12:00:00', '2026-10-04 12:00:00')"
        )
        connection.execute(
            "INSERT INTO clips "
            "(id, recorded_at, duration_seconds, status, original_path, thumbnail_path, prompt_id, "
            "prompt_name, prompt_text, model, model_input, attempts, created_at, updated_at) "
            "VALUES (?, '2026-10-04 12:00:00+00:00', 12, 'done', 'clips/old/original.mp4', "
            "'clips/old/thumb.jpg', 1, 'Archive', 'old character', 'owner/model', '{}', 1, "
            "'2026-10-04 12:00:00', '2026-10-04 12:00:00')",
            (clip_id,),
        )
        connection.execute(
            "INSERT INTO recording_settings "
            "(id, enabled, clips_per_day, window_start, window_end, min_seconds, max_seconds) "
            "VALUES (1, 1, 3, '08:00:00', '20:00:00', 10, 20)"
        )
        connection.execute(
            "INSERT INTO scheduled_recordings "
            "(id, plan_date, scheduled_at, duration_seconds, status, clip_id) "
            "VALUES (1, '2026-10-04', '2026-10-04 12:00:00+00:00', 15, 'done', ?)",
            (clip_id,),
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        clip = connection.execute(
            "SELECT original_path, duration_seconds, prompt_text, snapshot_path, pipeline_stage "
            "FROM clips WHERE id = ?",
            (clip_id,),
        ).fetchone()
        assert clip == ("clips/old/original.mp4", 12.0, "old character", None, None)

        schedule = connection.execute(
            "SELECT status, clip_id FROM scheduled_recordings WHERE id = 1"
        ).fetchone()
        assert schedule == ("done", clip_id)

        clip_columns = {row[1]: row[3] for row in connection.execute("PRAGMA table_info(clips)")}
        assert clip_columns["original_path"] == 0
        assert "provider_output" in clip_columns

        prompt_columns = {row[1] for row in connection.execute("PRAGMA table_info(prompts)")}
        assert {"name", "text", "is_active"} <= prompt_columns
        assert "model" not in prompt_columns

        schedule_columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_recordings)")}
        assert "duration_seconds" not in schedule_columns

    command.downgrade(config, "10636aa1b671")

    with sqlite3.connect(database_path) as connection:
        prompt_columns = {row[1]: row[3] for row in connection.execute("PRAGMA table_info(prompts)")}
        clip_columns = {row[1]: row[3] for row in connection.execute("PRAGMA table_info(clips)")}
        assert prompt_columns["model"] == 1
        assert prompt_columns["extra_input"] == 1
        assert clip_columns["original_path"] == 1
        assert connection.execute("SELECT original_path FROM clips WHERE id = ?", (clip_id,)).fetchone() == (
            "clips/old/original.mp4",
        )
