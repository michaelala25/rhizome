"""The "rendering frontend" group: turning a bitmap + boxes into something on the screen.

The protocol-neutral contract (``backend``), the shared letterbox math (``geometry``), the concrete
sixel backend (``sixel``) and kitty stub (``kitty``), and the Textual widget that drives them
(``image``). Capability detection and the cell size it needs live in the ``terminal`` group.
"""
