"""Shared search helpers for the console reads (P4b-B)."""

ESCAPE = "\\"


def like_escape(value: str) -> str:
    """Make `%`, `_` and the escape character literal in a LIKE pattern, so a
    search box treats them as characters rather than wildcards. Use with
    `escape=ESCAPE`."""
    return (
        value.replace(ESCAPE, ESCAPE + ESCAPE).replace("%", ESCAPE + "%").replace("_", ESCAPE + "_")
    )
