import pytest

from threadsift.intake.urls import RejectedUrl, normalize_post_url

CANONICAL = "https://www.threads.com/@user/post/ABC123"


@pytest.mark.parametrize(
    "line",
    [
        "https://www.threads.com/@user/post/ABC123",
        "https://threads.com/@user/post/ABC123",
        "https://www.threads.net/@user/post/ABC123",
        "https://threads.net/@user/post/ABC123",
        "http://threads.net/@user/post/ABC123",
        "https://threads.net/@user/post/ABC123?igshid=x",
        "https://www.threads.com/@user/post/ABC123/",
        "https://www.threads.com/@user/post/ABC123/?xmt=a&slof=1#frag",
        "  https://www.threads.com/@user/post/ABC123  ",
        "HTTPS://WWW.THREADS.COM/@user/post/ABC123",
    ],
)
def test_variants_normalize_to_one_canonical_url(line: str) -> None:
    normalized = normalize_post_url(line)
    assert normalized.code == "ABC123"
    assert normalized.canonical == CANONICAL


def test_igshid_and_trailing_slash_example_share_a_key() -> None:
    a = normalize_post_url("https://threads.net/@user/post/ABC123?igshid=x")
    b = normalize_post_url("https://www.threads.com/@user/post/ABC123/")
    assert a == b


def test_renamed_username_keeps_the_same_dedup_key() -> None:
    old = normalize_post_url("https://www.threads.com/@old_name/post/ABC123")
    new = normalize_post_url("https://www.threads.com/@new.name/post/ABC123")
    assert old.code == new.code == "ABC123"
    assert old.username != new.username


def test_shortcode_case_is_preserved() -> None:
    assert normalize_post_url("https://www.threads.com/@u/post/AbC_d-E").code == "AbC_d-E"


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("https://www.threads.com/@someuser", "not a post URL"),
        ("https://www.threads.com/@someuser/", "not a post URL"),
        ("https://www.threads.com/", "not a post URL"),
        ("https://www.threads.com/@u/post/", "not a post URL"),
        ("https://www.threads.com/@u/post/ABC/extra", "not a post URL"),
        ("https://www.threads.com/search?q=solar", "not a post URL"),
        ("https://www.instagram.com/p/ABC123/", "not a Threads URL"),
        ("https://example.com/@u/post/ABC", "not a Threads URL"),
        ("https://threads.com.evil.example/@u/post/ABC", "not a Threads URL"),
        ("https://evil.example/https://threads.com/@u/post/ABC", "not a Threads URL"),
        ("ftp://threads.com/@u/post/ABC", "not a URL"),
        ("just some text", "not a URL"),
        ("threads.com/@u/post/ABC", "not a URL"),
        ("", "not a URL"),
    ],
)
def test_non_post_urls_are_rejected_with_a_reason(line: str, reason: str) -> None:
    with pytest.raises(RejectedUrl, match=reason):
        normalize_post_url(line)
