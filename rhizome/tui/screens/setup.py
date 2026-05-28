"""First-run setup screen: collects user name and API key."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Input, Static

from rhizome.credentials import store_api_key
from rhizome.tui.options import Options
from rhizome.tui.widgets.legacy.welcome import ASCII_ART


class SetupScreen(Screen[bool]):
    """Two-step setup wizard: name → API key."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=False),
    ]

    DEFAULT_CSS = """
    SetupScreen {
        align: center middle;
    }

    #setup-container {
        width: 70;
        height: auto;
        align: center middle;
        padding: 1 2;
    }

    #setup-art {
        content-align: center middle;
        width: 1fr;
        height: auto;
        color: $text-muted;
    }

    .setup-heading {
        text-align: center;
        width: 1fr;
        height: auto;
        color: $text;
        margin-top: 1;
        margin-bottom: 1;
    }

    .setup-hint {
        text-align: center;
        width: 1fr;
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }

    .setup-input {
        width: 1fr;
        margin: 0 4;
        background: transparent;
    }

    #setup-error {
        text-align: center;
        width: 1fr;
        height: auto;
        color: $error;
        margin-top: 1;
        display: none;
    }

    #step-name {
        height: auto;
    }

    #step-key {
        height: auto;
        display: none;
    }
    """

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="setup-container"):
                yield Static(ASCII_ART, id="setup-art")

                with Vertical(id="step-name"):
                    yield Static(
                        "Welcome to Rhizome! What should I call you?",
                        classes="setup-heading",
                    )
                    yield Static(
                        "Press Enter to continue (leave blank to skip)",
                        classes="setup-hint",
                    )
                    yield Input(
                        placeholder="Your name",
                        id="name-input",
                        classes="setup-input",
                    )

                with Vertical(id="step-key"):
                    yield Static(
                        "Let's set up your Anthropic API key",
                        classes="setup-heading",
                    )
                    yield Static(
                        "Get one at console.anthropic.com/settings/keys",
                        classes="setup-hint",
                    )
                    yield Input(
                        placeholder="sk-ant-...",
                        password=True,
                        id="key-input",
                        classes="setup-input",
                    )
                    yield Static("", id="setup-error")

    def on_mount(self) -> None:
        self.query_one("#name-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "name-input":
            self._advance_to_key_step(event.value.strip())
        elif event.input.id == "key-input":
            self._submit_key(event.value.strip())

    def _advance_to_key_step(self, name: str) -> None:
        if name:
            self.app.options._values[Options.UserName.resolved_name] = name
            self.app.options.flush()
        self.query_one("#step-name").styles.display = "none"
        self.query_one("#step-key").styles.display = "block"
        self.query_one("#key-input", Input).focus()

    def _submit_key(self, key: str) -> None:
        if not key:
            return
        error_widget = self.query_one("#setup-error", Static)
        error_widget.styles.display = "none"
        self.run_worker(self._validate_and_store(key), exclusive=True)

    async def _validate_and_store(self, key: str) -> None:
        error_widget = self.query_one("#setup-error", Static)
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=key)
            await client.models.list(limit=1)
        except Exception as exc:
            exc_type = type(exc).__name__
            # AuthenticationError → reject; anything else (network) → accept
            if "AuthenticationError" in exc_type or (
                hasattr(exc, "status_code") and getattr(exc, "status_code", 0) == 401
            ):
                error_widget.update("Invalid API key. Please try again.")
                error_widget.styles.display = "block"
                self.query_one("#key-input", Input).focus()
                return

        store_api_key("anthropic", key)
        self.dismiss(True)

    def action_go_back(self) -> None:
        step_key = self.query_one("#step-key")
        if step_key.styles.display != "none":
            step_key.styles.display = "none"
            self.query_one("#step-name").styles.display = "block"
            self.query_one("#name-input", Input).focus()
        else:
            self.dismiss(False)
