"""`xscout` CLI: key generation, account management, database check."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import click
from sqlalchemy import text

from xscout.config import PROJECT_DIR, Settings, get_settings
from xscout.crypto import SecretBox, SecretBoxError
from xscout.log import redactor
from xscout.store.accounts import AccountError, AccountRepository, parse_cookie_string
from xscout.store.db import make_engine, make_sessionmaker
from xscout.store.models import AccountStatus


def _settings() -> Settings:
    try:
        return get_settings()
    except Exception as e:  # pydantic ValidationError carries the readable reason
        raise click.ClickException(f"configuration error: {e}") from e


def _run(fn: Callable[[AccountRepository], Awaitable[Any]]) -> Any:
    settings = _settings()

    async def main() -> Any:
        engine = make_engine(settings.database_url)
        try:
            box = SecretBox(settings.secret_key.get_secret_value())
            return await fn(AccountRepository(make_sessionmaker(engine), box))
        finally:
            await engine.dispose()

    try:
        return asyncio.run(main())
    except (AccountError, SecretBoxError) as e:
        raise click.ClickException(str(e)) from e


@click.group()
def cli() -> None:
    """xscout — self-hosted X scraping service."""


@cli.command()
@click.option("--write-env", is_flag=True, help="Store the key in .env instead of printing it.")
def keygen(write_env: bool) -> None:
    """Create a new Fernet key for XSCOUT_SECRET_KEY."""
    key = SecretBox.generate_key()
    if not write_env:
        click.echo(key)
        return
    env_file = PROJECT_DIR / ".env"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    for i, line in enumerate(lines):
        if line.startswith("XSCOUT_SECRET_KEY="):
            if line.split("=", 1)[1].strip():
                raise click.ClickException("XSCOUT_SECRET_KEY is already set in .env; refusing to overwrite it")
            lines[i] = f"XSCOUT_SECRET_KEY={key}"
            break
    else:
        lines.append(f"XSCOUT_SECRET_KEY={key}")
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    click.echo(f"XSCOUT_SECRET_KEY written to {env_file} (mode 600)")


@cli.group()
def db() -> None:
    """Database helpers."""


@db.command("check")
def db_check() -> None:
    """Connect to the database and show the migration revision."""
    settings = _settings()

    async def main() -> str:
        engine = make_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                try:
                    rev = await conn.scalar(text("select version_num from alembic_version"))
                except Exception:
                    rev = None
                return f"connected; alembic revision: {rev or 'none (run: uv run alembic upgrade head)'}"
        finally:
            await engine.dispose()

    click.echo(asyncio.run(main()))


@cli.group()
def accounts() -> None:
    """Manage X accounts (cookies are stored encrypted)."""


def _print_view(view: Any) -> None:
    click.echo(json.dumps(asdict(view), default=str, indent=2))


@accounts.command("add")
@click.argument("username")
@click.option("--with-proxy", is_flag=True, help="Prompt (hidden) for a proxy URL for this account.")
@click.option("--replace", is_flag=True, help="Replace cookies of an existing account.")
def accounts_add(username: str, with_proxy: bool, replace: bool) -> None:
    """Add an account. Cookies are read from a hidden prompt, never from arguments."""
    raw = click.prompt(
        "Paste cookies as `auth_token=...; ct0=...` (or press Enter to type them one by one)",
        hide_input=True,
        default="",
        show_default=False,
    )
    cookies = parse_cookie_string(raw) if raw else {}
    auth_token = cookies.get("auth_token") or click.prompt("auth_token", hide_input=True)
    ct0 = cookies.get("ct0") or click.prompt("ct0", hide_input=True)
    proxy = click.prompt("proxy URL (http://user:pass@host:port)", hide_input=True) if with_proxy else None
    redactor.register(auth_token, ct0, proxy)
    profiles = _settings().tunables.pool.impersonate_profiles
    view = _run(lambda repo: repo.add(username, auth_token, ct0, profiles, proxy=proxy, replace=replace))
    proxy_state = "yes" if view.has_proxy else "no"
    click.echo(f"account {view.username} saved (impersonate={view.impersonate}, proxy={proxy_state})")


@accounts.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def accounts_list(as_json: bool) -> None:
    """List accounts (never shows cookies)."""
    views = _run(lambda repo: repo.list())
    if as_json:
        click.echo(json.dumps([asdict(v) for v in views], default=str, indent=2))
        return
    if not views:
        click.echo("no accounts")
        return
    click.echo(f"{'username':<16} {'status':<10} {'profile':<10} {'proxy':<5} {'overflow':<8} last_error")
    for v in views:
        click.echo(
            f"{v.username:<16} {v.status:<10} {v.impersonate:<10} {'yes' if v.has_proxy else 'no':<5} "
            f"{'yes' if v.allow_overflow else 'no':<8} {v.last_error or ''}"
        )


@accounts.command("disable")
@click.argument("username")
@click.option("--reason", default="disabled manually")
def accounts_disable(username: str, reason: str) -> None:
    """Stop using an account."""
    _print_view(_run(lambda repo: repo.set_status(username, AccountStatus.DISABLED, reason)))


@accounts.command("enable")
@click.argument("username")
def accounts_enable(username: str) -> None:
    """Put an account back into rotation."""
    _print_view(_run(lambda repo: repo.set_status(username, AccountStatus.ACTIVE, "enabled manually")))


@accounts.command("overflow")
@click.argument("username")
@click.argument("state", type=click.Choice(["on", "off"]))
def accounts_overflow(username: str, state: str) -> None:
    """Allow or forbid the alternative-bearer overflow buckets for an account."""
    _print_view(_run(lambda repo: repo.set_overflow(username, state == "on")))


@accounts.command("remove")
@click.argument("username")
@click.confirmation_option(prompt="Remove this account and its stored cookies?")
def accounts_remove(username: str) -> None:
    """Delete an account and its cookies."""
    removed = _run(lambda repo: repo.remove(username))
    click.echo("removed" if removed else "not found")


def _register_dev_commands() -> None:
    from xscout.cli.xdev import pool_cmd, x

    cli.add_command(x)
    cli.add_command(pool_cmd)


_register_dev_commands()

if __name__ == "__main__":
    cli()
