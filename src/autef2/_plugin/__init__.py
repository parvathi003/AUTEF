"""Holder for the injected pytest plugin.

The plugin module itself is loaded by *another* interpreter (the project's
virtualenv) via PYTHONPATH, so it is deliberately not imported from here.
"""
