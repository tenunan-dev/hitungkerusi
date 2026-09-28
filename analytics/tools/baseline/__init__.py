"""GE16 baseline rebuild tools (owner directive: one program, whole baseline).

The single entrypoint is ``tools.baseline.rebuild_baseline`` — run it as
``./.venv/bin/python -m tools.baseline.rebuild_baseline --all``. Keeping this
package initializer import-free lets ``python -m tools.baseline.rebuild_baseline``
load the orchestrator exactly once.
"""
