import pytest

from threadsift.collection.guard import (
    BlockedError,
    check_page,
    check_response,
    check_url,
    is_graphql_url,
)

GRAPHQL = "https://www.threads.com/api/graphql"
POST = "https://www.threads.com/@someone/post/ABC123"


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://www.threads.com/login", "login wall"),
        ("https://www.threads.com/login/?next=%2F%40u%2Fpost%2FA", "login wall"),
        ("https://www.threads.com/accounts/login/", "login wall"),
        ("https://www.threads.com/challenge/", "challenge"),
        ("https://www.threads.com/accounts/challenge/?next=/", "challenge"),
        ("https://www.threads.com/checkpoint/1501092823525282/", "challenge"),
        ("https://www.threads.com/accounts/suspended/", "suspended"),
        ("https://www.threads.com/accounts/disabled/", "suspended"),
    ],
)
def test_redirects_to_blocking_pages_are_detected(url: str, kind: str) -> None:
    with pytest.raises(BlockedError) as blocked:
        check_url(url)
    assert blocked.value.kind == kind
    assert blocked.value.reason == f"blocked: {kind}"


@pytest.mark.parametrize(
    "url",
    [
        POST,
        "https://www.threads.com/",
        "https://www.threads.com/@login/post/ABC",
        "https://www.threads.com/@challenge",
        "https://www.threads.com/search?q=login",
        "https://www.threads.com/@someone/post/ABC123?next=/login",
    ],
)
def test_normal_pages_pass(url: str) -> None:
    check_url(url)
    check_page(url)


def test_login_form_means_logged_out() -> None:
    with pytest.raises(BlockedError) as blocked:
        check_page(POST, login_form_present=True)
    assert blocked.value.reason == "blocked: login wall"


@pytest.mark.parametrize(
    ("dialog", "kind"),
    [
        ("We suspect automated behavior. Please confirm it's you.", "challenge"),
        ("Підозріла активність на вашому акаунті", "challenge"),
        ("Необычная активность", "challenge"),
        ("Please try again later", "rate limited"),
        ("Спробуйте пізніше", "rate limited"),
        ("Log in to continue", "login wall"),
    ],
)
def test_blocking_dialogs_are_detected(dialog: str, kind: str) -> None:
    with pytest.raises(BlockedError) as blocked:
        check_page(POST, dialog_text=dialog)
    assert blocked.value.kind == kind


def test_page_without_dialogs_passes() -> None:
    check_page(POST, login_form_present=False, dialog_text="")


@pytest.mark.parametrize(("status", "kind"), [(429, "rate limited"), (403, "forbidden"), (401, "login wall")])
def test_graphql_http_errors_are_detected(status: int, kind: str) -> None:
    with pytest.raises(BlockedError) as blocked:
        check_response(GRAPHQL, status, None)
    assert blocked.value.kind == kind


@pytest.mark.parametrize("status", [429, 403, 401])
def test_http_errors_from_other_hosts_and_paths_are_ignored(status: int) -> None:
    check_response("https://scontent.cdninstagram.com/v/t51/pic.jpg", status)
    check_response("https://www.threads.com/ajax/bz", status)


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ({"errors": [{"message": "Rate limit exceeded"}]}, "rate limited"),
        ({"error": "rate_limited"}, "rate limited"),
        ({"errors": [{"extensions": {"code": "CHALLENGE_REQUIRED"}}]}, "challenge"),
        ({"message": "checkpoint_required", "status": "fail"}, "challenge"),
        ({"error_type": "login_required"}, "login wall"),
        ([{"errors": [{"message": "feedback_required"}]}], "action restricted"),
    ],
)
def test_graphql_error_payloads_are_detected(body, kind: str) -> None:
    with pytest.raises(BlockedError) as blocked:
        check_response(GRAPHQL, 200, body)
    assert blocked.value.kind == kind


def test_normal_graphql_payload_passes() -> None:
    check_response(GRAPHQL, 200, {"data": {"data": {"edges": [{"node": {"text": "rate limit exceeded, try again later"}}]}}})
    check_response(GRAPHQL, 200, None)
    check_response(GRAPHQL, 200, {"data": None})


def test_graphql_url_detection() -> None:
    assert is_graphql_url("https://www.threads.com/api/graphql")
    assert is_graphql_url("https://www.threads.com/graphql/query?x=1")
    assert not is_graphql_url(POST)


def test_blocked_error_message_names_what_was_detected() -> None:
    error = BlockedError("challenge", "redirected to /challenge/")
    assert "blocked: challenge" in str(error) and "/challenge/" in str(error)
