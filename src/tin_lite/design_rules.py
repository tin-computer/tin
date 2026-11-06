"""Tin's rules for everything it makes that someone looks at: films and their sketches, figures,
embeds, pages and boards, for every project. Every procedure's brief carries them, and the film
kit measures them on every frame (web/film/film.js, `eyebrows`)."""

NO_EYEBROWS = (
    "No eyebrows, anywhere. Never put a small label above a title, a heading, a card's words or "
    'a number: no uppercase, letter-spaced or mono kicker such as "HOW IT WORKS", "MENTIONS", '
    '"SAMPLE ANSWER · CLAUDE" or a quiet "needs you". It is a stock generated-design habit. '
    "Let the title or the content say it; when a qualifier is needed, put it in the sentence, or "
    "after the content in the same type. Table column headers and a product's own screens shown "
    "as they are stay as they are."
)

RULES = (NO_EYEBROWS,)


def instruction() -> str:
    """The rules as a brief's closing section."""
    return "\n\nDESIGN RULES (Tin's, for anything you make that someone looks at):\n" + "\n".join(
        f"- {rule}" for rule in RULES
    )
