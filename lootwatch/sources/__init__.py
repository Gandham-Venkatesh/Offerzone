"""Where deals come from: Telegram channel previews, RSS/Atom feeds and store pages."""

from bs4 import BeautifulSoup

try:  # lxml is much faster when it is available (it is not on a bare Termux install)
    import lxml  # noqa: F401

    _PARSER = "lxml"
except Exception:  # pragma: no cover - depends on the platform
    _PARSER = "html.parser"


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, _PARSER)
