"""Chromium transport for public pages that require a browser HTTP stack.

Each collection opens a fresh, anonymous context. No user profile or login cookies.
The caller retains robots.txt checks, throttling and HTTP error handling.
"""
import requests


class ChromiumSession:
    def __init__(self):
        # Compatibility with PoliteClient; native Chromium manages its own headers.
        self.headers = {}
        self._runtime = self._browser = self._page = None

    def get(self, url, timeout=30):
        if self._runtime is None:
            from playwright.sync_api import sync_playwright

            self._runtime = sync_playwright().start()
            try:
                self._browser = self._runtime.chromium.launch(headless=True)
                context = self._browser.new_context(locale="uk-UA")
                self._page = context.new_page()
            except Exception:
                self.close()
                raise
        result = self._page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        if result is None:
            raise requests.ConnectionError(f"Chromium did not receive a response: {url}")
        response = requests.Response()
        response.status_code = result.status
        response.headers.update(result.headers)
        response.url = result.url
        response.encoding = "utf-8"
        content_type = result.headers.get("content-type", "")
        # Rendered DOM for catalogs; raw text for robots.txt.
        response._content = (self._page.content().encode("utf-8")
                             if "text/html" in content_type else result.body())
        return response

    def close(self):
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = self._page = None
            if self._runtime is not None:
                self._runtime.stop()
                self._runtime = None

    def reveal_phone(self):
        """Use the public show-phone button, just as an anonymous visitor would."""
        if self._page is None:
            return ""
        from playwright.sync_api import Error
        try:
            button = self._page.locator('[data-testid="show-phone"]:visible')
            if button.count():
                button.first.click(timeout=4000)
                self._page.locator('a[href^="tel:"]:visible').first.wait_for(timeout=4000)
            return self._page.content()
        except Error:
            # Login-required or unavailable contacts do not prevent an advert alert.
            return ""
