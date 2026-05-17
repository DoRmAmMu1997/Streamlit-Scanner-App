"""Pluggable screener modules.

Each screener module exposes:
- SCREENER: metadata used by the Streamlit dropdown.
- run(universe_df, data_loader, params): returns a pandas DataFrame.
"""

