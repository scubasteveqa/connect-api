"""
Shiny for Python app that calls Connect's GET /v1/content/{guid} endpoint
and renders the raw JSON response. All field-level assertions live in the
calling pytest test, not here.

The app uses `posit.connect.Client()`, which authenticates using whatever
the deployed environment provides:
  * a visitor session token forwarded by Connect when the viewer is signed
    in (preferred — no API key needed), or
  * CONNECT_SERVER / CONNECT_API_KEY env vars (used when running locally).

The GUID is read from:
  1. ?content_id=<guid> (or ?guid=<guid>) on the app URL  -- primary
  2. document.referrer  -- catches the PCC dashboard iframe case
  3. A text input in the UI  -- manual fallback
"""

import json
import os
import re
from urllib.parse import parse_qs

from posit import connect
from shiny import App, Inputs, Outputs, Session, reactive, render, ui


GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def extract_guid(text):
    if not text:
        return None
    match = GUID_RE.search(text)
    return match.group(0) if match else None


def to_dict(content):
    try:
        return dict(content)
    except Exception:
        return {"_repr": repr(content)}


def make_client():
    """
    Try visitor-token auth first (works when the viewer is signed in to
    Connect and the platform forwards their session token to the app). Fall
    back to CONNECT_SERVER/CONNECT_API_KEY env vars, then to the SDK's
    default constructor.
    """
    server_url = os.environ.get("CONNECT_SERVER")
    api_key = os.environ.get("CONNECT_API_KEY")
    if server_url and api_key:
        return connect.Client(url=server_url, api_key=api_key)
    return connect.Client()


# Tiny JS shim: surface document.referrer to the server so we can pull a
# GUID out of the parent dashboard URL when the app is iframed.
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
        pre { background: #f6f8fa; padding: 12px; border-radius: 4px;
              max-height: 600px; overflow: auto; font-size: 0.85em; }
        .status { padding: 8px 12px; border-radius: 4px; margin: 12px 0;
                  font-family: monospace; }
        .status.ok    { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .status.info  { background: #d1ecf1; color: #0c5460; }
        .input-row { display: flex; gap: 8px; align-items: stretch;
                     margin: 12px 0; }
        .input-row .form-group { flex: 1; margin: 0; }
        """
    ),
    REFERRER_SHIM,
    ui.h2("Connect API content verifier"),
    ui.p("Calls ", ui.tags.code("GET /v1/content/{guid}"), " and displays the JSON response."),
    ui.div(
        ui.input_text(
            "guid_input",
            None,
            placeholder="Paste a content GUID, or a full URL containing one",
            width="100%",
        ),
        ui.input_action_button("verify", "Fetch", class_="btn-primary"),
        class_="input-row",
    ),
    ui.output_ui("status_line"),
    # The test reads this element. Keep the id stable.
    ui.tags.pre(ui.output_text("response_json"), id="response-json"),
    ui.tags.pre(ui.output_text("error_text"), id="error-text"),
)


def server(input: Inputs, output: Outputs, session: Session):
    @reactive.calc
    def auto_detected_guid():
        try:
            search = input[".clientdata_url_search"]() or ""
        except Exception:
            search = ""
        if search:
            params = parse_qs(search.lstrip("?"))
            for key in ("content_id", "guid"):
                if key in params and params[key]:
                    guid = extract_guid(params[key][0]) or params[key][0]
                    if GUID_RE.fullmatch(guid):
                        return guid
            guid = extract_guid(search)
            if guid:
                return guid

        try:
            referrer = input["referrer"]() or ""
        except Exception:
            referrer = ""
        return extract_guid(referrer)

    @reactive.effect
    def _autofill():
        guid = auto_detected_guid()
        if guid and not input.guid_input():
            ui.update_text("guid_input", value=guid)

    @reactive.calc
    def requested_guid():
        return extract_guid(input.guid_input())

    @reactive.calc
    @reactive.event(input.verify, auto_detected_guid, ignore_none=False)
    def fetch_result():
        guid = requested_guid()
        if not guid:
            return {"status": "no_guid"}
        try:
            client = make_client()
            content = client.content.get(guid)
            return {"status": "ok", "guid": guid, "body": to_dict(content)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "guid": guid, "detail": str(exc)}

    @render.ui
    def status_line():
        result = fetch_result()
        if result["status"] == "no_guid":
            return ui.div(
                "Paste a GUID (or a URL containing one) and click Fetch.",
                class_="status info",
            )
        if result["status"] == "error":
            return ui.div(f"Fetch failed for {result['guid']}", class_="status error")
        return ui.div(f"GET /v1/content/{result['guid']}", class_="status ok")

    @render.text
    def response_json():
        result = fetch_result()
        if result.get("status") == "ok":
            return json.dumps(result["body"], indent=2, default=str)
        return ""

    @render.text
    def error_text():
        result = fetch_result()
        if result.get("status") == "error":
            return result["detail"]
        return ""


app = App(app_ui, server)
