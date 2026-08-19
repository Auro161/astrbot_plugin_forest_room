import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import ForestDB


def test_save_and_find_mapping_with_image(tmp_path):
    db = ForestDB(tmp_path / "test.db")
    assert db.save_room_key_mapping("10001", 111, 222, "ABC123", "u1", image_msg_id=333) is True
    mapping = db.find_room_key_mapping("10001", 111)
    assert mapping is not None
    assert mapping["reply_msg_id"] == 222
    assert mapping["image_msg_id"] == 333


def test_find_mapping_without_image(tmp_path):
    db = ForestDB(tmp_path / "test.db")
    db.save_room_key_mapping("10001", 111, 222, "ABC123", "u1")
    mapping = db.find_room_key_mapping("10001", 111)
    assert mapping is not None
    assert mapping["reply_msg_id"] == 222
    assert mapping["image_msg_id"] is None


def test_legacy_schema_migration_adds_image_msg_id(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE room_key_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            original_msg_id INTEGER NOT NULL,
            reply_msg_id INTEGER NOT NULL,
            room_key TEXT NOT NULL,
            user_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_room_key_lookup
            ON room_key_mappings(group_id, original_msg_id)""")
        conn.commit()

    db = ForestDB(db_path)

    with sqlite3.connect(db_path) as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(room_key_mappings)")]
    assert "image_msg_id" in cols

    db.save_room_key_mapping("10001", 111, 222, "ABC123", image_msg_id=333)
    mapping = db.find_room_key_mapping("10001", 111)
    assert mapping["image_msg_id"] == 333
