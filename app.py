"""
Shiny for Python app that calls the Connect API endpoint
GET /v1/content/{guid} and verifies the response.

The GUID is read from the URL query string, e.g.
    https://<this-app>/?content_id=abc-123
"""

import os
import re
from datetime import datetime
from urllib.parse import parse_qs

from posit import connect
from shiny import App, Inputs, Outputs, Session, reactive, render, ui


GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

REQUIRED_FIELDS = [
    "guid",
    "name",
    "title",
    "app_mode",
    "owner_guid",
    "created_time",
    "last_deployed_time",
]

KNOWN_APP_MODES = {
    "shiny",
    "python-shiny",
    "python-streamlit",
    "python-dash",
    "python-bokeh",
    "python-fastapi",
    "python-api",
    "rmd-static",
    "rmd-shiny",
    "quarto-static",
    "quarto-shiny",
    "static",
    "jupyter-static",
    "jupyter-voila",
    "tensorflow-saved-model",
    "api",
}


def parse_iso8601(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def to_dict(content) -> dict:
    """posit-sdk Content behaves like a dict but make sure we materialize one."""
    try:
        return dict(content)
    except Exception:
        return {k: getattr(content, k, None) for k in REQUIRED_FIELDS}


def run_checks(requested_guid: str, body: dict) -> list[dict]:
    checks = []

    for field in REQUIRED_FIELDS:
        present = field in body and body[field] not in (None, "")
        checks.append(
            {
                "name": f"required field `{field}` present",
                "passed": present,
                "detail": repr(body.get(field)) if present else "missing/empty",
            }
        )

    guid_value = body.get("guid")
    checks.append(
        {
            "name": "response.guid is a valid UUID",
            "passed": isinstance(guid_value, str) and bool(GUID_RE.match(guid_value)),
            "detail": repr(guid_value),
        }
    )

    checks.append(
        {
            "name": "response.guid matches requested GUID",
            "passed": guid_value == requested_guid,
            "detail": f"requested={requested_guid!r}, got={guid_value!r}",
        }
    )

    app_mode = body.get("app_mode")
    checks.append(
        {
            "name": "app_mode is a known value",
            "passed": app_mode in KNOWN_APP_MODES,
            "detail": repr(app_mode),
        }
    )

    for ts_field in ("created_time", "last_deployed_time"):
        value = body.get(ts_field)
        checks.append(
            {
                "name": f"{ts_field} is ISO 8601",
                "passed": parse_iso8601(value),
                "detail": repr(value),
            }
        )

    return checks


app_ui = ui.page_fluid(
    ui.tags.style(
        """
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
        .summary-pass { background: #d4edda; color: #155724; padding: 12px;
                        border-radius: 4px; font-weight: 600; }
        .summary-fail { background: #f8d7da; color: #721c24; padding: 12px;
                        border-radius: 4px; font-weight: 600; }
        .summary-info { background: #d1ecf1; color: #0c5460; padding: 12px;
                        border-radius: 4px; font-weight: 600; }
        table.checks { width: 100%; border-collapse: collapse; margin-top: 12px; }
        table.checks th, table.checks td { padding: 8px 12px; text-align: left;
                                           border-bottom: 1px solid #eee; }
        table.checks tr.pass td:first-child::before { content: "✓ "; color: #28a745; }
        table.checks tr.fail td:first-child::before { content: "✗ "; color: #dc3545; }
        pre.raw { background: #f6f8fa; padding: 12px; border-radius: 4px;
                  max-height: 400px; overflow: auto; }
        """
    ),
    ui.h2("Connect API content verifier"),
    ui.p(
        "Calls ",
        ui.tags.code("GET /v1/content/{guid}"),
        " on the configured Connect server and checks the response shape.",
    ),
    ui.output_ui("guid_banner"),
    ui.output_ui("summary"),
    ui.output_ui("checks_table"),
    ui.h4("Raw response"),
    ui.output_ui("raw_response"),
)


def server(input: Inputs, output: Outputs, session: Session):
    @reactive.calc
    def requested_guid() -> str | None:
        search = input[".clientdata_url_search"]()
        if not search:
            return None
        params = parse_qs(search.lstrip("?"))
        for key in ("content_id", "guid"):
            if key in params and params[key]:
                return params[key][0]
        return None

    @reactive.calc
    def fetch_result():
        guid = requested_guid()
        if not guid:
            return {"status": "no_guid"}

        server_url = os.environ.get("CONNECT_SERVER")
        api_key = os.environ.get("CONNECT_API_KEY")
        if not server_url or not api_key:
            return {
                "status": "no_creds",
                "detail": "CONNECT_SERVER and CONNECT_API_KEY must be set",
            }

        try:
            client = connect.Client(url=server_url, api_key=api_key)
            content = client.content.get(guid)
            body = to_dict(content)
            return {"status": "ok", "guid": guid, "body": body}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "guid": guid, "detail": str(exc)}

    @render.ui
    def guid_banner():
        guid = requested_guid()
        if not guid:
            return ui.div(
                "No GUID supplied. Append ",
                ui.tags.code("?content_id=<guid>"),
                " to the URL.",
                class_="summary-info",
            )
        return ui.div(f"Verifying content GUID: {guid}", class_="summary-info")

    @render.ui
    def summary():
        result = fetch_result()
        if result["status"] == "no_guid":
            return ui.HTML("")
        if result["status"] == "no_creds":
            return ui.div(result["detail"], class_="summary-fail")
        if result["status"] == "error":
            return ui.div(
                f"API call failed: {result['detail']}", class_="summary-fail"
            )

        checks = run_checks(result["guid"], result["body"])
        failed = [c for c in checks if not c["passed"]]
        if failed:
            return ui.div(
                f"{len(failed)} of {len(checks)} checks failed",
                class_="summary-fail",
            )
        return ui.div(
            f"All {len(checks)} checks passed", class_="summary-pass"
        )

    @render.ui
    def checks_table():
        result = fetch_result()
        if result["status"] != "ok":
            return ui.HTML("")
        checks = run_checks(result["guid"], result["body"])
        rows = [
            ui.tags.tr(
                ui.tags.td(c["name"]),
                ui.tags.td(c["detail"]),
                class_="pass" if c["passed"] else "fail",
            )
            for c in checks
        ]
        return ui.tags.table(
            ui.tags.thead(ui.tags.tr(ui.tags.th("Check"), ui.tags.th("Detail"))),
            ui.tags.tbody(*rows),
            class_="checks",
        )

    @render.ui
    def raw_response():
        result = fetch_result()
        if result["status"] != "ok":
            return ui.HTML("")
        import json

        return ui.tags.pre(
            json.dumps(result["body"], indent=2, default=str), class_="raw"
        )


app = App(app_ui, server)
