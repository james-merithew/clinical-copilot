"""Operational-observability regressions in the correlation/metrics middleware.

Failure modes guarded (adversarial review, Week 2):
- (ops) an unhandled 500 must still be RECORDED as a 5xx in the PHI-free metrics
  and the response must still carry x-correlation-id — otherwise the dashboard
  shows a healthy service during an outage and the failure is untraceable;
- (PHI + cardinality) unmatched request paths must be bucketed under a single
  "<unmatched>" key, never recorded verbatim (a caller can put ids/PHI in a URL,
  and verbatim keys blow up the cardinality of the "PHI-free" summary).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from fastapi.testclient import TestClient

from app import metrics_summary
from app.main import app


def _add_route(path, handler, methods=("GET",)):
    app.add_api_route(path, handler, methods=list(methods))


def _drop_route(path):
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != path]


def test_unhandled_500_is_recorded_and_carries_correlation_id():
    """A route that raises: the response is 500 WITH x-correlation-id, and the
    metrics summary shows the 5xx (pre-fix the record ran after the raising
    call, so neither happened)."""
    async def _boom(request: Request):
        raise RuntimeError("kaboom")

    _add_route("/_test_boom", _boom)
    try:
        # raise_server_exceptions=False so TestClient returns the 500 response
        # instead of re-raising it (we assert on the response the client sees).
        with TestClient(app, raise_server_exceptions=False) as client:
            metrics_summary.reset()
            resp = client.get("/_test_boom")
        assert resp.status_code == 500
        assert resp.headers.get("x-correlation-id")
        summ = metrics_summary.summary()
        entry = summ["endpoints"].get("GET /_test_boom")
        assert entry is not None, summ["endpoints"]
        assert entry["errors_5xx"] >= 1
    finally:
        _drop_route("/_test_boom")


def test_unmatched_path_bucketed_and_matched_uses_template():
    """A matched route with an id in the path is keyed by its TEMPLATE; a random
    unmatched path is bucketed under "<unmatched>", never recorded verbatim."""
    with TestClient(app) as client:
        metrics_summary.reset()
        # Matched route (may 403/404 depending on config) but the route matches,
        # so scope['route'] is set → keyed by the template, not the raw id.
        client.get("/documents/999/page/1.png", params={"pid": 102})
        # Unmatched, id-bearing path → must be bucketed, not recorded verbatim.
        junk = f"/junk/{uuid4()}"
        client.get(junk)

    keys = list(metrics_summary.summary()["endpoints"].keys())
    assert any("/documents/{document_id}/page/{page}.png" in k for k in keys), keys
    assert any(k == "GET <unmatched>" for k in keys), keys
    # The raw id and the junk uuid must NEVER appear as (parts of) a metrics key.
    assert not any("999" in k for k in keys), keys
    assert not any("junk" in k for k in keys), keys
