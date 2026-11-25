"""Answering in the Dashboard through its HTTP interface, as a separate process (DN006 §8.10,
T357). The server is ``tsw status serve``; the worker's session inbox is a fake one in the test's
own ``CLAUDE_CONFIG_DIR`` (tests/fixtures/fake_inbox.py)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_inbox import TOKEN, FakeInbox
from tests.machine_harness import Harness
from tests.surface.test_direct_surface import App, _serve, handed_back  # pyright: ignore[reportPrivateUsage]
from tsw.home import Home

pytestmark = pytest.mark.use_case("UC-DIRECT-013")


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


@pytest.fixture
def app(h: Harness) -> Iterator[App]:
    yield from _serve("status", "serve")


def signed_in(app: App) -> None:
    status, headers, _ = app.request("GET", f"/?token={app.token}")
    assert status == 303
    app.cookie = headers["Set-Cookie"].split(";")[0]


def post(app: App, task: str, body: dict[str, Any], token: bool = True) -> tuple[int, Any]:
    headers = {"Origin": f"http://127.0.0.1:{app.port}"}
    if token:
        headers["X-TSW-Token"] = app.token
    status, _, raw = app.request("POST", f"/api/tasks/{task}/answer", body, headers)
    return status, json.loads(raw)


def test_the_page_answers_through_its_own_server_into_the_worker_session(
    h: Harness, app: App
) -> None:
    h.register("T001")
    h.plan("T001")
    worker = h.dispatch("T001")
    request_id = h.run(
        "ask_user", worker, task_id="T001", question="选哪个？", options=["方案 A", "方案 B"]
    )["events"][0]["data"]["request_id"]
    handed_back(h)
    inbox = FakeInbox(Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions", str(worker.session_id))
    try:
        signed_in(app)
        status, _, page = app.request("GET", "/")
        assert status == 200
        text = page.decode("utf-8")
        assert f'data-request="{request_id}"' in text and text.count('class="qa-opt"') == 2
        body = {"request_id": request_id, "text": "方案 B", "command_id": "dashboard-answer-s1"}
        # The same admission as the other buttons: the page's token in the header.
        assert post(app, "T001", body, token=False)[0] == 403
        status, result = post(app, "T001", body)
        assert (status, result["state"]) == (200, "sent")
        assert TOKEN not in json.dumps(result)
        [sent] = inbox.wait(1)
        content = f"[TaskSteward Dashboard 答复 {request_id}]\n方案 B"
        assert (sent["from"], sent["message"]["content"]) == ("tsw-dashboard", content)
        status, _, raw = app.request(
            "GET", f"/api/tasks/T001/answer?request_id={request_id}&command_id=dashboard-answer-s1"
        )
        assert (status, json.loads(raw)["state"]) == (200, "sent")
        # Without the cookie the progress is not shown.
        cookie, app.cookie = app.cookie, ""
        assert app.request("GET", f"/api/tasks/T001/answer?request_id={request_id}")[0] == 403
        app.cookie = cookie
        # A retry is not delivered again.
        status, result = post(app, "T001", body)
        assert (status, result["duplicate"]) == (200, True)
        assert len(inbox.wait(2, timeout=1)) == 1
    finally:
        inbox.close()
