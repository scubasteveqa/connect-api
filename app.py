"""
Shiny for Python app that calls the Connect API for the running content
itself and dumps two responses to the page:

  * `<pre id="content-json">`     -- GET /v1/content/{this content's guid}
  * `<pre id="associations-json">` -- the SNOWFLAKE OAuth association on
                                      that content (or {} if none)

This app verifies the fix in posit-hosted/vivid-blender#1840 which added
`content_url` to the content payload and `app_guid` to OAuth associations.

The app does no assertions — all field checks live in the calling pytest
test (tests/integration/test_content_api.py).

Deployment notes (manual publish):
  * Attach a SNOWFLAKE OAuth integration to this content before publishing
    so `associations.find_by(integration_type=SNOWFLAKE)` returns a record.
  * No env vars or secrets needed. The SDK's default Client() picks up the
    in-content credentials Connect provides automatically.
"""

import json
from datetime import date, datetime

from posit import connect
from posit.connect import oauth as oauth_module  # for OAuthIntegrationType
from shiny import App, Inputs, Outputs, Session, reactive, render, ui


SNOWFLAKE_TYPE = getattr(
    getattr(oauth_module, "types", oauth_module),
    "OAuthIntegrationType",
    None,
)


def _coerce(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def to_dict(obj):
    """Best-effort conversion of a posit-sdk record (dict-like) to a plain dict."""
    if obj is None:
        return {}
    try:
        return {k: _coerce(v) for k, v in dict(obj).items()}
    except Exception:
        return {"_repr": repr(obj)}


def fetch_content_and_association():
    client = connect.Client()
    current_content = client.content.get()  # self
    content_payload = to_dict(current_content)

    association_payload = {}
    association_error = None
    try:
        snowflake_value = (
            SNOWFLAKE_TYPE.SNOWFLAKE
            if SNOWFLAKE_TYPE is not None and hasattr(SNOWFLAKE_TYPE, "SNOWFLAKE")
            else "snowflake"
        )
        sf_assoc = current_content.oauth.associations.find_by(
            integration_type=snowflake_value
        )
        association_payload = to_dict(sf_assoc)
    except Exception as exc:  # noqa: BLE001
        association_error = f"{type(exc).__name__}: {exc}"

    return content_payload, association_payload, association_error


app_ui = ui.page_fluid(
    ui.tags.style(
        """
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
               max-width: 980px; margin: 24px auto; padding: 0 16px; }
        h3 { margin-top: 28px; }
        pre { background: #f6f8fa; padding: 12px; border-radius: 4px;
              max-height: 480px; overflow: auto; font-size: 0.85em;
              white-space: pre-wrap; word-break: break-all; }
        .err { background: #f8d7da; color: #721c24; padding: 8px 12px;
               border-radius: 4px; font-family: monospace; margin: 8px 0; }
        """
    ),
    ui.h2("Connect API content + OAuth association response"),
    ui.p(
        "Verifies posit-hosted/vivid-blender#1840: ",
        ui.tags.code("content_url"),
        " on the content payload and ",
        ui.tags.code("app_guid"),
        " on the SNOWFLAKE OAuth association.",
    ),
    ui.h3("Content (client.content.get())"),
    ui.tags.pre(ui.output_text("content_json"), id="content-json"),
    ui.h3("Snowflake OAuth association (find_by integration_type=SNOWFLAKE)"),
    ui.tags.pre(ui.output_text("associations_json"), id="associations-json"),
    ui.output_ui("error_block"),
    ui.tags.pre(ui.output_text("error_text"), id="error-text"),
)


def server(input: Inputs, output: Outputs, session: Session):
    @reactive.calc
    def result():
        try:
            content, assoc, assoc_err = fetch_content_and_association()
            return {
                "status": "ok",
                "content": content,
                "association": assoc,
                "association_error": assoc_err,
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}

    @render.text
    def content_json():
        r = result()
        if r["status"] != "ok":
            return ""
        return json.dumps(r["content"], indent=2, default=str)

    @render.text
    def associations_json():
        r = result()
        if r["status"] != "ok":
            return ""
        return json.dumps(r["association"], indent=2, default=str)

    @render.ui
    def error_block():
        r = result()
        msgs = []
        if r["status"] == "error":
            msgs.append(r["detail"])
        elif r.get("association_error"):
            msgs.append(f"association fetch error: {r['association_error']}")
        if not msgs:
            return ui.HTML("")
        return ui.div(*[ui.div(m, class_="err") for m in msgs])

    @render.text
    def error_text():
        # Machine-readable single-string error for the test to scrape.
        r = result()
        if r["status"] == "error":
            return r["detail"]
        if r.get("association_error"):
            return r["association_error"]
        return ""


app = App(app_ui, server)
