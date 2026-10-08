"""Optional hosted traffic-analysis sensor; self-hosted builds load nothing."""

import re
from html import escape

TRAFFIC_SENSOR_SRC = "https://edge.tin.computer/sdk/v1.js"
TRAFFIC_SENSOR_MARKER = "<!--TRAFFIC_SENSOR_SCRIPT-->"
_SITE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_traffic_sensor_site(value: str | None) -> str | None:
    if value is None:
        return None
    if not _SITE_ID.fullmatch(value):
        raise ValueError("Traffic sensor site id must be lowercase letters, digits and hyphens")
    return value


def traffic_sensor_script(settings) -> str:
    site = getattr(settings, "traffic_sensor_site", None)
    if not site:
        return ""
    # crossorigin="anonymous": the script request carries no cookies.
    return (
        f'<script async src="{TRAFFIC_SENSOR_SRC}" crossorigin="anonymous" '
        f'data-site="{escape(site, quote=True)}"></script>'
    )
