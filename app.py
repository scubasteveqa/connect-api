"""
Shiny for Python app that calls the Connect API endpoint
GET /v1/content/{guid} and verifies the response.

The GUID can come from (in priority order):
  1. The text input in the UI (user pastes a GUID or a full content URL)
  2. The browser URL: query string (?content_id=<guid>), pathname, or hash
  3. document.referrer (catches iframe-embedded dashboards where the
     parent URL carries the GUID, e.g.
     https://staging.connect.posit.cloud/<org>/content/<guid>)
"""

import json
import os
import re
from datetime import datetime
from urllib.parse import parse_qs

from posit import connect
from shiny import App, Inputs, Outputs, Session, reactive, render, ui


GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
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


def extract_guid(text: str | None) -> str | None:
    if not text:
        return None
    match = GUID_RE.search(text)
    return match.group(0) if match else None


def parse_iso8601(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def to_dict(content) -> dict:
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
            "passed": isinstance(guid_value, str)
            and bool(GUID_RE.fullmatch(guid_value)),
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


# Tiny JS shim: send document.referrer to the server as a Shiny input so
# we can auto-detect the GUID from the dashboard URL when this app is
# embedded in an iframe.
REFERRER_SHIM = ui.tags.script(
    """
    document.addEventListener('DOMContentLoaded', function () {
        function send() {
            if (window.Shiny && Shiny.setInputValue) {
                Shiny.setInputValue('referrer', document.referrer || '',
                                    {priority: 'event'});
            } else {
                setTimeout(send, 100);
            }
        }
        send();
    });
    """
)


app_ui = ui.page_fluid(
    ui.tags.style(
        """
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
               max-width: 960px; margin: 24px auto; padding: 0 16px; }
        .summary-pass { background: #d4edda; color: #155724; padding: 12px;
                        border-radius: 4px; font-weight: 600; }
        .summary-fail { background: #f8d7da; color: #721c24; padding: 12px;
                        border-radius: 4px; font-weight: 600; }
        .summary-info { background: #d1ecf1; color: #0c5460; padding: 12px;
                        border-radius: 4px; font-weight: 600; }
        .source-note { color: #555; font-size: 0.9em; margin-top: 4px; }
        table.checks { width: 100%; border-collapse: collapse; margin-top: 12px; }
        table.checks th, table.checks td { padding: 8px 12px; text-align: left;
                                           border-bottom: 1px solid #eee;
                                           vertical-align: top; }
        table.checks tr.pass td:first-child::before { content: "PASS  ";
                                                      color: #28a745;
                                                      font-weight: 700; }
        table.checks tr.fail td:first-child::before { content: "FAIL  ";
                                                      color: #dc3545;
                                                      font-weight: 700; }
        pre.raw { background: #f6f8fa; padding: 12px; border-radius: 4px;
                  max-height: 400px; overflow: auto; font-size: 0.85em; }
        .input-row { display: flex; gap: 8px; align-items: stretch;
                     margin: 12px 0; }
        .input-row .form-group { flex: 1; margin: 0; }
        """
    ),
    REFERRER_SHIM,
    ui.h2("Connect API content verifier"),
    ui.p(
        "Calls ",
        ui.tags.code("GET /v1/content/{guid}"),
        " on the configured Connect server and verifies the response.",
    ),
    ui.div(
        ui.input_text(
            "guid_input",
            None,
            placeholder="Paste a content GUID, or a full URL containing one",
            width="100%",
        ),
        ui.input_action_button("verify", "Verify", class_="btn-primary"),
        class_="input-row",
    ),
    ui.output_ui("source_note"),
    ui.output_ui("summary"),
    ui.output_ui("checks_table"),
    ui.output_ui("raw_response_section"),
)


def server(input: Inputs, output: Outputs, session: Session):
    detected_source: reactive.Value[str] = reactive.Value("")

    @reactive.calc
    def auto_detected_guid() -> tuple[str | None, str]:
        """Best-effort GUID extraction from the browser URL/referrer.

        Returns (guid, source-label).
        """
        try:
            search = input[".clientdata_url_search"]()
        except Exception:
            search = ""
        if search:
            params = parse_qs(search.lstrip("?"))
            for key in ("content_id", "guid"):
                if key in params and params[key]:
                    guid = extract_guid(params[key][0]) or params[key][0]
                    if GUID_RE.fullmatch(guid):
                        return guid, f"URL query string ({key})"
            guid = extract_guid(search)
            if guid:
                return guid, "URL query string"

        try:
            pathname = input[".clientdata_url_pathname"]()
        except Exception:
            pathname = ""
        guid = extract_guid(pathname)
        if guid:
            return guid, "URL path"

        try:
            url_hash = input[".clientdata_url_hash"]()
        except Exception:
            url_hash = ""
        guid = extract_guid(url_hash)
        if guid:
            return guid, "URL hash"

        try:
            referrer = input["referrer"]()
        except Exception:
            referrer = ""
        guid = extract_guid(referrer)
        if guid:
            return guid, "document.referrer"

        env_guid = extract_guid(os.environ.get("TARGET_CONTENT_ID", ""))
        if env_guid:
            return env_guid, "TARGET_CONTENT_ID env var"

        return None, ""

    @reactive.effect
    def _autofill():
        # Populate the input box once we've detected something.
        guid, source = auto_detected_guid()
        if guid and not input.guid_input():
            ui.update_text("guid_input", value=guid)
            detected_source.set(source)

    @reactive.calc
    def requested_guid() -> str | None:
        raw = input.guid_input()
        return extract_guid(raw)

    @reactive.calc
    @reactive.event(input.verify, auto_detected_guid, ignore_none=False)
    def fetch_result():
        guid = requested_guid()
        if not guid:
            return {"status": "no_guid"}

        server_url = os.environ.get("CONNECT_SERVER")
        api_key = os.environ.get("CONNECT_API_KEY")
        if not server_url or not api_key:
            return {
                "status": "no_creds",
                "detail": "CONNECT_SERVER and CONNECT_API_KEY must be set in the app environment",
            }

        try:
            client = connect.Client(url=server_url, api_key=api_key)
            content = client.content.get(guid)
            body = to_dict(content)
            return {"status": "ok", "guid": guid, "body": body}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "guid": guid, "detail": str(exc)}

    @render.ui
    def source_note():
        guid = requested_guid()
        if not guid:
            return ui.div(
                "Paste a GUID above (or a full URL — the GUID will be extracted) "
                "and click Verify.",
                class_="summary-info",
            )
        src = detected_source.get()
        note = f"Verifying content GUID: {guid}"
        if src:
            note += f"  (auto-detected from {src})"
        return ui.div(note, class_="summary-info")

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
        if result.get("status") != "ok":
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
    def raw_response_section():
        result = fetch_result()
        if result.get("status") != "ok":
            return ui.HTML("")
        return ui.div(
            ui.h4("Raw response"),
            ui.tags.pre(
                json.dumps(result["body"], indent=2, default=str), class_="raw"
            ),
        )


app = App(app_ui, server)
