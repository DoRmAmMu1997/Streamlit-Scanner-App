"""Streamlit UI modules extracted from app.py (REF-001).

Why this package exists:
app.py grew past 2,000 lines while staying responsible for every page and
widget. The page renderers and their pure helpers now live here so each page
can be read, tested, and reviewed on its own. The split follows one rule:

- ``backend/`` never imports Streamlit (headless jobs reuse it);
- ``ui/`` may import Streamlit and ``backend/``, never ``app``;
- ``app.py`` wires pages together and owns the entrypoint.

app.py re-exports the moved names so tests and callers that use
``app.<helper>`` keep working unchanged.
"""
