"""User-facing record labels, independent of immutable scheduling tags."""
from __future__ import annotations

import re
import sqlite3
import uuid

from .common import now


def _error(status, message):
    from .hub import APIError
    return APIError(status, message)


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise _error(400, "无效的标签或记录标识")
    return value


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS record_tags (
            id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            color TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS job_tags (
            record_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES record_tags(id) ON DELETE CASCADE,
            PRIMARY KEY(record_id,tag_id));
        CREATE TABLE IF NOT EXISTS matrix_tags (
            record_id TEXT NOT NULL REFERENCES matrices(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES record_tags(id) ON DELETE CASCADE,
            PRIMARY KEY(record_id,tag_id));
        CREATE INDEX IF NOT EXISTS job_tags_tag ON job_tags(tag_id);
        CREATE INDEX IF NOT EXISTS matrix_tags_tag ON matrix_tags(tag_id);
    """)


class RecordTagsHubMixin:
    def record_tags(self):
        with self.lock:
            return {"tags": [dict(row) for row in self.db.execute(
                "SELECT t.*, (SELECT COUNT(*) FROM job_tags WHERE tag_id=t.id) AS job_count, "
                "(SELECT COUNT(*) FROM matrix_tags mt JOIN matrices m ON m.id=mt.record_id "
                "WHERE mt.tag_id=t.id AND m.deleted_at IS NULL) AS matrix_count "
                "FROM record_tags t ORDER BY t.name COLLATE NOCASE,t.id")]}

    def _attach_record_tags(self, records, target):
        # One join per collection, rather than a separate query for every record.
        by_id = {record["id"]: record for record in records}
        for record in records:
            record["tags"] = []
        if not by_id:
            return records
        table = {"job": "job_tags", "matrix": "matrix_tags"}[target]
        # Detail views should not scan the labels of every historical experiment.
        where, parameters = "", ()
        if len(by_id) <= 400:
            where = "WHERE r.record_id IN (" + ",".join("?" for _ in by_id) + ") "
            parameters = tuple(by_id)
        for row in self.db.execute(
                f"SELECT r.record_id,t.* FROM {table} r JOIN record_tags t ON t.id=r.tag_id "
                + where + "ORDER BY t.name COLLATE NOCASE,t.id", parameters):
            if row["record_id"] in by_id:
                tag = dict(row)
                tag.pop("record_id")
                by_id[row["record_id"]]["tags"].append(tag)
        for record in records:
            record["tag_ids"] = [tag["id"] for tag in record["tags"]]
        return records

    def _validate_tag_ids(self, values, check_exists=True):
        if not isinstance(values, list) or len(values) > 32:
            raise _error(400, "每条记录最多选择 32 个标签")
        identities = sorted({_identity(value) for value in values})
        if identities and check_exists:
            placeholders = ",".join("?" for _ in identities)
            found = {row[0] for row in self.db.execute(
                f"SELECT id FROM record_tags WHERE id IN ({placeholders})", identities)}
            if found != set(identities):
                raise _error(400, "部分标签已被删除，请刷新标签后重试")
        return identities

    def _set_record_tags(self, target, identity, tag_ids):
        table = {"job": "job_tags", "matrix": "matrix_tags"}[target]
        self.db.execute(f"DELETE FROM {table} WHERE record_id=?", (identity,))
        self.db.executemany(f"INSERT INTO {table}(record_id,tag_id) VALUES (?,?)",
                            [(identity, tag_id) for tag_id in tag_ids])

    def tag_save(self, payload):
        if not isinstance(payload, dict):
            raise _error(400, "标签必须是对象")
        name, color = payload.get("name"), payload.get("color", "#246657")
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 40
                or any(ord(char) < 32 for char in name)):
            raise _error(400, "标签名称应为 1–40 个字符，且不能包含控制字符")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise _error(400, "标签颜色应为 #RRGGBB 格式")
        name, color = name.strip(), color.lower()
        with self.transaction():
            identity, timestamp = payload.get("id"), now()
            if "id" in payload:
                _identity(identity)
                if not self.db.execute("SELECT 1 FROM record_tags WHERE id=?", (identity,)).fetchone():
                    raise _error(404, "此标签已被删除")
            else:
                identity = uuid.uuid4().hex
            try:
                self.db.execute(
                    "INSERT INTO record_tags(id,name,color,created,updated) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,color=excluded.color,updated=excluded.updated",
                    (identity, name, color, timestamp, timestamp))
            except sqlite3.IntegrityError as error:
                raise _error(409, "已有同名标签，请使用其他名称") from error
            return dict(self.db.execute("SELECT * FROM record_tags WHERE id=?", (identity,)).fetchone())

    def tag_delete(self, payload):
        if not isinstance(payload, dict):
            raise _error(400, "请求必须是对象")
        identity = _identity(payload.get("id"))
        with self.transaction():
            # Deletion is idempotent; foreign keys remove record associations.
            self.db.execute("DELETE FROM record_tags WHERE id=?", (identity,))
        return {"id": identity, "deleted": True}

    def tag_assign(self, payload):
        if not isinstance(payload, dict) or payload.get("target") not in ("job", "matrix"):
            raise _error(400, "请选择实验记录或实验矩阵")
        target, identity = payload["target"], _identity(payload.get("id"))
        with self.transaction():
            self._find_job(identity) if target == "job" else self._matrix_find(identity)
            tag_ids = self._validate_tag_ids(payload.get("tag_ids"))
            self._set_record_tags(target, identity, tag_ids)
            return self._attach_record_tags([{"id": identity}], target)[0]
