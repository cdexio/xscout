"""Fetch x.com pages through an account transport, following X's JS redirect and migrate form."""

from __future__ import annotations

from xscout.transport.session import AccountTransport, PreparedRequest


class PageError(Exception):
    pass


async def fetch_text(transport: AccountTransport, url: str) -> tuple[int, str, str]:
    rep = await transport.get_text(url)
    return rep.status, rep.content_type, rep.text


async def fetch_page(transport: AccountTransport, url: str) -> str:
    """Page HTML; handles the `document.location =` redirect and the x.com/x/migrate form."""
    rep = await transport.get_text(url)
    if rep.status >= 400:
        raise PageError(f"HTTP {rep.status} for {url}")
    if ">document.location =" not in rep.text:
        return rep.text
    url = rep.text.split('document.location = "')[1].split('"')[0]
    rep = await transport.get_text(url)
    if rep.status >= 400:
        raise PageError(f"HTTP {rep.status} for {url}")
    if 'action="https://x.com/x/migrate" method="post"' not in rep.text:
        return rep.text
    import json

    form = {}
    for chunk in rep.text.split("<input")[1:]:
        form[chunk.split('name="')[1].split('"')[0]] = chunk.split('value="')[1].split('"')[0]
    rep = await transport.send(
        PreparedRequest(
            "POST",
            "https://x.com/x/migrate",
            body=json.dumps(form),
            headers={"content-type": "application/json"},
            csrf=False,
        )
    )
    if rep.status >= 400:
        raise PageError(f"HTTP {rep.status} for migrate")
    return rep.text
