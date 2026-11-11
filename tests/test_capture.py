import json
from pathlib import Path

import pytest

from threadsift.collection.capture import ResponseCapture, extract_embedded_json
from threadsift.collection.guard import BlockedError
from threadsift.collection.monitor import EarlyWarning, Monitor
from threadsift.config import EarlyWarningConfig

from tests.fakes import FakeNetResponse, FakePage, Timeline

GRAPHQL = "https://www.threads.com/api/graphql"
POST_URL = "https://www.threads.com/@someone/post/ABC123"

HTML = """<html><head>
<script type="application/json" data-sjs>{"require": [["post", {"pk": "1", "caption": {"text": "Нарешті ☀️"}}]]}</script>
<script type="text/javascript">var x = {"not": "captured"};</script>
<script type="application/json">this is not json</script>
<script type="application/json">{"second": [1, 2, 3]}</script>
</head><body>hello</body></html>"""


def make(tmp_path: Path | None = None, monitor: Monitor | None = None):
    page = FakePage(Timeline())
    capture = ResponseCapture(page, monitor, save_dir=tmp_path)
    return page, capture


def test_embedded_json_is_extracted_and_junk_skipped() -> None:
    found = extract_embedded_json(HTML)
    assert found == [
        {"require": [["post", {"pk": "1", "caption": {"text": "Нарешті ☀️"}}]]},
        {"second": [1, 2, 3]},
    ]


def test_html_without_embedded_json() -> None:
    assert extract_embedded_json("<html><body>nothing</body></html>") == []


def test_graphql_and_embedded_page_data_are_buffered_in_order() -> None:
    page, capture = make()
    capture.begin_url()
    page.emit("response", FakeNetResponse(POST_URL, 200, html=HTML))
    page.emit("response", FakeNetResponse(GRAPHQL, 200, {"data": {"replies": []}}))
    payloads = capture.end_url()
    assert [(p.kind, p.url) for p in payloads] == [
        ("embedded", POST_URL), ("embedded", POST_URL), ("graphql", GRAPHQL),
    ]
    assert payloads[2].data == {"data": {"replies": []}}


def test_page_data_outside_a_url_is_dropped() -> None:
    page, capture = make()
    page.emit("response", FakeNetResponse("https://www.threads.com/", 200, html=HTML))  # the feed page
    capture.begin_url()
    assert capture.end_url() == []


def test_failed_document_and_non_document_responses_are_ignored() -> None:
    page, capture = make()
    capture.begin_url()
    page.emit("response", FakeNetResponse(POST_URL, 404, html=HTML))
    page.emit("response", FakeNetResponse("https://www.threads.com/static/x.js", 200, None))
    assert capture.end_url() == []


def test_blocking_graphql_response_is_remembered_and_raised_later() -> None:
    page, capture = make()
    capture.begin_url()
    page.emit("response", FakeNetResponse(GRAPHQL, 200, {"errors": [{"message": "challenge_required"}]}))
    with pytest.raises(BlockedError, match="challenge"):
        capture.raise_pending()
    capture.raise_pending()  # raised once only


def test_response_statistics_reach_the_monitor_and_a_warning_is_raised_later() -> None:
    monitor = Monitor(EarlyWarningConfig(failure_window=4, failure_ratio=0.5))
    page, capture = make(monitor=monitor)
    capture.begin_url()
    for status in (200, 500, 500, 500):
        page.emit("response", FakeNetResponse(GRAPHQL, status, {"data": 1} if status == 200 else None))
    with pytest.raises(EarlyWarning, match="failing_responses"):
        capture.raise_pending()


def test_payloads_are_saved_per_url_when_a_directory_is_given(tmp_path: Path) -> None:
    page, capture = make(tmp_path / "out")
    capture.begin_url("ABC123")
    page.emit("response", FakeNetResponse(POST_URL, 200, html=HTML))
    page.emit("response", FakeNetResponse(GRAPHQL, 200, {"data": {"text": "Привіт ☀️"}}))
    capture.end_url()

    folder = tmp_path / "out" / "ABC123"
    assert sorted(p.name for p in folder.iterdir()) == [
        "001-embedded.json", "002-embedded.json", "003-graphql.json",
    ]
    saved = json.loads((folder / "003-graphql.json").read_text(encoding="utf-8"))
    assert saved == {"url": GRAPHQL, "kind": "graphql", "data": {"data": {"text": "Привіт ☀️"}}}
    assert "Привіт ☀️" in (folder / "003-graphql.json").read_text(encoding="utf-8")  # not \u-escaped


