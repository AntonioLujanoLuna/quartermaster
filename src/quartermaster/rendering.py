"""Message rendering bounded by Discord's hard content limit.

Discord rejects any message whose content exceeds 2000 characters, and it
rejects it the same way every time. Nothing in Quartermaster is naturally
bounded: the Party Stash grows for the length of a campaign, character rosters
only accumulate, and a DM can open as many Loot Drops as they like. A surface
that renders all of them eventually crosses the limit and then never renders
again, which is why the bound lives here rather than at each call site.

Two functions, used for two different reasons:

- `fit_discord_lines` is what a list-shaped surface should render through. It
  drops whole lines from the end and says how many it dropped, so a stash that
  outgrows one message still shows the newest entries and tells the reader that
  the rest exist.
- `clamp_discord_content` is the backstop at the send boundary. It guarantees
  the limit for content nobody thought of as a list — an error message quoting a
  long name, an event payload rendered as raw JSON — at the cost of cutting
  mid-line.
"""

from __future__ import annotations

from collections.abc import Sequence

DISCORD_MESSAGE_LIMIT = 2000

_ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"


def clamp_discord_content(text: str, *, limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    """Cut content to the platform limit, marking that something was cut."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + _ELLIPSIS


def _overflow_note(dropped: int, label: str) -> str:
    entries = "entry" if dropped == 1 else "entries"
    return (
        f"{_ELLIPSIS} and {dropped} more {label} {entries} not shown here. "
        "The Quartermaster export holds the full record."
    )


def fit_discord_lines(
    lines: Sequence[str],
    *,
    label: str,
    limit: int = DISCORD_MESSAGE_LIMIT,
) -> str:
    """Join lines into one message, dropping from the end if they do not fit.

    Lines earlier in the sequence survive, so a surface should render its
    heading and its most urgent rows first. The count in the trailing note is
    the number of lines actually dropped, so it stays honest as the note itself
    grows.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    rendered = [clamp_discord_content(line, limit=limit) for line in lines]
    lengths = [len(line) for line in rendered]
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)

    def joined_length(count: int) -> int:
        return prefix[count] + max(count - 1, 0)

    if joined_length(len(rendered)) <= limit:
        return "\n".join(rendered)

    for kept in range(len(rendered) - 1, -1, -1):
        note = _overflow_note(len(rendered) - kept, label)
        candidate_length = len(note) + (joined_length(kept) + 1 if kept else 0)
        if candidate_length <= limit:
            return "\n".join([*rendered[:kept], note])
    # Every line dropped and the note alone still does not fit: the caller's
    # label is pathological, so fall back to the unconditional guarantee.
    return clamp_discord_content(_overflow_note(len(rendered), label), limit=limit)
