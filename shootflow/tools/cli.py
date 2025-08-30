# language: python
import logging
from typing import Optional, List
from pathlib import Path
from enum import Enum

import typer

from shootflow.shared.logger import setup_basic_logging
from shootflow.shared.config import settings
from shootflow.shared.s3 import S3Storage
from shootflow.tools.get_instagram import main as get_instagram_main

app = typer.Typer(help="ShootFlow CLI: ingest helpers and utilities.")


class Profile(str, Enum):
    development = "development"
    production = "production"


def _version_callback(value: bool):
    if value:
        try:
            from shootflow import __version__ as v  # provided by package init
        except Exception:
            v = "0.1.0"
        typer.echo(f"ShootFlow {v}")
        raise typer.Exit()


@app.callback()
def common(
    verbose: int = typer.Option(
        0,
        "--verbose",
        "-v",
        count=True,
        help="Increase verbosity: -v INFO, -vv DEBUG",
    ),
    profile: Profile = typer.Option(
        Profile.development,
        "--profile",
        "-p",
        help="Dynaconf profile/environment to use (development or production). Defaults to development.",
    ),
    version: Optional[bool] = typer.Option(None, "--version", callback=_version_callback, is_eager=True),
):
    """Common options for all commands."""
    # Switch Dynaconf environment early so all subsequent settings reflect the chosen profile
    try:
        # settings is a Dynaconf instance; setenv() switches the active environment at runtime
        # Convert Enum to its string value
        chosen_profile = profile.value if isinstance(profile, Enum) else str(profile)
        settings.setenv(chosen_profile)
    except Exception:
        # Fallback: keep current environment if switching fails for any reason
        pass

    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    setup_basic_logging(level)


@app.command("s3-upload", help="Upload files to S3 ready/ so ingest can process them.")
def s3_upload(
    paths: List[Path] = typer.Argument(..., exists=True, readable=True, dir_okay=False, help="Image files to upload"),
    bucket: Optional[str] = typer.Option(None, "--bucket", help="Target S3 bucket (defaults to settings.S3_BUCKET)"),
):
    bkt = bucket or settings.S3_BUCKET
    if not bkt:
        typer.secho("S3_BUCKET not configured (pass --bucket or set settings)", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    s3 = S3Storage()
    success = 0
    errors = 0

    for p in paths:
        try:
            key = s3.upload_to_ready(str(p), bkt)
            success += 1
            typer.secho(f"uploaded s3://{bkt}/{key}", fg=typer.colors.GREEN)
        except Exception as e:
            errors += 1
            typer.secho(f"upload_failed path={p} error={e}", fg=typer.colors.RED, err=True)

    if errors and not success:
        raise typer.Exit(code=1)


@app.command("instagram-pick", help="Select and download top-scored Flickr image; mark as POSTED for Instagram queue.")
def instagram_pick():
    # Delegate to existing tool to preserve current behavior
    get_instagram_main()


@app.command("flickr-public-sync", help="Sync your public Flickr photos into DynamoDB as POSTED items.")
def flickr_public_sync(
    start_date: Optional[str] = typer.Option(None, "--start-date", help="Start date (ISO8601 or epoch)")
    , end_date: Optional[str] = typer.Option(None, "--end-date", help="End date (ISO8601 or epoch)")
    , by: str = typer.Option("uploaded", "--by", help='Date field: "uploaded" or "taken"')
    , per_page: int = typer.Option(500, "--per-page", help="Flickr API page size (default 500)")
):
    from shootflow.handlers import flickr_public_sync as fps
    event = {"by": by, "per_page": per_page}
    if start_date is not None:
        event["start_date"] = start_date
    if end_date is not None:
        event["end_date"] = end_date
    result = fps.handler(event, None)
    typer.secho(str(result), fg=typer.colors.GREEN)


@app.command("flickr-score-refresh", help="Recompute Flickr engagement score for all items and update DynamoDB.")
def flickr_score_refresh(
    page_limit: int = typer.Option(1000, "--page-limit", help="DynamoDB scan page size"),
    max_items: Optional[int] = typer.Option(None, "--max-items", help="Process at most this many items"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute but do not write updates (overrides settings)"),
):
    # The handler reads dry_run from settings, so override it here if flag provided
    from shootflow.handlers import flickr_score_refresher as fsr
    previous = getattr(settings, "FLICKR_SCORE_REFRESH_DRY_RUN", False)
    try:
        if dry_run:
            setattr(settings, "FLICKR_SCORE_REFRESH_DRY_RUN", True)
        event = {"page_limit": page_limit}
        if max_items is not None:
            event["max_items"] = max_items
        result = fsr.handler(event, None)
    finally:
        # Restore previous value to avoid leaking across commands/tests
        setattr(settings, "FLICKR_SCORE_REFRESH_DRY_RUN", previous)
    typer.secho(str(result), fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
