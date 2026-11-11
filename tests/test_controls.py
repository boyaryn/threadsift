import pytest

from threadsift.collection.controls import is_reveal_label


@pytest.mark.parametrize(
    "label",
    [
        "View replies",
        "View more replies",
        "View 3 more replies",
        "View all 12 replies",
        "view  more   replies",
        "Show more replies",
        "See 5 more replies",
        "Load more replies",
        "Переглянути відповіді",
        "Переглянути ще 3 відповіді",
        "Показати ще відповіді",
        "Посмотреть ответы",
        "Показать еще 4 ответа",
    ],
)
def test_reveal_controls_are_allowed(label: str) -> None:
    assert is_reveal_label(label)


@pytest.mark.parametrize(
    "label",
    [
        "Like", "Unlike", "Follow", "Following", "Unfollow", "Reply", "Repost", "Quote",
        "Share", "Save", "Bookmark", "Vote", "Send", "Post", "Block", "Mute", "Report",
        "More", "Log in", "Delete", "Hide", "Restrict", "Add to story",
        "Reply to bob", "Like this reply", "Repost this reply", "Follow bob",
        "Подобається", "Відповісти", "Підписатися", "Ответить", "Подписаться",
        "", "   ",
        "View replies and follow",
        "Please view replies",
    ],
)
def test_action_controls_are_refused(label: str) -> None:
    assert not is_reveal_label(label)
