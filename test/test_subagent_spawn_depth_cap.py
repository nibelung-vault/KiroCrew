"""The lineage depth a spawn records, and the admission cap on it.

A root session's child is depth 1 and a sub-agent's child is one deeper than its
parent. ``SubagentManager.spawn`` refuses a child that would sit past
``_MAX_SPAWN_DEPTH``, so a recursive ``spawn_run`` chain stops at the cap instead
of holding every reserved child slot.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

from kiro_crew.execution_context import execution_for_store
from kiro_crew.subagent import _MAX_SPAWN_DEPTH, SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission.types import PreparedSpawn

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _manager_with_parent(parent_depth: int) -> tuple[SubagentManager, str]:
    """A manager holding one live sub-agent at *parent_depth*; returns its session key."""
    manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn())
    parent = SubagentInfo(
        execution_context=execution_for_store(""),
        id="parent01",
        task="parent work",
        parent_session_key="dashboard:root",
        depth=parent_depth,
    )
    manager._agents[parent.id] = parent
    return manager, f"subagent:{parent.id}"


@pytest.mark.asyncio
async def test_child_of_root_session_is_depth_one() -> None:
    manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn())
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("root child", parent_session_key="dashboard:root")
        assert info is not None
        await manager._tasks[info.id]

    assert info.error == ""
    assert info.depth == 1


@pytest.mark.asyncio
async def test_child_at_the_cap_is_admitted_one_deeper_than_its_parent() -> None:
    manager, parent_key = _manager_with_parent(_MAX_SPAWN_DEPTH - 1)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("deepest allowed", parent_session_key=parent_key)
        assert info is not None
        await manager._tasks[info.id]

    assert info.error == ""
    assert info.depth == _MAX_SPAWN_DEPTH


@pytest.mark.asyncio
async def test_queued_child_keeps_its_depth_after_the_parent_record_is_gone() -> None:
    """The row a spawn persists carries its depth, so a drain that cannot
    find the parent's live record keeps the count instead of restarting at 2."""
    manager, parent_key = _manager_with_parent(_MAX_SPAWN_DEPTH - 1)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        prepared = manager.prepare_spawn("queued deep child", parent_session_key=parent_key)
        assert isinstance(prepared, PreparedSpawn)
        assert prepared.params["_depth"] == _MAX_SPAWN_DEPTH
        assert prepared.record.params["_depth"] == _MAX_SPAWN_DEPTH

        del manager._agents["parent01"]
        params = {k: v for k, v in prepared.params.items() if k != "task"}
        info = manager.spawn(prepared.params["task"], **params, _from_queue=True)
        assert info is not None
        await manager._tasks[info.id]

    assert info.error == ""
    assert info.depth == _MAX_SPAWN_DEPTH


@pytest.mark.asyncio
async def test_a_carried_depth_never_lowers_what_the_live_parent_proves() -> None:
    manager, parent_key = _manager_with_parent(_MAX_SPAWN_DEPTH - 1)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("stale carried depth", parent_session_key=parent_key, _depth=1)
        assert info is not None
        await manager._tasks[info.id]

    assert info.error == ""
    assert info.depth == _MAX_SPAWN_DEPTH


def test_a_carried_depth_past_the_cap_is_refused_without_a_live_parent() -> None:
    """A retry of a run that sat at the cap, whose parent record is gone, stays
    refused for its children: the retry carries its depth back in."""
    manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn())
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn(
            "too deep", parent_session_key="subagent:gone", _depth=_MAX_SPAWN_DEPTH + 1
        )

    assert info is not None
    assert f"nesting cap of {_MAX_SPAWN_DEPTH}" in info.error
    assert manager._tasks == {}


def test_a_child_of_a_continuation_counts_from_the_continuations_depth() -> None:
    """A continuation runs on its original run's session key under a new id;
    after a restart only the continuation is live, and it is the parent."""
    manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn())
    manager._agents["cont02"] = SubagentInfo(
        execution_context=execution_for_store(""),
        id="cont02",
        task="follow-up",
        parent_session_key="dashboard:root",
        conversation_key="subagent:orig01",
        depth=_MAX_SPAWN_DEPTH,
    )
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("one level too deep", parent_session_key="subagent:orig01")

    assert info is not None
    assert f"nesting cap of {_MAX_SPAWN_DEPTH}" in info.error
    assert manager._tasks == {}


def test_child_past_the_cap_is_refused_and_audited() -> None:
    manager, parent_key = _manager_with_parent(_MAX_SPAWN_DEPTH)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel") as mock_sel:
        mock_sel.return_value.log_tool_invocation = MagicMock()
        info = manager.spawn("one level too deep", parent_session_key=parent_key)

    assert info is not None
    assert info.done is True
    assert f"nesting cap of {_MAX_SPAWN_DEPTH}" in info.error
    assert info.depth == _MAX_SPAWN_DEPTH + 1
    assert manager._tasks == {}
    assert set(manager._agents) == {"parent01"}
    outcomes = [
        c.kwargs["outcome"] for c in mock_sel.return_value.log_tool_invocation.call_args_list
    ]
    assert outcomes == ["rejected_depth_cap"]


@pytest.mark.asyncio
async def test_a_retry_carries_the_failed_runs_depth() -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.messaging import api_spawn_retry
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    old = SimpleNamespace(
        id="a1",
        task="t",
        _raw_task="t",
        parent_session_key="subagent:gone",
        agent="",
        max_turns=0,
        cwd="",
        model="",
        reasoning_effort="",
        backend=None,
        approval_mode="",
        silent=False,
        delegation={},
        include_memory=True,
        include_lessons=True,
        include_project=True,
        memory_store="",
        crew="",
        done=True,
        outcome="failed",
        depth=_MAX_SPAWN_DEPTH,
        execution_context=ExecutionContext(None, MemoryStoreRef("default"), "template", "k"),
    )
    mgr = MagicMock()
    mgr.get.return_value = old
    mgr.spawn.return_value = SimpleNamespace(id="a2", done=False, error="")
    request = MagicMock()
    request.app = {"state": SimpleNamespace(subagents=mgr)}
    request.headers = {}
    request.match_info = {"agent_id": "a1"}
    await api_spawn_retry(request)
    assert mgr.spawn.call_args.kwargs["_depth"] == _MAX_SPAWN_DEPTH
