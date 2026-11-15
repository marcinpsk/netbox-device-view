"""Render a real response with its static files in Chromium."""

from contextlib import contextmanager
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.staticfiles import finders
from playwright.sync_api import sync_playwright


@contextmanager
def browser_page(content):
    def static_file(route):
        path = urlsplit(route.request.url).path
        filename = (
            finders.find(path.removeprefix(settings.STATIC_URL))
            if path.startswith(settings.STATIC_URL)
            else None
        )
        if filename:
            route.fulfill(path=filename)
        else:
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", static_file)
            page.set_content(
                content.replace("<head>", '<head><base href="http://testserver/">'),
                wait_until="load",
            )
            yield page
        finally:
            browser.close()
