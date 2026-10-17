"""Houston - biomania.ro command center. Start with:  streamlit run app.py"""
import streamlit as st

st.set_page_config(page_title="Houston", page_icon="🚀", layout="wide")

import core  # noqa: E402
import views  # noqa: E402

st.markdown("""<style>
[data-testid="stNavSectionHeader"] {text-transform: uppercase; letter-spacing: .08em; font-size: .72rem;
  font-weight: 700; opacity: .65; margin-top: .9rem;}
[data-testid="stSidebarNavLink"] span {font-size: .95rem;}
[data-testid="stSidebarNavLink"][href$="/review"] {background: #2fb36b; border-radius: 6px; margin: 4px 0;}
[data-testid="stSidebarNavLink"][href$="/review"] span {color: #fff !important; font-weight: 600;}
</style>""", unsafe_allow_html=True)
views.header()
waiting = len(core.load_pending())
pages = {
    "": [st.Page(views.page_overview, title="Overview", default=True),
         st.Page(views.page_worklog, title="Work log", url_path="worklog")],
    "Catalogue": [
        st.Page(views.page_products, title="Products"),
        st.Page(views.page_bulk, title="Bulk edit"),
        st.Page(views.page_categories, title="Categories"),
        st.Page(views.page_attributes, title="Attributes"),
        st.Page(views.page_review, title=f"Review and push ({waiting})" if waiting else "Review and push", url_path="review"),
        st.Page(views.page_log, title="Change log"),
    ],
    "Google": [
        st.Page(views.page_google_search_console, title="Google Search Console", url_path="google-search-console"),
        st.Page(views.page_google_analytics, title="Google Analytics", url_path="google-analytics"),
        st.Page(views.page_google_ads, title="Google Ads"),
    ],
    "Store (later)": [st.Page(views.page_onsite_search, title="On-site search")],
}
page = st.navigation(pages)
views.sidebar_connection()
page.run()
