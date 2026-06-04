"""Central, overridable keybinding vocabulary.

Bindings whose meaning is shared across many hosts live here as ``Keybind`` members, so a concept's
``(id, default_key)`` is defined once. A host turns a concept into a Textual ``Binding`` at its use site
via ``as_binding`` — supplying its own ``action_<name>`` handler and presentation (description / show /
system / priority), which stay local and visible:

    BINDINGS = [
        Keybind.DialogConfirm.as_binding("select", "Select", show=True),
        Keybind.DialogBack.as_binding("back", "Back", show=True),
        Keybind.DialogCancel.as_binding("cancel", "Cancel", show=True, priority=True),
    ]

Rebinding is keyed by ``id`` (Textual's keymap), so two hosts may map one concept to different actions and
a single keymap entry still retargets both. Ids follow ``<scope>.<action>``; a leading ``_`` on any segment
marks a concept private — a convention users shouldn't rebind (see ``is_private``).

A binding owned by a single widget needs no concept here — declare it inline as an ordinary ``Binding(...)``
with an inline id.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from textual.binding import Binding


# ========================================================================================================================
# ON BINDING CLASHES (design intent)
# ========================================================================================================================
# A clash is two bindings with the same key reachable in one *focus chain* at runtime — the focused widget,
# then its DOM ancestors, then the screen, then the app — where the lower (more-focused) binding silently
# shadows the rest. This is genuinely dynamic: we can't know a priori which widgets share a chain (a focused
# TextArea, say, can shadow a key bound on a parent without it being visible anywhere in the source).
#
# Decision: we do NOT detect clashes ourselves. Reproducing Textual's chain resolution statically is a losing
# game, so we lean entirely on Textual to surface them — chiefly ``App.handle_bindings_clash``, which Textual
# calls when a user keymap remaps a key onto one already bound *on the same node*. Override that hook if/when
# we want to warn the user; otherwise let chain precedence resolve things as Textual intends.
#
# What the namespaces ARE for: the ``<scope>.<action>`` id is a *human* signal of how global an action is, so
# a dev designing a new view can read the lay of the land and avoid stomping a broad key — e.g. ``space`` is
# the root-level ``Toggle``, so a new view should think twice before binding ``space`` to something else and
# pick a different key/action instead. The namespaces are a discipline we maintain by READING them, not a
# programmatic conflict engine. Keep them aligned with where a binding actually lives in the widget tree so
# the signal stays honest. (Same-key bindings that share one id and are gated by ``check_action`` — e.g.
# FlashcardReview's ``enter`` flavors — are intentional co-existence, not a clash.)


class Keybind(Enum):
    """A shared, overridable binding concept: its stable ``id`` and ``default_key``.

    Value is ``(id, default_key)``. The Textual action is deliberately NOT stored — each host passes its
    own to ``as_binding`` so hosts keep their natural ``action_<name>`` handlers.
    """

    def __init__(self, id: str, default_key: str) -> None:
        self.id = id
        self.default_key = default_key

    def as_binding(self, action: str, description: str = "", **kwargs) -> Binding:
        """Build a Textual ``Binding`` for this concept: the concept supplies ``key`` + ``id``, the caller
        supplies ``action`` (its own ``action_<action>`` handler) and presentation kwargs (show / system /
        priority / key_display / …).
        """
        return Binding(self.default_key, action, description, id=self.id, **kwargs)

    # MainScreen bindings
    NewTab    = ("root.new_tab",    "ctrl+n")
    CloseTab  = ("root.close_tab",  "ctrl+w")
    NextTab   = ("root.next_tab",   "ctrl+pagedown")
    PrevTab   = ("root.prev_tab",   "ctrl+pageup")
    FocusChat = ("root.focus_chat", "ctrl+l")

    # Global navigation — focus orchestration (alt+arrows) and cursor movement (arrows). Root-level: in play
    # all over the app, so a new view should avoid colliding with these.
    FocusLeft   = ("root.focus_left",   "alt+left")
    FocusRight  = ("root.focus_right",  "alt+right")
    FocusUp     = ("root.focus_up",     "alt+up")
    FocusDown   = ("root.focus_down",   "alt+down")
    CursorUp    = ("root.cursor_up",    "up")
    CursorDown  = ("root.cursor_down",  "down")
    CursorLeft  = ("root.cursor_left",  "left")
    CursorRight = ("root.cursor_right", "right")
    Toggle      = ("root.toggle",       "space")
    SelectUp    = ("root.select_up",    "shift+up")
    SelectDown  = ("root.select_down",  "shift+down")
    PageUp      = ("root.page_up",      "pageup")
    PageDown    = ("root.page_down",    "pagedown")

    # Menus / dropdowns
    CloseMenu   = ("menu.close",   "escape")
    MenuConfirm = ("menu.confirm", "enter")
    MenuReset   = ("menu.reset",   "r")

    # Text editing
    EditAccept    = ("edit.accept",     "ctrl+j")
    EditSelectAll = ("edit.select_all", "ctrl+a")

    # Shared ModalScreen bindings
    DialogBack    = ("dialog._back",    "escape")
    DialogCancel  = ("dialog._cancel",  "ctrl+c")
    DialogConfirm = ("dialog._confirm", "enter,ctrl+j")

    # Browser — tab switching, plus the entries tab and topics panel within it
    BrowserNextTab = ("browser.next_tab", "ctrl+right")
    BrowserPrevTab = ("browser.prev_tab", "ctrl+left")

    BrowserDelete      = ("browser.tab.delete",       "d")
    BrowserSort        = ("browser.tab.sort",         "s")
    BrowserFilter      = ("browser.tab.filter",       "f")
    BrowserEdit        = ("browser.tab.edit",         "e")
    BrowserMultiSelect = ("browser.tab.multi_select", "m")
    BrowserCycleMode   = ("browser.tab.cycle_mode",   "tab")

    # Entry tab specific
    BrowserRelink      = ("browser.entries_tab.relink", "l")

    # Filter-menu sub-area escape — could elevate for other purposes
    BrowserFilterCloseSubarea = ("browser.tab.filter_menu.close_subarea", "escape")

    BrowserRenameTopic       = ("browser.topics.rename",      "r")
    BrowserCreateTopic       = ("browser.topics.create",      "c")
    BrowserCreateTopicAtRoot = ("browser.topics.create_root", "shift+c")
    BrowserDeleteTopic       = ("browser.topics.delete",      "d")

    # File browser navigation
    FileBrowserParent   = ("file_browser.parent",    "left")
    FileBrowserEnterDir = ("file_browser.enter_dir", "right")
    FileBrowserHome     = ("file_browser.home",      "home")

    # Proposal widgets — commit + flashcard proposals share these (the two views are identical)
    ProposalAcceptAll              = ("proposal.accept_all",               "ctrl+a")
    ProposalReset                  = ("proposal.reset",                    "ctrl+r")
    ProposalCancel                 = ("proposal.cancel",                   "ctrl+c")
    ProposalToggleEditInstructions = ("proposal.toggle_edit_instructions", "ctrl+e")
    ProposalSetTopicAll            = ("proposal.set_topic_all",            "shift+t")
    ProposalToggleCollapsed        = ("proposal.toggle_collapsed",         "enter")
    ProposalEdit                   = ("proposal.edit",                     "e")
    ProposalToggleExclude          = ("proposal.toggle_exclude",           "d")
    ProposalSetTopic               = ("proposal.set_topic",                "t")
    ProposalCycleType              = ("proposal.cycle_type",               "f")

    # Chat interrupts — extras beyond the shared cursor/confirm/cancel (multi-question prompts)
    InterruptSubmit       = ("interrupt.submit",        "ctrl+j")
    InterruptPrevQuestion = ("interrupt.prev_question", "ctrl+left")
    InterruptNextQuestion = ("interrupt.next_question", "ctrl+right")

    # Options editor
    OptionsApply   = ("options.apply",   "ctrl+a")
    OptionsReset   = ("options.reset",   "ctrl+r")
    OptionsDismiss = ("options.dismiss", "ctrl+c")

    # Resource viewer — dual-key shortcuts bind both default keys (rebound together)
    ResourceSelectTopic   = ("resource.select_topic",   "ctrl+t")
    ResourceCreate        = ("resource.create",         "ctrl+n")
    ResourceFocusTree     = ("resource.focus_tree",     "alt+r,ctrl+up")
    ResourceFocusLinker   = ("resource.focus_linker",   "alt+l,ctrl+down")
    ResourceFocusSearch   = ("resource.focus_search",   "ctrl+f")
    ResourceConfirmEdits  = ("resource.confirm_edits",  "ctrl+enter,ctrl+j")
    ResourceToggleContext = ("resource.toggle_context", "ctrl+enter,ctrl+j")

    # Chat pane
    ChatCycleMode           = ("chat.cycle_mode",            "shift+tab")
    ChatCycleVerbosity      = ("chat.cycle_verbosity",       "ctrl+b")
    ChatCommitSubmit        = ("chat.commit_submit",         "ctrl+j")
    ChatCancel              = ("chat.cancel",                "ctrl+c")
    ChatNavUp               = ("chat.nav_up",                "ctrl+up")
    ChatNavDown             = ("chat.nav_down",              "ctrl+down")
    ChatFocusResourceViewer = ("chat.focus_resource_viewer", "alt+r")
    ChatCloseResourceViewer = ("chat.close_resource_viewer", "alt+w")

    # Chat pane — branch navigation (alt+arrows here walk the branch graph, not the focus graph)
    ChatBranchSiblingLeft  = ("chat.branch.sibling_left",  "alt+left")
    ChatBranchSiblingRight = ("chat.branch.sibling_right", "alt+right")
    ChatBranchAscend       = ("chat.branch.ascend",        "alt+up")
    ChatBranchDescend      = ("chat.branch.descend",       "alt+down")
    ChatBranchRename       = ("chat.branch.rename",        "r")


def is_private(binding_id: str) -> bool:
    """True if any dotted segment of ``binding_id`` starts with ``_`` — i.e. not surfaced to the user."""
    return any(segment.startswith("_") for segment in binding_id.split("."))


def binding_hint(bindings: Iterable[Binding], *, sep: str = "   ") -> str:
    """Render the shown bindings as a compact ``<key>: <desc>`` hint line, in declaration order.

    Lets the small in-widget hint rows screens draw be sourced from their real bindings rather than
    hardcoded strings. Skips hidden / description-less bindings; uses ``key_display`` when set, else the
    binding's first key.
    """
    parts = []
    for binding in bindings:
        if not (binding.show and binding.description):
            continue
        key = binding.key_display or binding.key.split(",")[0]
        parts.append(f"{key}: {binding.description.lower()}")
    return sep.join(parts)
