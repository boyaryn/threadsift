from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from threadsift.intake.queue import UrlQueue

MONDAY = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TUESDAY = MONDAY + timedelta(days=1)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(MONDAY)


@pytest.fixture
def queue(clean_engine: Engine, clock: Clock) -> UrlQueue:
    return UrlQueue(clean_engine, clock=clock)


def url(code: str, user: str = "user") -> str:
    return f"https://www.threads.com/@{user}/post/{code}"


def write(tmp_path: Path, name: str, *lines: str) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def rows(engine: Engine, where: str = "true") -> list[dict]:
    with engine.connect() as connection:
        result = connection.execute(text(f"SELECT * FROM url_queue WHERE {where} ORDER BY id"))
        return [dict(r) for r in result.mappings()]


# --- 5.2 import ----------------------------------------------------------

def test_new_urls_are_added_as_pending(queue, clean_engine, tmp_path) -> None:
    report = queue.import_file(write(tmp_path, "batch-01.txt", url("A1"), url("B2"), url("C3")))
    assert (report.added, report.already_pending, report.refresh_requested) == (3, 0, 0)
    assert report.rejected == []
    stored = rows(clean_engine)
    assert [r["state"] for r in stored] == ["pending"] * 3
    assert [r["code"] for r in stored] == ["A1", "B2", "C3"]
    assert stored[0]["canonical_url"] == url("A1")


def test_comments_and_blank_lines_are_not_rejections(queue, tmp_path) -> None:
    path = write(tmp_path, "f.txt", "", "# a comment", url("A1"), "   ", "  # indented comment", url("B2"))
    report = queue.import_file(path)
    assert report.added == 2
    assert report.rejected == []


def test_rejected_lines_are_listed_with_reason_and_not_queued(queue, clean_engine, tmp_path) -> None:
    path = write(
        tmp_path, "f.txt",
        "# header",
        "https://www.threads.com/@someuser",
        url("A1"),
        "https://example.com/x",
        "hello",
    )
    report = queue.import_file(path)
    assert report.added == 1
    assert [(r.line_number, r.text) for r in report.rejected] == [
        (2, "https://www.threads.com/@someuser"),
        (4, "https://example.com/x"),
        (5, "hello"),
    ]
    assert "not a post URL" in report.rejected[0].reason
    assert len(rows(clean_engine)) == 1


def test_variants_in_one_import_are_one_entry(queue, clean_engine, tmp_path) -> None:
    path = write(
        tmp_path, "f.txt",
        "https://threads.net/@user/post/ABC123?igshid=x",
        "https://www.threads.com/@user/post/ABC123/",
    )
    report = queue.import_file(path)
    assert (report.added, report.already_pending) == (1, 1)
    assert len(rows(clean_engine)) == 1


def test_pending_url_imported_again_stays_one_pending_entry(queue, clean_engine, tmp_path) -> None:
    queue.import_file(write(tmp_path, "one.txt", url("A1")))
    report = queue.import_file(write(tmp_path, "two.txt", url("A1"), url("B2")))
    assert (report.added, report.already_pending, report.refresh_requested) == (1, 1, 0)
    assert len(rows(clean_engine)) == 2


@pytest.mark.parametrize("state", ["done", "partial", "failed"])
def test_relisting_a_processed_url_requests_a_refresh(queue, clean_engine, tmp_path, state) -> None:
    queue.import_file(write(tmp_path, "one.txt", url("A1")))
    queue.record_result(rows(clean_engine)[0]["id"], state, reason=None if state == "done" else "why")

    report = queue.import_file(write(tmp_path, "two.txt", url("A1")))

    assert (report.added, report.already_pending, report.refresh_requested) == (0, 0, 1)
    stored = rows(clean_engine)
    assert len(stored) == 1
    assert stored[0]["state"] == "pending"
    assert stored[0]["reason"] is None


def test_source_and_first_queued_are_recorded_and_kept(queue, clean_engine, clock, tmp_path) -> None:
    queue.import_file(write(tmp_path, "batch-01.txt", url("A1")))
    stored = rows(clean_engine)[0]
    assert stored["source"] == "batch-01.txt"
    assert stored["first_queued_at"] == MONDAY

    queue.record_result(stored["id"], "done")
    clock.now = TUESDAY
    queue.import_file(write(tmp_path, "batch-02.txt", url("A1")))
    again = rows(clean_engine)[0]
    assert again["source"] == "batch-01.txt"
    assert again["first_queued_at"] == MONDAY


