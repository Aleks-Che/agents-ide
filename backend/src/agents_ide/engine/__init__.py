"""Engine modules: graph traversal, candidate selection, events and artifacts.

The engine runs queued Runs sequentially on the worker. It consumes the
immutable snapshot produced at start time, so editing the source pipeline
after dispatch does not change execution. External work is delegated to
adapters registered through :mod:`agents_ide.adapters`; the engine itself
never opens sockets or processes.
"""

from agents_ide.engine import (  # noqa: F401  (re-exported)
    artifacts,
    candidates,
    events,
    runner,
    visits,
)
