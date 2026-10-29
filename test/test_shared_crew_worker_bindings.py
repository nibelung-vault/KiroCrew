"""Worker session keys inherit their run's admission binding; apps cannot cancel work."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import routes_admission


def _service() -> ag.AdmissionService:
    limits = effective_limits(
        shared_crew_config_from({"admission_enabled": True}), auto_crew_concurrency=2
    )
    return ag.AdmissionService(None, limits=limits)


@pytest.mark.parametrize(
    ("bound", "worker"),
    [
        ("taskrunner:t1", "taskrunner:t1:runtime"),
        ("taskrunner:t1", "taskrunner:t1:autoapprove"),
        ("cron:job1", "cron:job1:run-7"),
    ],
)
def test_a_run_worker_key_inherits_the_run_binding(bound, worker):
    svc = _service()
    svc.bind_session(bound, "p_alice", "w-root")
    assert svc.budget_for_session(worker) == "p_alice"
    assert svc.root_for_session(worker) == "w-root"


@pytest.mark.parametrize(
    ("bound", "other"),
    [
        ("slack:C1", "slack:C1:1700000000.1"),
        ("dashboard:tab", "dashboard:tab:x"),
        ("taskrunner:t1", "taskrunner:t10:runtime"),
    ],
)
def test_other_keys_never_inherit(bound, other):
    svc = _service()
    svc.bind_session(bound, "p_alice", "w-root")
    assert svc.budget_for_session(other) is None
    assert svc.root_for_session(other) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "handler"),
    [
        ("POST", "/api/shared-crew/work/w1/cancel", routes_admission.api_work_cancel),
        ("GET", "/api/shared-crew/work?session_key=dashboard:bob", routes_admission.api_work),
    ],
)
async def test_an_app_token_cannot_cancel_or_list_other_sessions_work(method, path, handler):
    request = make_mocked_request(method, path, match_info={"work_id": "w1"})
    request["app"] = "demo"
    resp = await handler(request)
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "app_token_not_allowed"
