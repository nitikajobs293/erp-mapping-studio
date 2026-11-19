"""ERP Logistics Data Architect - Streamlit entry point.

Run locally:   streamlit run app.py
Deploy:        Streamlit Community Cloud -> main file path = app.py  (see README.md)
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import APP_NAME  # noqa: E402

st.set_page_config(page_title=APP_NAME, page_icon="🚚", layout="wide", initial_sidebar_state="expanded")

P = "app_pages/"
pages = {
    "Overview": [
        st.Page(P + "home.py", title="Home Dashboard", icon="🏠", default=True),
        st.Page(P + "erp_assistant.py", title="Ask ERP Assistant", icon="💬"),
    ],
    "Explore & Map": [
        st.Page(P + "tcode_explorer.py", title="T-Code Explorer", icon="🔎"),
        st.Page(P + "business_object_mapping.py", title="Business Object Mapping", icon="📦"),
        st.Page(P + "column_mapping.py", title="Column Mapping", icon="🧩"),
    ],
    "Design & Build": [
        st.Page(P + "data_model.py", title="Data Model Designer", icon="🗺️"),
        st.Page(P + "sql_generator.py", title="SQL Generator", icon="❄️"),
        st.Page(P + "cross_erp.py", title="Cross-ERP Comparison", icon="⚖️"),
    ],
    "Knowledge & Output": [
        st.Page(P + "metadata_repository.py", title="ERP Metadata Repository", icon="🗄️"),
        st.Page(P + "downloads.py", title="Documentation & Downloads", icon="📄"),
        st.Page(P + "settings.py", title="Settings", icon="⚙️"),
    ],
}
st.navigation(pages).run()
