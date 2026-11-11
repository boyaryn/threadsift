import re

# Only controls that REVEAL more replies may be clicked. Labels are matched whole
# (after lower-casing and collapsing spaces), so an action such as "Reply" never
# matches "View replies". These patterns are starting points to be confirmed
# against the real interface during the manual live check.
REVEAL_PATTERNS = [
    r"view (all )?(\d+ )?(more )?repl(y|ies)",
    r"(show|see|load) (all )?(\d+ )?(more )?repl(y|ies)",
    r"view (all )?(\d+ )?more",
    r"переглянути (всі )?(ще )?(\d+ )?відповід(ь|і|ей)",
    r"показати (всі )?(ще )?(\d+ )?відповід(ь|і|ей)",
    r"посмотреть (все )?(еще |ещё )?(\d+ )?ответ(а|ы|ов)?",
    r"показать (все )?(еще |ещё )?(\d+ )?ответ(а|ы|ов)?",
]
_REVEAL = [re.compile(pattern) for pattern in REVEAL_PATTERNS]


class ForbiddenControl(Exception):
    """The control is not on the allow-list of content-revealing controls."""

    def __init__(self, label: str) -> None:
        super().__init__(f"refusing to click a control that is not a reveal control: {label!r}")
        self.label = label


def normalize_label(label: str) -> str:
    return " ".join(label.casefold().split())


def is_reveal_label(label: str) -> bool:
    normalized = normalize_label(label)
    return any(pattern.fullmatch(normalized) for pattern in _REVEAL)
