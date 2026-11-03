"""Sidebar box (shown while connected to the store): update the website orders the channel rules use."""
import streamlit as st

import health
import store_orders
from magento import ApiError


def orders_box(mg):
    i = store_orders.info()
    label = f"Website orders · {i['last_pull'][:16]}" if i["last_pull"] else "Website orders · never"
    with st.expander(label):
        st.caption("Houston keeps four things per order: number, date, status and total. No names or addresses. "
                   "The first update reads every order since April 2011 and takes a few minutes; later ones "
                   "re-read the last 60 days.")
        if i["orders"]:
            st.caption(f"{i['orders']:,} orders stored, newest placed {i['newest']}.")
        if i["last_error"]:
            st.caption(f"⚠️ Last update stopped: {i['last_error']}")
        if st.button("Update website orders", key="mg_orders_go", use_container_width=True):
            bar = st.progress(0.0, "Starting")
            try:
                r = store_orders.update(mg, lambda f, t: bar.progress(min(float(f), 1.0), t))
            except ApiError as e:
                st.error(str(e))
                return
            h = health.rebuild_quietly("Website orders update")
            msg = f"{r['orders']:,} orders read ({r['start']} to {r['end']})."
            if h:
                msg += f" Checks re-run: {h['counts'].get('sale_no_channel', 0)} sales with no channel."
            st.success(msg)
