"""cordboard — pass work between coding agents with a file-based baton.

No server and no broker: the whole protocol is a JSON file, an append-only chat
file, and a spec both agents read. See :mod:`cordboard.workflows` for the phase
graphs and :mod:`cordboard.roles` for who may act when.
"""

from __future__ import annotations

from .state import SCHEMA_VERSION, StateError

__all__ = ["SCHEMA_VERSION", "StateError", "__version__"]

__version__ = "0.3.0"
