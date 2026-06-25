"""ViewBase: the ``request_scroll_visible`` seam wiring.

``ViewModelBase.request_scroll_visible(top)`` emits ``RequestScrollVisible``; ``ViewBase`` defers a
``Widget.scroll_visible(top=...)`` to after the next refresh (the target may have only just mounted).
Here we pin the *wiring* — that the deferred call fires and carries ``top`` through — by recording the
call rather than asserting on real layout (that belongs to Textual).
"""

from textual.app import App, ComposeResult

from rhizome.app.model import ViewModelBase
from rhizome.tui.widgets.view_base import ViewBase


class _Probe(ViewBase[ViewModelBase]):
    """Records scroll_visible calls instead of really scrolling, isolating the ViewBase wiring."""

    def __init__(self, vm: ViewModelBase) -> None:
        super().__init__(vm)
        self.scroll_tops: list[bool | None] = []

    def scroll_visible(self, *args, **kwargs):   # type: ignore[override]
        self.scroll_tops.append(kwargs.get("top"))


class _ProbeApp(App):
    def __init__(self, vm: ViewModelBase) -> None:
        super().__init__()
        self._vm = vm

    def compose(self) -> ComposeResult:
        yield _Probe(self._vm)


async def test_request_scroll_visible_defers_scroll_with_top_true():
    vm = ViewModelBase()
    app = _ProbeApp(vm)
    async with app.run_test() as pilot:
        vm.request_scroll_visible(top=True)
        await pilot.pause()                       # let call_after_refresh fire
        assert app.query_one(_Probe).scroll_tops == [True]


async def test_request_scroll_visible_passes_top_false_through():
    vm = ViewModelBase()
    app = _ProbeApp(vm)
    async with app.run_test() as pilot:
        vm.request_scroll_visible(top=False)
        await pilot.pause()
        assert app.query_one(_Probe).scroll_tops == [False]


async def test_default_top_is_true():
    vm = ViewModelBase()
    app = _ProbeApp(vm)
    async with app.run_test() as pilot:
        vm.request_scroll_visible()               # top defaults to True
        await pilot.pause()
        assert app.query_one(_Probe).scroll_tops == [True]