def test_import_is_all_or_nothing(queue, clean_engine, tmp_path) -> None:
    class Boom(Exception):
        pass

    def entries():
        from threadsift.intake.source import read_url_file

        yield from read_url_file(write(tmp_path, "f.txt", url("A1")))
        raise Boom

    with pytest.raises(Boom):
        queue.import_entries(entries(), source="f.txt")
    assert rows(clean_engine) == []


def test_file_with_byte_order_mark_and_cyrillic_comment(queue, tmp_path) -> None:
    path = tmp_path / "f.txt"
    path.write_bytes(b"\xef\xbb\xbf" + "# Сонячна енергетика\n".encode() + url("A1").encode() + b"\n")
    report = queue.import_file(path)
    assert (report.added, report.rejected) == (1, [])


# --- 5.3 pending picker and state ---------------------------------------

def test_oldest_pending_urls_go_first_and_the_rest_stay_pending(queue, clean_engine, clock, tmp_path) -> None:
    queue.import_file(write(tmp_path, "mon.txt", url("MON1"), url("MON2")))
    clock.now = TUESDAY
    queue.import_file(write(tmp_path, "tue.txt", url("TUE1"), url("TUE2")))

    picked = queue.pending(limit=3)
    assert [u.code for u in picked] == ["MON1", "MON2", "TUE1"]
    assert picked[0].canonical_url == url("MON1")
    assert [r["state"] for r in rows(clean_engine)] == ["pending"] * 4
    assert [u.code for u in queue.pending(limit=10)] == ["MON1", "MON2", "TUE1", "TUE2"]


def test_only_pending_urls_are_picked(queue, clean_engine, tmp_path) -> None:
    queue.import_file(write(tmp_path, "f.txt", url("A"), url("B"), url("C"), url("D")))
    ids = {r["code"]: r["id"] for r in rows(clean_engine)}
    queue.record_result(ids["A"], "done")
    queue.record_result(ids["B"], "partial", reason="cap: depth 5")
    queue.record_result(ids["C"], "failed", reason="not found")
    assert [u.code for u in queue.pending(limit=10)] == ["D"]


def test_failed_url_records_reason_and_attempt_time(queue, clean_engine, clock, tmp_path) -> None:
    queue.import_file(write(tmp_path, "f.txt", url("A")))
    clock.now = TUESDAY
    queue.record_result(rows(clean_engine)[0]["id"], "failed", reason="not found")
    stored = rows(clean_engine)[0]
    assert stored["state"] == "failed"
    assert stored["reason"] == "not found"
    assert stored["last_attempt_at"] == TUESDAY


def test_stopped_url_goes_back_to_pending_with_reason(queue, clean_engine, tmp_path) -> None:
    queue.import_file(write(tmp_path, "f.txt", url("A")))
    queue.record_result(rows(clean_engine)[0]["id"], "pending", reason="blocked: challenge")
    stored = rows(clean_engine)[0]
    assert (stored["state"], stored["reason"]) == ("pending", "blocked: challenge")
    assert [u.code for u in queue.pending(limit=5)] == ["A"]


def test_done_clears_reason_and_links_the_post(queue, clean_engine, tmp_path) -> None:
    queue.import_file(write(tmp_path, "f.txt", url("A")))
    with clean_engine.begin() as connection:
        connection.execute(text("INSERT INTO authors (id, username) VALUES ('u', 'user')"))
        connection.execute(text(
            "INSERT INTO posts (id, code, url, author_id, first_text, latest_text) "
            "VALUES ('p1', 'A', 'https://x', 'u', 't', 't')"
        ))
    url_id = rows(clean_engine)[0]["id"]
    queue.record_result(url_id, "pending", reason="blocked: challenge")
    queue.record_result(url_id, "done", post_id="p1")
    stored = rows(clean_engine)[0]
    assert (stored["state"], stored["reason"], stored["post_id"]) == ("done", None, "p1")
