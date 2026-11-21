#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""Shoots the README's Tern screenshots into docs/tern/: the widget bar with the Claude widget's
details, then the dashboard zoomed to fill the window on each tab (Usage, Agents, Waiting with a
row's details open, History) and the Settings dialog over it.

It runs take_tern.py's world (scratch HOME, private Tern daemon, stand-in agents, fixture
agents.json and usage, three sessions set aside) in a 1160x900 window; Usage grows the window to
fit its providers. Toasts are hidden so none covers a shot. WORK keeps the raw shots and their
layout JSON; the next run replaces it.

    headless-gui --size 1600x1000 -- uv run film/shoot_tern.py [WORK]
        (default WORK /tmp/codexbar-agentview-shots-tern)

Needs Tern and a Wayland session; headless-gui keeps the window off your desktop.
"""

import json
import re
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import take_tern as base  # noqa: E402

DOCS = base.REPO / "docs/tern"
COMMAND = "plugin.codexbar-agentview."
# The dashboard's tab strip, in this order, and the Settings dialog over it.
TABS = ["usage", "agents", "waiting", "history"]
TAB = r"sf-tabs\.kit-tabs>div\.kit-tab\.sf-tab[.a-z-]*$"
PREFS = r"sf-prefs\.sf-act\.sf-window$"
# Usage's provider cards, and the status bar and key hints docked under them.
CARD = r"sf-region\.sf-main[.a-z-]*>div\.sf\.sf-card[.a-z-]*$"
DOCK = r"sf-region\.sf-dock[.a-z-]*$"
# The plugin's widgets at the widget bar's start (the first is Claude's), and a widget's details.
WIDGET = r"footer#widgetbar>div\.wg-row>div\.wg\.carded[.a-z]*$"
DETAILS = r"kit-popover\.wgc$"
# Room between Usage's last card and its status bar, as the window grows to fit them.
USAGE_GAP = 48


class Shots(base.Take):
    size = (1160, 900)

    def prepare(self):
        super().prepare()
        # The tick that brings the passed check back on you toasts; a toast would cover a shot.
        with open(self.work / "film.css", "a") as css:
            css.write("div.toast { display: none !important; }\n")

    def visible(self, pattern: str) -> list[list[float]]:
        return [e["rect"] for e in self.layout()
                if e["visible"] and e["rect"][2] > 0 and re.search(pattern, e["path"])]

    def park_pointer(self):
        """Puts the pointer in the window's corner, off every control, so nothing shows a hover."""
        self.ctl("move", "4", str(self.height - 4))

    def resize(self, height: int):
        self.height = height
        self.ctl("resize", str(self.size[0]), str(height))
        time.sleep(1.0)
        self.park_pointer()

    def snap(self, name: str) -> Image.Image:
        """Shoots the window, after a moment for it to settle."""
        time.sleep(0.6)
        self.ctl("shot", name)
        return Image.open(self.shots / f"{name}.png")

    def save(self, image: Image.Image, name: str):
        image.save(DOCS / f"{name}.png", optimize=True)
        print(f"docs/tern/{name}.png {image.width}x{image.height}")

    def until(self, check, what: str, timeout=8.0):
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() >= deadline:
                raise RuntimeError(f"timed out waiting for {what}")
            time.sleep(0.2)

    def dashboard_block(self) -> dict | None:
        return next((b for b in self.blocks() if str(b.get("program", "")).startswith("codexbar-agentview.dashboard")), None)

    def open_dashboard(self):
        """Opens Agents, which places the dashboard beside the tab's panes, then zooms its pane."""
        self.ctl("plugins", "run", COMMAND + "agents")
        self.until(lambda: (b := self.dashboard_block()) and b.get("focused"), "the dashboard")
        # Its placement runs on a timer after it opens.
        time.sleep(1.5)
        self.ctl("zoom")
        self.until(lambda: any(t.get("zoomed") for s in json.loads(self.tern("ls", "--json"))["sessions"]
                               for t in s["tabs"] if t.get("shown")), "the zoomed dashboard")
        self.show_tab("agents", run=False)

    def show_tab(self, tab: str, run=True):
        if run:
            self.ctl("plugins", "run", COMMAND + tab)

        def shown():
            tabs = sorted(((e["rect"][0], e["path"]) for e in self.layout()
                           if e["visible"] and e["rect"][2] > 0 and re.search(TAB, e["path"])))
            return len(tabs) == len(TABS) and ".on" in tabs[TABS.index(tab)][1].rsplit(">", 1)[-1]

        self.until(shown, f"the {tab} tab")
        time.sleep(0.8)

    def widget_bar(self):
        """The bar's start with the Claude widget's details open over it."""
        x, y, w, h = self.visible(WIDGET)[0]
        self.ctl("move", f"{x + w / 2:.1f}", f"{y + h / 2:.1f}")
        self.until(lambda: self.visible(DETAILS), "the Claude widget's details")
        details = self.visible(DETAILS)[0]
        right = max(r[0] + r[2] for r in self.visible(WIDGET))
        image = self.snap("widget-bar")
        self.save(image.crop((0, round(details[1]) - 18, round(right) + 7, image.height)), "widget-bar")
        self.park_pointer()

    def usage(self):
        """Usage in a window grown until every provider fits above the status bar."""
        self.show_tab("usage")
        tall = 2000
        self.resize(tall)
        bottom = max(r[1] + r[3] for r in self.visible(CARD))
        dock = min(r[1] for r in self.visible(DOCK))
        self.resize(round(tall - (dock - bottom) + USAGE_GAP))
        self.save(self.snap("usage"), "usage")
        self.resize(self.size[1])

    def film(self):
        self.height = self.size[1]
        self.tern("focus", str(self.tabs["atlas"]))
        time.sleep(2.5)
        self.widget_bar()

        self.open_dashboard()
        # The first row is selected, its buttons open under it.
        self.save(self.snap("agents"), "agents")
        self.usage()
        self.show_tab("history")
        self.save(self.snap("history"), "history")
        # The row back on you leads, its details (what happened since it was set aside) open.
        self.show_tab("waiting")
        self.ctl("key", "Space")
        time.sleep(0.6)
        self.save(self.snap("waiting"), "waiting")
        self.ctl("key", "Space")

        self.show_tab("agents")
        self.ctl("plugins", "run", COMMAND + "settings")
        self.wait_shown(PREFS)
        self.save(self.snap("settings"), "settings")


if __name__ == "__main__":
    Shots(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/codexbar-agentview-shots-tern").resolve()).run()
