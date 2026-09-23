from collections.abc import Iterable

from app.models import Turn

_LABELS = {"agent": "Agent", "user": "Caller"}


def _has_text(turn: Turn) -> bool:
    return bool(turn.message and turn.message.strip())


def flatten(transcript: Iterable[Turn]) -> str:
    """One `Agent: ...` / `Caller: ...` line per non-empty turn, joined with newlines."""
    return "\n".join(
        f"{_LABELS[t.role]}: {t.message.strip()}"  # type: ignore[union-attr]
        for t in transcript
        if t.role in _LABELS and _has_text(t)
    )


def user_turns(transcript: Iterable[Turn]) -> int:
    return sum(1 for t in transcript if t.role == "user" and _has_text(t))
