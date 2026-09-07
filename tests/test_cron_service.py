import asyncio
import json

import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.add_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert job.schedule.tz == "America/Vancouver"
    assert job.state.next_run_at_ms is not None


@pytest.mark.asyncio
async def test_execute_job_records_run_history(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path, on_job=lambda _: asyncio.sleep(0))
    job = service.add_job(
        name="hist",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    await service.run_job(job.id)

    loaded = service.get_job(job.id)
    assert loaded is not None
    assert len(loaded.state.run_history) == 1
    rec = loaded.state.run_history[0]
    assert rec.status == "ok"
    assert rec.duration_ms >= 0
    assert rec.error is None


@pytest.mark.asyncio
async def test_run_history_records_errors(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"

    async def fail(_):
        raise RuntimeError("boom")

    service = CronService(store_path, on_job=fail)
    job = service.add_job(
        name="fail",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    await service.run_job(job.id)

    loaded = service.get_job(job.id)
    assert len(loaded.state.run_history) == 1
    assert loaded.state.run_history[0].status == "error"
    assert loaded.state.run_history[0].error == "boom"


@pytest.mark.asyncio
async def test_run_history_trimmed_to_max(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path, on_job=lambda _: asyncio.sleep(0))
    job = service.add_job(
        name="trim",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    for _ in range(25):
        await service.run_job(job.id)

    loaded = service.get_job(job.id)
    assert len(loaded.state.run_history) == CronService._MAX_RUN_HISTORY


@pytest.mark.asyncio
async def test_run_history_persisted_to_disk(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path, on_job=lambda _: asyncio.sleep(0))
    job = service.add_job(
        name="persist",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    await service.run_job(job.id)

    raw = json.loads((store_path.parent / "state.json").read_text())
    history = raw["states"][job.id]["runHistory"]
    assert len(history) == 1
    assert history[0]["status"] == "ok"
    assert "runAtMs" in history[0]
    assert "durationMs" in history[0]

    # Definitions stay free of runtime state.
    definitions = json.loads(store_path.read_text())
    assert "state" not in definitions["jobs"][0]

    fresh = CronService(store_path)
    loaded = fresh.get_job(job.id)
    assert len(loaded.state.run_history) == 1
    assert loaded.state.run_history[0].status == "ok"


@pytest.mark.asyncio
async def test_running_service_honors_external_disable(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    called: list[str] = []

    async def on_job(job) -> None:
        called.append(job.id)

    service = CronService(store_path, on_job=on_job)
    job = service.add_job(
        name="external-disable",
        schedule=CronSchedule(kind="every", every_ms=200),
        message="hello",
    )
    await service.start()
    try:
        # Wait slightly to ensure file mtime is definitively different
        await asyncio.sleep(0.05)
        external = CronService(store_path)
        updated = external.enable_job(job.id, enabled=False)
        assert updated is not None
        assert updated.enabled is False

        await asyncio.sleep(0.35)
        assert called == []
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_legacy_inline_state_is_migrated(tmp_path) -> None:
    """A pre-split jobs.json keeps its runtime state on first load and save."""
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text(json.dumps({
        "version": 1,
        "jobs": [{
            "id": "legacy01",
            "name": "legacy",
            "enabled": True,
            "schedule": {"kind": "every", "everyMs": 60_000},
            "payload": {"kind": "agent_turn", "message": "hi"},
            "state": {
                "nextRunAtMs": 1788872276526,
                "lastRunAtMs": 1788732451579,
                "lastStatus": "ok",
                "lastError": None,
                "runHistory": [{"runAtMs": 1788732451579, "status": "ok", "durationMs": 4}],
            },
            "createdAtMs": 1776602121430,
            "updatedAtMs": 1788732451594,
            "deleteAfterRun": False,
        }],
    }), encoding="utf-8")

    service = CronService(store_path)
    job = service.get_job("legacy01")
    assert job.state.last_run_at_ms == 1788732451579
    assert len(job.state.run_history) == 1

    # Saving relocates state and strips it from the definitions file.
    service._save_store()
    definitions = json.loads(store_path.read_text())
    assert "state" not in definitions["jobs"][0]
    assert definitions["jobs"][0]["payload"]["message"] == "hi"
    states = json.loads((store_path.parent / "state.json").read_text())["states"]
    assert states["legacy01"]["lastRunAtMs"] == 1788732451579


@pytest.mark.asyncio
async def test_execution_does_not_touch_definitions_file(tmp_path) -> None:
    """A plain run writes state only, leaving jobs.json byte- and mtime-stable."""
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path, on_job=lambda _: asyncio.sleep(0))
    job = service.add_job(
        name="quiet",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )

    before_bytes = store_path.read_bytes()
    before_mtime = store_path.stat().st_mtime
    await asyncio.sleep(0.05)

    await service.run_job(job.id)

    assert store_path.read_bytes() == before_bytes
    assert store_path.stat().st_mtime == before_mtime
    # ...while the run was still recorded.
    assert len(service.get_job(job.id).state.run_history) == 1