def test_nothing_is_saved_without_a_directory_or_outside_a_url(tmp_path: Path) -> None:
    page, capture = make(tmp_path / "out")
    page.emit("response", FakeNetResponse(GRAPHQL, 200, {"feed": True}))  # warm-up
    assert not (tmp_path / "out").exists()

    page2, capture2 = make(None)
    capture2.begin_url("X")
    page2.emit("response", FakeNetResponse(GRAPHQL, 200, {"a": 1}))
    assert len(capture2.end_url()) == 1


def test_second_url_numbers_its_files_again(tmp_path: Path) -> None:
    page, capture = make(tmp_path)
    for label in ("A", "B"):
        capture.begin_url(label)
        page.emit("response", FakeNetResponse(GRAPHQL, 200, {"n": label}))
        capture.end_url()
    assert (tmp_path / "A" / "001-graphql.json").exists()
    assert (tmp_path / "B" / "001-graphql.json").exists()


def test_drain_returns_what_arrived_since_the_last_drain_and_keeps_capturing() -> None:
    page, capture = make()
    capture.begin_url("X")
    page.emit("response", FakeNetResponse(GRAPHQL, 200, {"n": 1}))
    assert [p.data for p in capture.drain()] == [{"n": 1}]
    assert capture.drain() == []
    page.emit("response", FakeNetResponse(GRAPHQL, 200, {"n": 2}))
    assert [p.data for p in capture.end_url()] == [{"n": 2}]


def test_a_warning_says_which_responses_failed_and_how_without_their_contents() -> None:
    monitor = Monitor(EarlyWarningConfig(failure_window=4, failure_ratio=0.5))
    page, capture = make(monitor=monitor)
    capture.begin_url()
    page.emit("response", FakeNetResponse(GRAPHQL, 200, {"data": 1}))
    page.emit("response", FakeNetResponse(
        GRAPHQL, 502, headers={"content-type": "text/plain"}, raw=b"Bad gateway",
        request_headers={"x-fb-friendly-name": "BarcelonaActivityBadgeQuery"}))
    page.emit("response", FakeNetResponse(
        GRAPHQL, 500, post_data="av=17841&fb_api_req_friendly_name=BarcelonaFeedQuery&variables=%7B%22token%22%3A%22secret%22%7D"))
    page.emit("response", FakeNetResponse(GRAPHQL, 500))
    with pytest.raises(EarlyWarning) as caught:
        capture.raise_pending()
    failed = caught.value.details()["failed_responses"]
    assert failed[0] == {"status": 502, "reason": "HTTP 502", "operation": "BarcelonaActivityBadgeQuery",
                         "content_type": "text/plain", "bytes": 11}
    assert failed[1]["operation"] == "BarcelonaFeedQuery" and failed[1]["reason"] == "HTTP 500"
    assert "secret" not in str(failed)
    assert failed[2] == {"status": 500, "reason": "HTTP 500", "bytes": 0}  # nothing readable is left out, not invented


def test_replies_that_are_not_a_single_json_document_do_not_count_as_failures() -> None:
    """The page's background subscriptions and widgets answer 200 with streamed or script bodies
    (seen on the live site); they say nothing about throttling."""
    monitor = Monitor(EarlyWarningConfig(failure_window=4, failure_ratio=0.5))
    page, capture = make(monitor=monitor)
    capture.begin_url()
    page.emit("response", FakeNetResponse(GRAPHQL, 200, {"data": 1}))
    for _ in range(10):
        page.emit("response", FakeNetResponse(GRAPHQL, 200, broken_json=True,
                                              headers={"content-type": "text/javascript"}, raw=b"for (;;);{}"))
    capture.raise_pending()  # nothing was counted, so there is no warning


def test_error_statuses_still_count_when_unreadable_replies_surround_them() -> None:
    monitor = Monitor(EarlyWarningConfig(failure_window=4, failure_ratio=0.5))
    page, capture = make(monitor=monitor)
    capture.begin_url()
    for status in (200, 500, 500, 500):
        page.emit("response", FakeNetResponse(GRAPHQL, 200, broken_json=True))  # ignored
        page.emit("response", FakeNetResponse(GRAPHQL, status, {"data": 1} if status == 200 else None))
    with pytest.raises(EarlyWarning, match="failing_responses"):
        capture.raise_pending()
