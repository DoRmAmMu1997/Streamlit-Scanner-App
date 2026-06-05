"""Headless job entrypoints for running scanner workflows outside Streamlit.

Beginner note:
The Streamlit app is great for interactive use, but scheduled jobs need plain
Python modules that can run from a terminal, cron, or a hosting platform. Files
in this package should avoid importing Streamlit and should reuse backend
services directly.
"""

