"""``ResourceViewer`` — root view for ``ResourceViewerVM``.

Bones layout: a vertical stack of the panel's regions, with ``alt+up`` / ``alt+down`` focus
orchestration down the focusable chain. The actions bar, loader tree, status box, linker
(search + table + staged-changes Accept/Cancel), and the two preview boxes are all live.

Focus graph (vertical chain)::

    rv-actions
      ↕ alt+up/down
    rv-loader-tree
      ↕ alt+up/down
    rv-linker-search   (the SearchBar forwards this id to its inner field)
      ↕ alt+up/down
    rv-linker-table
      ↕ alt+up/down
    rv-linker-accept   (only reachable while staging is dirty — gated in ``_is_node_available``)

``focus_first`` lands on the actions bar until a topic is set, then on the resource tree. The
non-focusable regions (the two section headers, status, rule, previews, shortcuts row) sit outside
the graph and are skipped.

Dual preview: a top preview (below the tree) tracks the *loader* cursor, a bottom preview (below
the linker) tracks the *linker* cursor. Only one shows at a time — the half of the chain that holds
focus picks it (nodes above the rule → top, below → bottom). The swap is driven from
``on_descendant_focus`` / ``on_descendant_blur`` off the focus node's half (see
``_sync_focus_ui``); each preview is a pure view over its surface VM's ``cursor_target``. That same
focus swap also flips the bottom shortcuts hint's first line to the focused half's keys.
"""

from __future__ import annotations

from rich.text import Text

from textual import on
from textual.actions import SkipAction
from textual.containers import Vertical
from textual.widgets import Rule, Static

from rhizome.app.resource_viewer import ResourceLinkerVM, ResourceViewerVM
from rhizome.tui.widgets.resource_viewer.actions import ResourceViewerActions
from rhizome.tui.widgets.resource_viewer.linker_accept import ResourceLinkerAccept
from rhizome.tui.widgets.resource_viewer.linker_table import ResourceLinkerTable
from rhizome.tui.widgets.resource_viewer.loader_tree import ResourceLoaderTree
from rhizome.tui.widgets.resource_viewer.preview import ResourcePreview
from rhizome.tui.widgets.resource_viewer.status import ResourceStatus
from rhizome.tui.widgets.shared.search_bar import SearchBar
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableViewBase


