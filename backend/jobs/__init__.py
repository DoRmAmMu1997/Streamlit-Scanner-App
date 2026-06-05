"""Headless job entrypoints for scanner workflows.

Beginner note:
The Streamlit app is great for interactive use, but scheduled jobs need plain
Python modules that can run from a terminal, cron, GitHub Actions, or a hosting
platform's scheduler. That is why modules in this package should avoid importing
Streamlit: Streamlit expects a browser session and widget state, while a job only
needs backend services and a process exit code.

Design rule for future files in this package:
Keep them thin. They should gather inputs, call existing backend services, print
operator-friendly summaries, and exit clearly. Strategy logic still belongs in
``screeners/`` and persistence logic still belongs in ``backend.storage`` /
``backend.scanning``.
"""
