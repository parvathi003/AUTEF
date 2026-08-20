"""A Material-styled web front end for the nine-stage repair pipeline.

Separate from ``ui.py`` (Streamlit) rather than replacing it: the Streamlit app
still carries the Compare and Benchmark tabs, which are run offline to produce
the report. This front end is the demonstrable product -- sign in, upload a
project, watch nine stages run.

Deliberately built on ``http.server`` from the standard library. Adding Flask or
FastAPI would put a second web framework in a project whose whole argument is
about dependency portability, to serve one page and six endpoints.
"""

from .server import DEFAULT_PORT, serve

__all__ = ["serve", "DEFAULT_PORT"]
