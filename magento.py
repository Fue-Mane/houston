"""Talks to the biomania.ro store through Magento's XML-RPC API.

Every write is done at Default Values (store 0) and by SKU, never by product ID.
"""
import time
import http.client
import xmlrpc.client

STORE = "0"


class ApiError(Exception):
    """A readable error from the store."""


def _readable(fault):
    msg = getattr(fault, "faultString", str(fault))
    if "interzis" in msg.lower() or "denied" in msg.lower():
        return f"The store refused this ({msg}). The API role probably needs this permission ticked."
    return msg


class Magento:
    def __init__(self, url, user, key):
        self.url, self.user, self._key = url, user, key
        self.proxy = None
        self._reset_proxy()
        self.session = None
        self.gap = 0.5        # seconds between requests; grows when the store asks to slow down
        self._last = 0.0
        self.on_wait = None   # optional callback(seconds) to show waiting in the UI

    # --- connection -----------------------------------------------------
    def _reset_proxy(self):
        """Discard a half-used HTTP connection and create a fresh XML-RPC proxy."""
        if self.proxy is not None:
            try:
                self.proxy.close()
            except Exception:
                pass
        self.proxy = xmlrpc.client.ServerProxy(self.url)

    def login(self):
        try:
            self.session = self.proxy.login(self.user, self._key)
        except xmlrpc.client.Fault as e:
            raise ApiError(f"Login refused: {e.faultString}") from None
        except Exception as e:
            raise ApiError(f"Could not reach the store: {type(e).__name__}: {e}") from None

    def logout(self):
        try:
            if self.session:
                self.proxy.endSession(self.session)
        except Exception:
            pass
        try:
            self.proxy.close()
        except Exception:
            pass
        self.session = None

    def _pace(self):
        wait = self._last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _run(self, fn, attempts=8, retry_connection=True):
        relogged = False
        connection_retried = False
        for attempt in range(attempts):
            self._pace()
            try:
                return fn()
            except (http.client.ResponseNotReady, http.client.CannotSendRequest,
                    http.client.RemoteDisconnected, http.client.BadStatusLine,
                    ConnectionResetError, BrokenPipeError, TimeoutError) as e:
                if retry_connection and not connection_retried and attempt < attempts - 1:
                    connection_retried = True
                    self._reset_proxy()
                    self.gap = max(self.gap, 1.0)
                    continue
                raise ApiError(f"Connection problem: {type(e).__name__}: {e}") from None
            except xmlrpc.client.ProtocolError as e:
                if e.errcode in (429, 503) and attempt < attempts - 1:
                    retry = e.headers.get("Retry-After") if e.headers else None
                    secs = int(retry) if retry and str(retry).isdigit() else min(60, 5 * 2 ** attempt)
                    self.gap = min(self.gap * 2 + 0.5, 5.0)
                    if self.on_wait:
                        self.on_wait(secs)
                    time.sleep(secs)
                    continue
                raise ApiError(f"The store answered HTTP {e.errcode} {e.errmsg}. Try again in a few minutes.") from None
            except xmlrpc.client.Fault as e:
                if e.faultCode == 5 and not relogged:  # session expired: log in again once
                    relogged = True
                    self.login()
                    continue
                raise ApiError(_readable(e)) from None
            except ApiError:
                raise
            except Exception as e:
                raise ApiError(f"Connection problem: {type(e).__name__}: {e}") from None
        raise ApiError("The store kept asking to slow down. Try again in a few minutes.")

    def call(self, path, args=None):
        return self._run(lambda: self.proxy.call(self.session, path, args or []))

    def multi(self, calls):
        """calls: list of (path, args). Returns results; a failed call comes back as ApiError."""
        def go():
            res = self.proxy.multiCall(self.session, [[p, a] for p, a in calls], [])
            return [ApiError(_readable(r.get("faultMessage", "error")))
                    if isinstance(r, dict) and r.get("isFault") else r for r in res]
        return self._run(go)

    # --- attributes -----------------------------------------------------
    def attribute_sets(self):
        return self.call("catalog_product_attribute_set.list")

    def set_attributes(self, set_id):
        return self.call("catalog_product_attribute.list", [set_id])

    def attribute_info_many(self, codes):
        return self.multi([("catalog_product_attribute.info", [c]) for c in codes])

    def attribute_options_many(self, attribute_ids):
        return self.multi([("catalog_product_attribute.options", [a, STORE]) for a in attribute_ids])

    def add_option(self, code, label):
        data = {"label": [{"store_id": [STORE], "value": label}], "order": 0, "is_default": 0}
        return self._run(lambda: self.proxy.call(self.session, "catalog_product_attribute.addOption",
                                                 [code, data]), retry_connection=False)

    # --- products -------------------------------------------------------
    def product_list(self, filters=None):
        return self.call("catalog_product.list", [filters or {}, STORE])

    def products_info(self, items):
        """items: list of (sku, attribute codes)."""
        return self.multi([("catalog_product.info", [sku, STORE, codes, "sku"]) for sku, codes in items])

    def product_info(self, sku, codes):
        return self.call("catalog_product.info", [sku, STORE, codes, "sku"])

    def update_product(self, sku, data):
        return self._run(lambda: self.proxy.call(self.session, "catalog_product.update",
                                                 [sku, data, STORE, "sku"]), retry_connection=False)

    def stock(self, skus):
        return self.call("cataloginventory_stock_item.list", [skus])

    def update_stock(self, sku, data):
        return self._run(lambda: self.proxy.call(self.session, "cataloginventory_stock_item.update",
                                                 [sku, data]), retry_connection=False)

    # --- images ---------------------------------------------------------
    def media(self, sku):
        return self.call("catalog_product_attribute_media.list", [sku, STORE, "sku"])

    def media_update(self, sku, file, data):
        return self._run(lambda: self.proxy.call(self.session, "catalog_product_attribute_media.update",
                                                 [sku, file, data, STORE, "sku"]), retry_connection=False)

    def media_create(self, sku, data):
        return self._run(lambda: self.proxy.call(self.session, "catalog_product_attribute_media.create",
                                                 [sku, data, STORE, "sku"]), retry_connection=False)

    def media_remove(self, sku, file):
        return self._run(lambda: self.proxy.call(self.session, "catalog_product_attribute_media.remove",
                                                 [sku, file, "sku"]), retry_connection=False)

    # --- related products ----------------------------------------------
    def links(self, link_type, sku):
        return self.call("catalog_product_link.list", [link_type, sku, "sku"])

    def link_assign(self, link_type, sku, linked):
        return self._run(lambda: self.proxy.call(self.session, "catalog_product_link.assign",
                                                 [link_type, sku, linked, {}, "sku"]), retry_connection=False)

    def link_remove(self, link_type, sku, linked):
        return self._run(lambda: self.proxy.call(self.session, "catalog_product_link.remove",
                                                 [link_type, sku, linked, "sku"]), retry_connection=False)

    # --- categories -----------------------------------------------------
    def category_tree(self):
        return self.call("catalog_category.tree")

    def category_info(self, cid):
        return self.call("catalog_category.info", [cid, STORE])

    def category_update(self, cid, data):
        return self._run(lambda: self.proxy.call(self.session, "catalog_category.update",
                                                 [cid, data, STORE]), retry_connection=False)

    def category_create(self, parent_id, data):
        return self._run(lambda: self.proxy.call(self.session, "catalog_category.create",
                                                 [parent_id, data, STORE]), retry_connection=False)

    def category_products(self, cid):
        return self.call("catalog_category.assignedProducts", [cid, STORE])

    def category_assign(self, cid, sku):
        return self._run(lambda: self.proxy.call(self.session, "catalog_category.assignProduct",
                                                 [cid, sku, 0, "sku"]), retry_connection=False)

    def category_remove(self, cid, sku):
        return self._run(lambda: self.proxy.call(self.session, "catalog_category.removeProduct",
                                                 [cid, sku, "sku"]), retry_connection=False)
