"""Entry point: ``uv run python -m rhizome.tui``."""

from datetime import datetime
from pathlib import Path

import rich_click as click

from rhizome.config import get_default_db_path
from rhizome.db import init_db
from rhizome.tui.app import PROFILE_DIR, RhizomeApp


@click.command()
@click.option(
    "--db",
    default=None,
    type=click.Path(dir_okay=False),
    help="Path to the SQLite database file. [default: platform data dir or $RHIZOME_DB]",
)
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging of agent stream events.")
@click.option(
    "--new-chat-pane",
    is_flag=True,
    default=False,
    help="(temporary) Use the in-progress MVVM chat pane rewrite. Step 1 only — no agent, no commands.",
)
@click.option(
    "--profile",
    is_flag=True,
    default=False,
    help="Wrap the entire session in a pyinstrument profile. Writes HTML to /tmp/rhizome-profiles on exit.",
)
def main(db: str | None, debug: bool, new_chat_pane: bool, profile: bool) -> None:
    """Launch the rhizome TUI."""
    db_path = db or str(get_default_db_path())
    init_db(db_path)
    app = RhizomeApp(db_path=db_path, debug=debug, new_chat_pane=new_chat_pane)

    if not profile:
        app.run()
        return

    from pyinstrument import Profiler

    profiler = Profiler(async_mode="enabled")
    profiler.start()
    try:
        app.run()
    finally:
        profiler.stop()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = PROFILE_DIR / f"profile-startup-{stamp}.html"
        out.write_text(profiler.output_html())
        click.echo(f"Profile written: {out}")


main()
