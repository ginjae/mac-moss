from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class NoteStore:
    def __init__(self, database: Path) -> None:
        database = database.resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.data_dir = database.parent
        self.lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    media_type TEXT,
                    source_path TEXT NOT NULL,
                    normalized_path TEXT,
                    status TEXT NOT NULL,
                    duration REAL,
                    chunk_count INTEGER NOT NULL DEFAULT 1,
                    processed_chunks INTEGER NOT NULL DEFAULT 0,
                    bridge_count INTEGER NOT NULL DEFAULT 0,
                    processed_bridges INTEGER NOT NULL DEFAULT 0,
                    progress_phase TEXT NOT NULL DEFAULT 'queued',
                    progress_completed_steps INTEGER NOT NULL DEFAULT 0,
                    progress_total_steps INTEGER NOT NULL DEFAULT 0,
                    processed_audio_seconds REAL DEFAULT 0,
                    language TEXT,
                    hotwords TEXT,
                    raw_transcript TEXT,
                    raw_segments_json TEXT NOT NULL DEFAULT '[]',
                    segments_json TEXT NOT NULL DEFAULT '[]',
                    speaker_names_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(notes)").fetchall()
            }
            if "chunk_count" not in columns:
                connection.execute(
                    "ALTER TABLE notes ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 1"
                )
            if "processed_chunks" not in columns:
                connection.execute(
                    "ALTER TABLE notes ADD COLUMN processed_chunks INTEGER NOT NULL DEFAULT 0"
                )
            migrations = {
                "raw_segments_json": "TEXT NOT NULL DEFAULT '[]'",
                "bridge_count": "INTEGER NOT NULL DEFAULT 0",
                "processed_bridges": "INTEGER NOT NULL DEFAULT 0",
                "progress_phase": "TEXT NOT NULL DEFAULT 'queued'",
                "progress_completed_steps": "INTEGER NOT NULL DEFAULT 0",
                "progress_total_steps": "INTEGER NOT NULL DEFAULT 0",
                "processed_audio_seconds": "REAL DEFAULT 0",
                "model_progress_json": "TEXT NOT NULL DEFAULT '{}'",
                "processing_started_at": "TEXT",
                "processing_finished_at": "TEXT",
                "processing_seconds": "REAL",
                "chunk_seconds": "INTEGER",
                "preprocessing_seconds": "REAL NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE notes ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS prepared_uploads (
                    id TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    media_type TEXT,
                    source_path TEXT NOT NULL,
                    normalized_path TEXT,
                    duration REAL,
                    status TEXT NOT NULL DEFAULT 'preparing',
                    preprocessing_seconds REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            if "progress_phase" not in columns:
                # Old completed notes are fully done. Their historical bridge
                # counts were not recorded, so do not invent those counts.
                connection.execute(
                    """
                    UPDATE notes SET
                        progress_phase = CASE status
                            WHEN 'done' THEN 'done'
                            WHEN 'error' THEN 'error'
                            ELSE 'queued'
                        END,
                        progress_total_steps = CASE WHEN status = 'done'
                            THEN MAX(chunk_count, 1) + 1 ELSE 0 END,
                        progress_completed_steps = CASE WHEN status = 'done'
                            THEN MAX(chunk_count, 1) + 1 ELSE 0 END,
                        processed_audio_seconds = CASE WHEN status = 'done'
                            THEN duration ELSE 0 END
                    """
                )
            # Preserve the ASR result of existing notes before future manual edits.
            connection.execute(
                """
                UPDATE notes SET raw_segments_json = segments_json
                WHERE status = 'done'
                  AND (raw_segments_json IS NULL OR raw_segments_json = '[]')
                """
            )
            for row in connection.execute(
                "SELECT id, source_path, normalized_path FROM notes"
            ).fetchall():
                paths = {}
                for field in ("source_path", "normalized_path"):
                    try:
                        paths[field] = self.portable_path(row["id"], row[field])
                    except ValueError:
                        # A broken note must not prevent other notes loading.
                        # Its file operation will report the invalid path.
                        paths[field] = row[field]
                source_path = paths["source_path"]
                normalized_path = paths["normalized_path"]
                if (source_path, normalized_path) != (
                    row["source_path"], row["normalized_path"]
                ):
                    connection.execute(
                        "UPDATE notes SET source_path = ?, normalized_path = ? WHERE id = ?",
                        (source_path, normalized_path, row["id"]),
                    )

    def upload_directory(self, note_id: str) -> Path:
        if not note_id or note_id in {".", ".."} or Path(note_id).name != note_id:
            raise ValueError("올바르지 않은 노트 파일 경로입니다.")
        expected = self.data_dir / "uploads" / note_id
        directory = expected.resolve()
        if directory != expected:
            raise ValueError("노트 파일 경로가 저장 폴더를 벗어납니다.")
        return directory

    def portable_path(self, note_id: str, value: str | Path | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.relative_to(self.data_dir)
            except ValueError:
                # Legacy managed uploads belong to this checkout even if an
                # old copy remains or a file is missing. Preserve other paths.
                if path.parts[-4:-1] == ("data", "uploads", note_id):
                    candidate = self.upload_directory(note_id) / path.name
                    path = candidate.relative_to(self.data_dir)
                else:
                    return str(path)
        resolved = (self.data_dir / path).resolve()
        directory = self.upload_directory(note_id)
        if resolved == directory or not resolved.is_relative_to(directory):
            raise ValueError("노트 파일 경로가 전용 저장 폴더를 벗어납니다.")
        return resolved.relative_to(self.data_dir).as_posix()

    def resolve_path(self, note_id: str, value: str | Path) -> Path:
        path = Path(self.portable_path(note_id, value))
        resolved = (path if path.is_absolute() else self.data_dir / path).resolve()
        directory = self.upload_directory(note_id)
        if resolved == directory or not resolved.is_relative_to(directory):
            raise ValueError("노트 파일 경로가 전용 저장 폴더를 벗어납니다.")
        return resolved

    def _insert_note(self, connection: sqlite3.Connection, note: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        values = {
            "normalized_path": None,
            "duration": None,
            "chunk_seconds": None,
            "chunk_count": 1,
            "processed_chunks": 0,
            "bridge_count": 0,
            "processed_bridges": 0,
            "progress_phase": "queued",
            "progress_completed_steps": 0,
            "progress_total_steps": 0,
            "processed_audio_seconds": 0.0,
            "model_progress_json": "{}",
            "processing_started_at": None,
            "processing_finished_at": None,
            "processing_seconds": None,
            "preprocessing_seconds": 0.0,
            "raw_transcript": None,
            "raw_segments_json": "[]",
            "segments_json": "[]",
            "speaker_names_json": "{}",
            "error": None,
            "created_at": now,
            "updated_at": now,
            **note,
        }
        for field in ("source_path", "normalized_path"):
            values[field] = self.portable_path(note["id"], values[field])
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO notes ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )

    def create(self, note: dict[str, Any]) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            self._insert_note(connection, note)
        return self.get(note["id"])

    def create_upload(self, upload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        values = {
            "normalized_path": None, "duration": None, "status": "preparing",
            "preprocessing_seconds": 0.0, "error": None,
            "created_at": now, "updated_at": now, **upload,
        }
        for field in ("source_path", "normalized_path"):
            values[field] = self.portable_path(upload["id"], values[field])
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO prepared_uploads ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        return self.get_upload(upload["id"])

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM prepared_uploads WHERE id = ?", (upload_id,)).fetchone()
        if row is None:
            raise KeyError(upload_id)
        return dict(row)

    def list_uploads(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM prepared_uploads").fetchall()
        return [dict(row) for row in rows]

    def update_upload(self, upload_id: str, **changes: Any) -> dict[str, Any]:
        for field in ("source_path", "normalized_path"):
            if field in changes:
                changes[field] = self.portable_path(upload_id, changes[field])
        changes["updated_at"] = datetime.now(UTC).isoformat()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self.lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE prepared_uploads SET {assignments} WHERE id = ?",
                (*changes.values(), upload_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(upload_id)
        return self.get_upload(upload_id)

    def discard_upload(self, upload_id: str) -> None:
        with self.lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM prepared_uploads WHERE id = ?", (upload_id,))
            if cursor.rowcount == 0:
                raise KeyError(upload_id)

    def consume_upload(self, upload_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        # Promotion and removal share a transaction so a double submission
        # cannot create or queue the same recording twice.
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM prepared_uploads WHERE id = ?", (upload_id,)).fetchone()
            if row is None:
                raise KeyError(upload_id)
            upload = dict(row)
            if upload["status"] != "ready":
                raise ValueError("전처리가 완료된 후 전사를 시작해 주세요.")
            self._insert_note(connection, {
                "id": upload_id, "original_filename": upload["original_filename"],
                "media_type": upload["media_type"], "source_path": upload["source_path"],
                "normalized_path": upload["normalized_path"], "duration": upload["duration"],
                "preprocessing_seconds": upload["preprocessing_seconds"],
                "status": "queued", **settings,
            })
            connection.execute("DELETE FROM prepared_uploads WHERE id = ?", (upload_id,))
        return self.get(upload_id)

    def get(self, note_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise KeyError(note_id)
        return self._deserialize(row)

    def list(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notes ORDER BY created_at DESC"
            ).fetchall()
        return [self._deserialize(row) for row in rows]

    def queued_count(self) -> int:
        with self.lock, self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM notes WHERE status = 'queued'").fetchone()[0]

    def pending_ids(self) -> list[str]:
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM notes WHERE status IN ('queued', 'processing') ORDER BY created_at"
            ).fetchall()
            connection.execute(
                """
                UPDATE notes SET status = 'queued',
                    processed_chunks = 0, bridge_count = 0, processed_bridges = 0,
                    progress_phase = 'queued', progress_completed_steps = 0,
                    progress_total_steps = 0, processed_audio_seconds = 0,
                    model_progress_json = '{}', processing_started_at = NULL,
                    processing_finished_at = NULL, processing_seconds = NULL
                WHERE status IN ('queued', 'processing')
                """
            )
        return [row["id"] for row in rows]

    def update(self, note_id: str, **changes: Any) -> dict[str, Any]:
        if not changes:
            return self.get(note_id)
        json_fields = {
            "raw_segments": "raw_segments_json",
            "segments": "segments_json",
            "speaker_names": "speaker_names_json",
            "model_progress": "model_progress_json",
        }
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            if key in {"source_path", "normalized_path"}:
                value = self.portable_path(note_id, value)
            target = json_fields.get(key, key)
            normalized[target] = (
                json.dumps(value, ensure_ascii=False) if key in json_fields else value
            )
        normalized["updated_at"] = datetime.now(UTC).isoformat()
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        with self.lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE notes SET {assignments} WHERE id = ?",
                (*normalized.values(), note_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(note_id)
        return self.get(note_id)

    def delete(self, note_id: str) -> dict[str, Any]:
        note = self.get(note_id)
        with self.lock, self._connect() as connection:
            connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return note

    @staticmethod
    def _deserialize(row: sqlite3.Row) -> dict[str, Any]:
        # Archived columns in older databases stay on disk, but are not part
        # of the transcription-only note model.
        result = {
            key: value
            for key, value in dict(row).items()
            if not key.startswith("correction_") and key != "corrected_segments_json"
        }
        result["raw_segments"] = json.loads(result.pop("raw_segments_json") or "[]")
        result["segments"] = json.loads(result.pop("segments_json") or "[]")
        result["speaker_names"] = json.loads(result.pop("speaker_names_json") or "{}")
        result["model_progress"] = json.loads(result.pop("model_progress_json", "{}") or "{}")
        return result
