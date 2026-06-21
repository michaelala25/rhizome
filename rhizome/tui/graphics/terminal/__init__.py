"""The "terminal dance" group: discovering what the terminal can do, before the UI takes over.

Everything here talks to the terminal at the byte level — the raw-mode query/reply round-trip
(``query``), the cell-size resolution built on it (``cellsize``), and the capability probes that map
a terminal to a graphics protocol (``capabilities``). None of it knows anything about rendering.
"""
