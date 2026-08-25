"""A Material-styled web front end for the nine-stage repair pipeline.

This is the product: sign in, upload a project, watch nine stages run. The
comparison and the benchmark are deliberately absent -- they are experiments
costing real money per project, and they belong on the command line, which is
where report numbers should come from.

Deliberately built on ``http.server`` from the standard library. Adding Flask or
FastAPI would put a second web framework in a project whose whole argument is
about dependency portability, to serve one page and six endpoints.
"""

from .server import DEFAULT_PORT, serve

__all__ = ["serve", "DEFAULT_PORT"]
