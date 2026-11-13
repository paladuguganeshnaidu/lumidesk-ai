"""Chromium smoke checks for the real FastAPI/Jinja/JS UI.

Run with: python -m playwright install chromium && python tests/browser_smoke_v5.py
No Google/NVIDIA credentials required. This checks public UI and security barriers.
"""
from __future__ import annotations
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8879"


def await_health():
    for _ in range(60):
        try:
            with urllib.request.urlopen(URL + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(.25)
    raise RuntimeError("LumiDesk server never became healthy")


def block_externals(route):
    # Keep E2E deterministic: fonts and Unsplash photography are decorative.
    if route.request.url.startswith(URL):
        route.continue_()
    else:
        route.abort()


def check_portal(browser, mobile=False, reduced=False):
    width, height = (390, 844) if mobile else (1440, 900)
    context = browser.new_context(
        viewport={"width": width, "height": height},
        reduced_motion="reduce" if reduced else "no-preference",
        is_mobile=mobile,
        has_touch=mobile,
    )
    context.route("**/*", block_externals)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_selector("body.portal-ready", timeout=12000)
    assert page.locator(".p-hero").count() == 1
    assert page.locator(".p-deck .p-card").count() == 4
    assert page.locator("main section").count() == 6
    assert "rgb(10, 12, 14)" == page.locator("body").evaluate("(el) => getComputedStyle(el).backgroundColor")
    if mobile:
        page.locator(".p-menu").click()
        assert "open" in page.locator(".p-nav-links").get_attribute("class")
        page.locator(".p-nav-links a").first.click()
        assert "open" not in page.locator(".p-nav-links").get_attribute("class")
    if reduced:
        assert page.locator(".p-panel.left").evaluate("(el) => getComputedStyle(el).display") == "none"
    else:
        page.evaluate("document.documentElement.style.scrollBehavior='auto';window.scrollTo(0,700)")
        page.wait_for_timeout(150)
        shift = page.locator(".p-panel.left").evaluate("(el)=>getComputedStyle(el).transform")
        assert shift.startswith("matrix("), shift
        offset = abs(float(shift.split(",")[4]))
        assert offset > 100, (width, offset)
    page.locator("#tools").scroll_into_view_if_needed()
    page.locator("#p-next").click()
    page.wait_for_timeout(550)
    assert page.locator(".p-dots button.active").get_attribute("data-i") == "1"
    page.locator("#p-prev").click()
    page.wait_for_timeout(550)
    assert page.locator(".p-dots button.active").get_attribute("data-i") == "0"
    page.locator("#p-deck").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(550)
    assert page.locator(".p-dots button.active").get_attribute("data-i") == "1"
    assert not errors, errors
    context.close()


def main():
    env = os.environ.copy()
    env.setdefault("APP_ENV", "development")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                               "--host", "127.0.0.1", "--port", "8879"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
    try:
        await_health()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            check_portal(browser)
            check_portal(browser, mobile=True)
            check_portal(browser, reduced=True)
            context = browser.new_context(viewport={"width": 1200, "height": 900})
            context.route("**/*", block_externals)
            page = context.new_page()
            page.goto(URL + "/app?tab=reports", wait_until="domcontentloaded")
            assert page.locator("#report-form").count() == 1
            assert page.locator("#format-inspector").count() == 1
            assert page.locator("#regenerate-panel").count() == 1
            body_bg = page.locator("body").evaluate("(el)=>getComputedStyle(el).backgroundColor")
            assert body_bg == "rgb(10, 12, 14)", body_bg
            for slug in ("/privacy", "/terms"):
                page.goto(URL + slug, wait_until="domcontentloaded")
                assert page.locator("body.editorial-aux").count() == 1
            assert page.request.get(URL + "/api/student/overview").status == 401
            browser.close()
        print("LIVE CHROMIUM UI PASS: desktop, mobile, reduced motion, scroll portal >100px,")
        print("deck controls and keyboard, workspace dark mode, public legal pages and API protection.")
    finally:
        server.terminate()
        try:
            server.wait(timeout=6)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
