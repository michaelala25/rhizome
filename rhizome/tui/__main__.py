"""Entry point: ``uv run python -m rhizome.tui``."""

import argparse
from datetime import datetime

import rhizome.tui.graphics as graphics
from rhizome.config import get_default_db_path
from rhizome.db import init_db
from rhizome.tui.app import PROFILE_DIR, RhizomeApp


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rhizome.tui", description="Launch the rhizome TUI.")
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="Path to the SQLite database file. [default: platform data dir or $RHIZOME_DB]",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging of agent stream events.",
    )
    parser.add_argument(
        "--profile", action="store_true",
        help="Wrap the entire session in a pyinstrument profile. Writes HTML to /tmp/rhizome-profiles on exit.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    db_path = args.db or str(get_default_db_path())
    init_db(db_path)

    # Probe the terminal + select a graphics backend BEFORE Textual starts (it needs raw stdin while it
    # is still ours). No backend selected -> the chat falls back to plain Markdown; no error either way.
    graphics.initialize()

    app = RhizomeApp(db_path=db_path, debug=args.debug)

    if not args.profile:
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
        print(f"Profile written: {out}")


main()