class ResourceViewer(NavigableViewBase[ResourceViewerVM], FocusOrchestrationMixin):
    """Root view for the resource viewer panel. See module docstring.

    Inherits ``NavigableViewBase`` for the focus-aware border treatment, but narrows it to the right
    edge only: the panel docks to the left of the chat pane, so its border reads as the *divider*
    between the two. ``_sync_focus_border`` is overridden to drive ``border_right`` (the base drives
    all four sides); the default / hover right-border lives in ``DEFAULT_CSS`` below.
    """

    # Both bases want the panel focusable; set it explicitly since the mixin (listed last for CSS
    # aggregation) can be shadowed in MRO. Lets an external ``.focus()`` land here and auto-delegate
    # inward to the loader tree.
    can_focus = True

    DEFAULT_CSS = """
    /* Right-edge divider only — overrides NavigableViewBase's full ``border``. The default/hover
       tones live here; the focus tone is set inline by the ``_sync_focus_border`` override. */
    ResourceViewer {
        layout: vertical;
        background: transparent;
        width: 1fr;
        height: 1fr;
        padding: 0 1 1 1;
        border: none;
        border-right: solid rgb(60, 60, 60);
    }
    /* ``border: none`` has to be repeated here: NavigableViewBase's own ``:hover`` rule sets a full
       ``border`` at this same specificity, so without re-zeroing the other three sides they'd leak
       back in on hover (and reflow the content box). */
    ResourceViewer:hover {
        border: none;
        border-right: solid rgb(120, 120, 120);
    }
    /* Section headers (Resources / Resource Linker): a bold title line over a dim description line,
       both styled inline in the rendered ``Text``. */
    ResourceViewer .rv-section-header {
        height: auto;
        margin-bottom: 1;
        background: transparent;
    }
    /* Auto height: grows to the no-topic prompt, collapses to nothing once a topic is loaded. */
    ResourceViewer #rv-loader-hint {
        height: auto;
        color: #707070;
        padding: 0 1;
        background: transparent;
    }
    /* Box chrome shared by the loader-tree, status, linker-table, and preview regions. ``:focus``
       (matches the node itself) is correct while the box *is* the focus leaf; when a Tree/DataTable
       child lands inside, switch to an inline descendant-focus border (see Browser) so the heavy
       child's restyle doesn't cascade the whole subtree. */
    ResourceViewer .resource-box {
        border: solid #3a3a3a;
        border-title-align: left;
        border-title-color: rgb(120,120,120);
        background: transparent;
    }
    /* The two stretchy regions split the panel's leftover height ~60/40 (loader above the rule,
       linker below). Everything else is auto/fixed; nudge these two ``fr`` weights to reallocate. */
    ResourceViewer #rv-loader-tree {
        height: 3fr;
    }
    ResourceViewer #rv-loader-tree:focus {
        border: solid #6a6a6a;
    }
    /* Active-topic label + status box form a group: a line of padding above the topic label, with
       the status box flush beneath it (and flush against the tree below). */
    ResourceViewer #rv-topic {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        background: transparent;
    }
    ResourceViewer #rv-status {
        height: auto;
    }
    /* Vertical margin sets the two halves apart across the divider. */
    ResourceViewer #rv-divider {
        height: 1;
        color: #3a3a3a;
        margin: 2 0;
    }
    /* Fixed-height linker area; the table flexes within it so the dialog eats the table, not the
       panel (the loader half keeps its share). */
    ResourceViewer #rv-linker-area {
        height: 2fr;
        background: transparent;
    }
    ResourceViewer #rv-linker-table {
        height: 1fr;
    }
    ResourceViewer #rv-linker-table:focus {
        border: solid #6a6a6a;
    }
    /* Staged-changes Accept/Cancel — revealed via the ``.-visible`` class on dirty/clean transition
       (set inside ``ResourceLinkerAccept`` off ``vm.is_dirty_staging``). Thin top border that flips
       accent on focus, mirroring the entries-tab relink choices. A bottom margin separates it from
       the linker preview below. */
    ResourceViewer #rv-linker-accept {
        height: auto;
        margin: 1 0 1 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200, 200, 200);
        display: none;
    }
    ResourceViewer #rv-linker-accept.-visible {
        display: block;
    }
    ResourceViewer #rv-linker-accept:focus {
        border-top: solid $accent;
    }
    /* Two read-only previews, never shown at once — the focused half of the chain picks one (see
       ``_sync_focus_ui``). Fixed height (content scrolls past it) so cursoring through
       rows with varying summary lengths doesn't shift the regions below. */
    ResourceViewer #rv-top-preview,
    ResourceViewer #rv-bottom-preview {
        height: 14;
    }
    /* Keyboard-shortcuts row — pinned at the bottom by the stretchy regions above it. */
    ResourceViewer #rv-shortcuts {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        background: transparent;
    }
    """

    BINDINGS = [
        Keybind.FocusUp.  as_binding("focus_neighbour('up')",   show=False),
        Keybind.FocusDown.as_binding("focus_neighbour('down')", show=False),
        Keybind.ResourceSelectTopic.as_binding("select_topic", show=False),
        # Panel-internal focus / create shortcuts (fire from anywhere in the panel; no priority
        # ancestor grabs these keys). ctrl+up/down jump between the two halves; alt+r / alt+l name
        # the halves directly (ctrl+shift+* avoided — terminals can't distinguish it from ctrl+*).
        Keybind.ResourceFocusTree.  as_binding("focus_resource_tree", show=False),
        Keybind.ResourceFocusLinker.as_binding("focus_linker",        show=False),
        Keybind.ResourceCreate.     as_binding("create_resource",     show=False),
    ]

    FOCUS_GRAPH = FocusGraph(
        source="rv-loader-tree",
        edges={
            "rv-actions": {
                "down": "rv-loader-tree",
            },
            "rv-loader-tree": {
                "up": "rv-actions",
                "down": "rv-linker-search",
            },
            "rv-linker-search": {
                "up": "rv-loader-tree",
                "down": "rv-linker-table",
            },
            "rv-linker-table": {
                "up": "rv-linker-search",
                "down": "rv-linker-accept",
            },
            "rv-linker-accept": {
                "up": "rv-linker-table",
            },
        },
    )

    # Which preview a focus node drives — the split is the horizontal rule. Nodes above it feed the
    # top (loader) preview; nodes below feed the bottom (linker) preview.
    _TOP_PREVIEW_NODES = frozenset({"rv-actions", "rv-loader-tree"})
    _BOTTOM_PREVIEW_NODES = frozenset({"rv-linker-search", "rv-linker-table", "rv-linker-accept"})

    _NO_TOPIC_HINT = (
        "No topic currently selected. Use ctrl+t or the action menu to select one and load "
        "its resources."
    )

    # Edge-detection for the no-topic → topic transition (see ``_refresh``), so acquiring a topic
    # moves focus to the freshly-populated tree exactly once.
    _had_topic: bool = False

    def compose(self):
        yield Static(
            self._section_header(
                "Resources", "Browse and load the resources available to the current topic."
            ),
            id="rv-loader-header",
            classes="rv-section-header",
        )

        yield ResourceViewerActions(self._vm, id="rv-actions")

        # Empty-state hint above the tree. Toggling its text (no-topic prompt ↔ blank once a topic is
        # loaded) collapses the auto-height slot rather than reflowing the regions below.
        yield Static("", id="rv-loader-hint")

        # Active-topic label, grouped with the status box below it (both painted from the root VM in
        # ``_refresh``). Empty — and collapsed — until a topic is selected.
        yield Static("", id="rv-topic")

        status = ResourceStatus(self._vm.loader, id="rv-status", classes="resource-box")
        status.border_title = "Status"
        yield status

        loader = ResourceLoaderTree(self._vm.loader, id="rv-loader-tree", classes="resource-box")
        loader.border_title = "Resource Tree"
        yield loader

        # Top preview — tracks the loader cursor. Shown while focus is in the upper half of the chain.
        top_preview = ResourcePreview(self._vm.loader, id="rv-top-preview", classes="resource-box")
        top_preview.border_title = "Preview"
        yield top_preview

        yield Rule(id="rv-divider")

        yield Static(
            self._section_header(
                "Resource Linker", 
                ("Search for and link resources to the current topic. "
                 "When linked, resources will show up in the tree above, "
                 "so you can load them into the conversation.")
            ),
            id="rv-linker-header",
            classes="rv-section-header",
        )

        # SearchBar forwards the id to its inner field, so the focus node "rv-linker-search"
        # resolves to the focus-receiving widget. Driven by the linker VM (its natural owner);
        # submitting is a harmless no-op until a topic is set.
        yield SearchBar[ResourceLinkerVM](self._vm.linker, id="rv-linker-search")

        # Table + Accept/Cancel share one fixed-height (``2fr``) area: when the staged-changes dialog
        # appears it eats into the table (which is ``1fr`` inside the area) rather than growing the
        # area, so the box+dialog total height is fixed and the loader half above never shifts.
        with Vertical(id="rv-linker-area"):
            linker = ResourceLinkerTable(self._vm.linker, id="rv-linker-table", classes="resource-box")
            linker.border_title = "Link Resources"
            yield linker

            # Accept/Cancel for the staged link diff. Mounted unconditionally so its VM subscription
            # survives show/hide cycles; visibility is driven by the ``.-visible`` class it sets off
            # ``vm.is_dirty_staging`` (see ``ResourceLinkerAccept``).
            yield ResourceLinkerAccept(self._vm.linker, id="rv-linker-accept")

        # Bottom preview — tracks the linker cursor. Shown while focus is in the lower half.
        bottom_preview = ResourcePreview(self._vm.linker, id="rv-bottom-preview", classes="resource-box")
        bottom_preview.border_title = "Preview"
        yield bottom_preview

        # Initial content is the loader half's keys; ``_sync_focus_ui`` swaps line 1 with focus.
        yield Static(self._render_shortcuts("loader"), id="rv-shortcuts")

    # ------------------------------------------------------------------
    # Static content helpers
    # ------------------------------------------------------------------

    def _section_header(self, title: str, description: str) -> Text:
        """A section header as a single static: a bold title line over a dim description line."""
        text = Text()
        text.append(title + "\n", style="bold")
        text.append(description, style="#707070")
        return text

    def _render_topic(self) -> Text:
        """The active-topic label: a dim ``Topic`` lead-in then the name (id fallback)."""
        name = self._vm.current_topic_name or f"#{self._vm.current_topic_id}"
        text = Text()
        text.append("Current Topic:  ", style="#707070")
        text.append(name, style="bold rgb(190,190,190)")
        return text

    # Bottom keyboard-shortcuts hint. Line 1 is the focused half's keys (swapped on focus, see
    # ``_sync_focus_ui``); line 2 is the panel-wide keys, always shown.
    # Trimmed to fit the ~52-col panel on one line each (no mid-pair wrapping). The omitted keys
    # are reachable elsewhere — ctrl+f/search sits right above the table, ctrl+n/new is in the
    # actions bar, ctrl+r / ctrl+shift+l name the halves that ctrl+↑↓ already switches between.
    _LOADER_SHORTCUTS = (("space", "load (index)"), ("ctrl+enter", "load (context)"))
    _LINKER_SHORTCUTS = (("space", "link"), ("ctrl+enter", "confirm"), ("esc", "discard"))
    _GLOBAL_SHORTCUTS = (("alt+↑↓", "navigate"), ("ctrl+↑↓", "switch"), ("ctrl+t", "topic"), ("alt+w", "close"))

    def _render_shortcuts(self, half: str) -> Text:
        context = self._LOADER_SHORTCUTS if half == "loader" else self._LINKER_SHORTCUTS
        text = self._shortcut_line(context)
        text.append("\n")
        text.append(self._shortcut_line(self._GLOBAL_SHORTCUTS))
        return text

    def _shortcut_line(self, pairs) -> Text:
        """One ``[key] [action]`` row, key brighter than action, pairs separated by 3 spaces."""
        text = Text()
        for i, (key, action) in enumerate(pairs):
            if i:
                text.append("   ")
            text.append(key, style="#a0a0a0")
            text.append(" ")
            text.append(action, style="#707070")
        return text

    def on_mount(self) -> None:
        # Default to the loader (top) preview — matches the focus-graph source. Descendant focus
        # events refine it once focus actually lands somewhere in the chain.
        self.query_one("#rv-top-preview").display = True
        self.query_one("#rv-bottom-preview").display = False
        self._refresh()

    def _refresh(self) -> None:
        # Root-VM ``dirty`` (e.g. a topic change) lands here via ``ViewBase``. The only panel-level
        # state to paint is the empty-state hint; the sub-regions own their own refreshes.
        has_topic = self._vm.current_topic_id is not None
        # Collapse the hint entirely when a topic is set — ``display: none``, not just empty text, so
        # it claims no line below the actions bar.
        hint = self.query_one("#rv-loader-hint", Static)
        hint.display = not has_topic
        hint.update(self._NO_TOPIC_HINT)
        self.query_one("#rv-topic", Static).update(self._render_topic() if has_topic else Text())

        # Acquiring a topic (none → set) hands focus to the freshly-populated tree. Deferred so the
        # focus lands after the topic-selector modal has finished popping.
        if has_topic and not self._had_topic:
            self.call_after_refresh(lambda: self.query_one("#rv-loader-tree").focus())
        self._had_topic = has_topic

    # ------------------------------------------------------------------
    # Focus-within border — right edge only (the divider against the chat pane)
    # ------------------------------------------------------------------

    def _sync_focus_border(self) -> None:
        # Mirror NavigableViewBase's treatment, but paint only ``border_right`` — setting
        # ``styles.border`` would light up all four sides. ``None`` clears the inline override so the
        # CSS default / ``:hover`` right-border takes back over.
        focused = self.screen.focused if self.screen else None
        inside = focused is not None and (focused is self or self in focused.ancestors_with_self)
        self.styles.border_right = ("solid", "rgb(90, 140, 200)") if inside else None

    # ------------------------------------------------------------------
    # Focus orchestration
    # ------------------------------------------------------------------

    def action_focus_neighbour(self, direction: str) -> None:
        # On a None return the step had no in-graph target (a vertical edge of the chain). Bubble it
        # so a future host (the chat pane) can give alt+up/down its own meaning there.
        if self.focus_neighbour(direction) is None:  # type: ignore[arg-type]
            raise SkipAction()

    def _is_node_available(self, node_id: str) -> bool:
        # The Accept/Cancel menu is only a focus target while there's a staged diff to act on — it's
        # hidden (``display: none``) otherwise, so an ``alt+down`` into it would land on nothing.
        if node_id == "rv-linker-accept":
            return self._vm.linker.is_dirty_staging
        return True

    def focus_first(self) -> str | None:
        # Land on the actions bar when there's no topic yet (picking one is the user's first move),
        # otherwise on the resource tree.
        target = "rv-loader-tree" if self._vm.current_topic_id is not None else "rv-actions"
        widget = self._resolve_node(target)
        if widget is None:
            return None
        widget.focus()
        return target

    def action_focus_resource_tree(self) -> None:
        self.query_one("#rv-loader-tree").focus()

    def action_focus_linker(self) -> None:
        self.query_one("#rv-linker-table").focus()

    # ------------------------------------------------------------------
    # Focus-gated UI swap — which preview shows and which shortcuts line 1 shows track the focused
    # half (top/loader vs bottom/linker).
    # ------------------------------------------------------------------
    #
    # Both descendant events funnel into ``_sync_focus_ui``. ``screen.focused`` is already updated to
    # the new target by the time either handler runs (Textual reassigns it synchronously in
    # ``set_focus`` before posting Focus/Blur), so the two handlers compute the same answer and the
    # blur-vs-focus ordering is irrelevant. ``NavigableViewBase``'s own descendant handlers still fire
    # alongside these via Textual's per-MRO dispatch — no ``super()`` plumbing needed.

    def on_descendant_focus(self, event) -> None:
        self._sync_focus_ui()

    def on_descendant_blur(self, event) -> None:
        self._sync_focus_ui()

    def _sync_focus_ui(self) -> None:
        node = self._current_focus_node()
        if node in self._TOP_PREVIEW_NODES:
            half = "loader"
        elif node in self._BOTTOM_PREVIEW_NODES:
            half = "linker"
        else:
            return  # focus left the panel (or is mid-transition) — keep the last shown state
        self.query_one("#rv-top-preview").display = half == "loader"
        self.query_one("#rv-bottom-preview").display = half == "linker"
        self.query_one("#rv-shortcuts", Static).update(self._render_shortcuts(half))

    # ------------------------------------------------------------------
    # Topic selection (temporary ctrl+t shortcut)
    # ------------------------------------------------------------------

    def action_select_topic(self) -> None:
        """Spawn the topic-selector modal and point the VM at the chosen topic. No-op without a
        session factory (nothing to populate the selector)."""
        self._select_topic()
    
    def _select_topic(self):
        session_factory = self._vm.session_factory
        if session_factory is None:
            return

        # Deferred import: ``TopicSelectorScreen`` pulls ``rhizome.tui.widgets.TopicTree`` (part of
        # the widgets package init), so importing at module load would create a cycle.
        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        def _on_dismiss(result: tuple[int, str] | None) -> None:
            if result is None:
                return
            topic_id, topic_name = result
            self._vm.set_topic(topic_id, topic_name)

        self.app.push_screen(
            TopicSelectorScreen(session_factory=session_factory),
            _on_dismiss,
        )

    # ------------------------------------------------------------------
    # Action Message Handling
    # ------------------------------------------------------------------

    @on(ResourceViewerActions.SelectTopic)
    def _on_select_topic(self, event: ResourceViewerActions.SelectTopic) -> None:
        self._select_topic()

    def action_create_resource(self) -> None:
        self._create_resource()

    @on(ResourceViewerActions.CreateResource)
    def _on_create_resource(self, event: ResourceViewerActions.CreateResource) -> None:
        self._create_resource()

    def _create_resource(self) -> None:
        """Spawn the new-resource modal, then ingest the result into the panel's VM. No-op without a
        session factory (nothing to browse topics / persist against)."""
        if self._vm.session_factory is None:
            return

        from rhizome.tui.screens.new_resource import NewResourceScreen, NewResourceResult

        def _on_dismiss(result: NewResourceResult | None) -> None:
            if result is None:
                return
            self.run_worker(self._ingest_new_resource(result))

        self.app.push_screen(
            NewResourceScreen(session_factory=self._vm.session_factory),
            _on_dismiss,
        )

    async def _ingest_new_resource(self, result) -> None:
        """Drive the VM's ingest coroutine and surface the outcome as a toast. The loader tree
        refreshes itself off ``create_resource``'s internal reload."""
        try:
            message = await self._vm.create_resource(result)
        except Exception as exc:
            self.app.notify(f"Error creating resource: {exc}", severity="error")
            return
        self.app.notify(message)

    @on(ResourceViewerActions.LinkResources)
    def _on_link_resources(self, event: ResourceViewerActions.LinkResources) -> None:
        self.query_one("#rv-linker-table").focus()