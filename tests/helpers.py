from livejobs import db
from livejobs.models import Job as Fetched
from livejobs.services import auth, boards


def mk_user(email="a@x.io", role="user"):
    with db.session() as s:
        return auth.create_user(s, email, "correct-horse-battery", role).id


def mk_board(vendor="greenhouse", slug="acme", enable=True):
    with db.session() as s:
        return boards.add_board(s, vendor, slug, enable).id


def fj(
    title,
    board="acme",
    loc="Remote",
    company="Acme",
    url=None,
    desc="",
    vendor="greenhouse",
    posted="2026-11-01T00:00:00Z",
):
    return Fetched(
        vendor,
        board,
        title,
        company,
        loc,
        url or f"https://x/{title.replace(' ', '-')}",
        posted,
        desc,
    )
