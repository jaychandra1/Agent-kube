"""Renders an RCA as HTML. Saved to REPORT_DIR, the last one is served at /rca/latest."""
import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

import timefmt
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html", "j2"]))
REPORT_DIR = os.getenv("REPORT_DIR", "/tmp/reports")

# last report this pod rendered
LATEST_HTML = ""


def render_rca(rca: dict) -> tuple[str, str]:
    """Returns (file name, html)."""
    global LATEST_HTML
    now = timefmt.now()
    page = env.get_template("rca.html.j2").render(r=rca, generated=timefmt.fmt(now))
    os.makedirs(REPORT_DIR, exist_ok=True)
    name = f"rca-{now.strftime('%Y%m%d-%H%M%S')}.html"
    with open(os.path.join(REPORT_DIR, name), "w", encoding="utf-8") as f:
        f.write(page)
    LATEST_HTML = page
    return name, page
