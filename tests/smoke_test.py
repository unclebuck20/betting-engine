"""Browser smoke test: every tab renders, no JS errors, no sideways scroll at phone width, Took it + $ toggle work."""
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None


def main():
    srv = subprocess.Popen([sys.executable, "-m", "http.server", "8765"], cwd=ROOT / "docs",
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    fails = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            for scheme in ("light", "dark"):
                pg = b.new_page(viewport={"width": 375, "height": 1200}, color_scheme=scheme)
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.add_init_script("window.open = (u) => { window.__opened = u; }")
                pg.goto("http://localhost:8765/")
                pg.wait_for_selector(".tab")
                for label in ("Thursday", "Friday", "Saturday", "Sunday", "SNF", "MNF"):
                    pg.click(f".tab:has-text('{label}')")
                    pg.wait_for_timeout(100)
                    if pg.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
                        fails.append(f"{scheme}/{label}: sideways scroll")
                    if SHOTS and scheme == "light" and label in ("Saturday", "Thursday", "SNF"):
                        pg.screenshot(path=str(SHOTS / f"{label.lower()}.png"), full_page=False)
                pg.click(".tab:has-text('Saturday')")
                btn = pg.locator("[data-took]").first
                if btn.count():
                    btn.click()
                    if "issues/new" not in (pg.evaluate("window.__opened") or ""):
                        fails.append(f"{scheme}: Took it didn't open a GitHub issue")
                    if not pg.locator("text=Pending").count():
                        fails.append(f"{scheme}: no pending state after Took it")
                    pg.click(".undo")
                pg.click("#money")
                pg.fill("#unitval", "15")
                pg.press("#unitval", "Enter")
                pg.dispatch_event("#unitval", "change")
                pg.wait_for_timeout(100)
                if not pg.locator(".stat b:has-text('$')").count():
                    fails.append(f"{scheme}: $ toggle didn't show dollars")
                if SHOTS and scheme == "dark":
                    pg.screenshot(path=str(SHOTS / "saturday_dark_dollars.png"))
                pg.click("#v-bets")
                pg.wait_for_timeout(100)
                if SHOTS and scheme == "light":
                    pg.screenshot(path=str(SHOTS / "mybets.png"), full_page=True)
                if pg.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
                    fails.append(f"{scheme}/My bets: sideways scroll")
                fails += [f"{scheme}: JS error {e}" for e in errs]
            b.close()
    finally:
        srv.terminate()
    print(json.dumps({"ok": not fails, "fails": fails}))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
