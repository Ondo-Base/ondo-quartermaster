"""Pixels, as the floor: rung three of the ladder.

Used only where the accessibility tree has nothing to offer: a Citrix or
remote-desktop window, a canvas, a game-engine UI. Our own computer-tool schema
(``tools.py``), our own coordinate handling (``geometry.py``), and grounding
behind one interface (``grounding.py``) so the model that turns "the Submit
button" into a point can be swapped for a self-hosted one without touching the
executor.
"""
