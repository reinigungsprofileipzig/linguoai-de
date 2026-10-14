from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from linguoai.web_jobs import JobConflict, JobLimitReached, JobNotFound, JobStore, LeaseLost


@pytest.fixture
def store(tmp_path):
    return JobStore(
        tmp_path / "jobs.sqlite", lease_seconds=10, retention_seconds=30, max_active_per_owner=20
    )


def queued(store, owner="alice", **options):
    job = store.create(owner, "video.mp4", {"asr_engine": "whisper", **options})
    return store.queue(job["id"], owner, "input-ref", 42)


def claimed(store, worker="worker"):
    job = queued(store)
    lease = store.claim(worker)
    assert lease["id"] == job["id"]
    return lease


def add_results(store, lease):
    for name in ("video", "original_srt", "translated_srt"):
        store.add_artifact(
            lease["id"], lease["worker_id"], lease["lease_token"], name, f"result-{name}", 12
        )


def test_queue_survives_restart_and_owner_isolation(store):
    job = queued(store)
    restarted = JobStore(store.db_path)
    assert restarted.get(job["id"], "alice")["options"] == {"asr_engine": "whisper"}
    for operation in (
        lambda: restarted.get(job["id"], "bob"),
        lambda: restarted.cancel(job["id"], "bob"),
        lambda: restarted.queue(job["id"], "bob", "other", 3),
        lambda: restarted.fail_upload(job["id"], "bob", "failed"),
        lambda: restarted.get("missing"),
    ):
        with pytest.raises(JobNotFound):
            operation()
    assert restarted.list_jobs("bob") == []
    assert [row["id"] for row in restarted.list_jobs("alice")] == [job["id"]]


def test_concurrent_claims_have_exactly_one_winner(store):
    job = queued(store)
    with ThreadPoolExecutor(max_workers=12) as executor:
        leases = list(executor.map(lambda n: store.claim(f"worker-{n}"), range(12)))
    winners = [lease for lease in leases if lease]
    assert len(winners) == 1
    assert winners[0]["id"] == job["id"]
    assert len(winners[0]["lease_token"]) >= 40
    assert winners[0]["attempts"] == 1


def test_concurrent_create_limits_are_atomic(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite", max_active_per_owner=3)

    def create(_):
        try:
            return store.create("alice", "video.mp4", {})
        except JobLimitReached:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        jobs = list(executor.map(create, range(12)))
    assert sum(job is not None for job in jobs) == 3
    assert store.active_count("alice") == store.active_count() == 3
    assert len({job["id"] for job in jobs if job}) == 3
    store.cancel(next(job for job in jobs if job)["id"], "alice")
    assert store.create("alice", "new.mp4", {})["status"] == "uploading"


def test_global_create_limit_applies_across_owners(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite", max_active_total=1)
    store.create("alice", "a.mp4", {})
    with pytest.raises(JobLimitReached):
        store.create("bob", "b.mp4", {})


def test_heartbeat_extends_lease_and_bounds_progress(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        lease = claimed(store)
    with patch("linguoai.web_jobs.time.time", return_value=109):
        heartbeat = store.heartbeat(
            lease["id"], "worker", lease["lease_token"], progress=150, stage="transcribing"
        )
    assert heartbeat["lease_until"] == 119
    assert heartbeat["progress"] == 100
    assert heartbeat["stage"] == "transcribing"
    with patch("linguoai.web_jobs.time.time", return_value=115):
        assert store.claim("other") is None
        assert store.heartbeat(lease["id"], "worker", lease["lease_token"], -2)["progress"] == 0


@pytest.mark.parametrize(
    "operation", ["heartbeat", "verify_lease", "add_artifact", "complete", "fail"]
)
def test_all_worker_operations_reject_expired_or_replaced_claim(store, operation):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        old = claimed(store)
        store.add_artifact(old["id"], "worker", old["lease_token"], "video", "old-result", 10)
    with patch("linguoai.web_jobs.time.time", return_value=111):
        replacement = store.claim("worker")
        assert replacement["lease_token"] != old["lease_token"]
        assert replacement["artifacts"] == {}
        assert replacement["stale_refs"] == ["old-result"]
        assert replacement["attempts"] == 2
        arguments = [old["id"], "worker", old["lease_token"]]
        if operation == "add_artifact":
            arguments += ["video", "stale-upload", 10]
        if operation == "fail":
            arguments += ["old error"]
        with pytest.raises(LeaseLost):
            getattr(store, operation)(*arguments)
        with pytest.raises(LeaseLost):
            store.verify_lease(replacement["id"], "another", replacement["lease_token"])


def test_unreclaimed_expired_lease_is_also_fenced(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        lease = claimed(store)
    with patch("linguoai.web_jobs.time.time", return_value=110), pytest.raises(LeaseLost):
        store.heartbeat(lease["id"], "worker", lease["lease_token"])


def test_retry_limit_fails_crashed_job_and_moves_to_next(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        lease = claimed(store)
    for timestamp in (111, 122):
        with patch("linguoai.web_jobs.time.time", return_value=timestamp):
            assert store.claim("worker")["id"] == lease["id"]
    with patch("linguoai.web_jobs.time.time", return_value=133):
        assert store.claim("worker") is None
    failed = store.get(lease["id"])
    assert failed["status"] == "failed"
    assert failed["attempts"] == 3
    assert failed["expires_at"] == 163
    assert "retry limit" in failed["error"]


@pytest.mark.parametrize("initial_state", ["uploading", "queued"])
def test_cancel_unstarted_job_is_immediate(store, initial_state):
    job = store.create("alice", "video.mp4", {})
    if initial_state == "queued":
        store.queue(job["id"], "alice", "input", 42)
    result = store.cancel(job["id"], "alice")
    assert result["status"] == "cancelled"
    assert result["cancel_requested"]
    assert store.claim("worker") is None
    with pytest.raises(JobConflict):
        store.queue(job["id"], "alice", "input", 42)


def test_cancellation_cannot_be_completed_or_uploaded_afterwards(store):
    lease = claimed(store)
    add_results(store, lease)
    cancelled = store.cancel(lease["id"], "alice")
    assert cancelled["status"] == "running"
    assert store.heartbeat(lease["id"], "worker", lease["lease_token"])["cancel_requested"]
    with pytest.raises(JobConflict):
        store.add_artifact(lease["id"], "worker", lease["lease_token"], "video", "late", 1)
    assert store.complete(lease["id"], "worker", lease["lease_token"])["status"] == "cancelled"
    with pytest.raises(LeaseLost):
        store.complete(lease["id"], "worker", lease["lease_token"])


def test_cancelled_crashed_worker_never_restarts(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        lease = claimed(store)
        store.cancel(lease["id"], "alice")
    with patch("linguoai.web_jobs.time.time", return_value=111):
        assert store.claim("replacement") is None
    assert store.get(lease["id"])["status"] == "cancelled"


def test_complete_requires_all_results_and_preserves_refs(store):
    lease = claimed(store)
    with pytest.raises(JobConflict):
        store.complete(lease["id"], "worker", lease["lease_token"])
    add_results(store, lease)
    result = store.complete(lease["id"], "worker", lease["lease_token"])
    assert result["status"] == "completed"
    assert result["progress"] == 100
    assert result["artifacts"]["original_srt"] == {
        "storage_ref": "result-original_srt",
        "size_bytes": 12,
    }
    assert result["expires_at"] == result["finished_at"] + 30
    assert result["lease_token"] is None
    assert store.active_count() == 0


def test_cleanup_is_leased_retryable_and_fenced(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        lease = claimed(store)
        add_results(store, lease)
        store.fail(lease["id"], "worker", lease["lease_token"], "failed")
    with patch("linguoai.web_jobs.time.time", return_value=129):
        assert store.expired_cleanup_records() == []
        with pytest.raises(JobConflict):
            store.expire(lease["id"])
    with patch("linguoai.web_jobs.time.time", return_value=130):
        records = store.expired_cleanup_records()
        assert len(records) == 1
        assert records[0]["input_ref"] == "input-ref"
        assert len(records[0]["artifacts"]) == 3
        assert store.expired_cleanup_records() == []
    with patch("linguoai.web_jobs.time.time", return_value=431):
        replacement = store.expired_cleanup_records()[0]
        assert replacement["cleanup_token"] != records[0]["cleanup_token"]
        with pytest.raises(JobConflict):
            store.expire(lease["id"], records[0]["cleanup_token"])
        expired = store.expire(lease["id"], replacement["cleanup_token"])
        assert expired["status"] == "expired"
        assert expired["input_ref"] is None
        assert expired["artifacts"] == {}
        assert expired["stale_refs"] == []
        assert store.expire(lease["id"])["status"] == "expired"
        assert store.expired_cleanup_records() == []


def test_abandoned_upload_cleanup_blocks_queue(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        job = store.create("alice", "video.mp4", {})
    with patch("linguoai.web_jobs.time.time", return_value=131):
        cleanup = store.expired_cleanup_records()[0]
        with pytest.raises(JobConflict):
            store.queue(job["id"], "alice", "upload", 2)
        assert store.expire(job["id"], cleanup["cleanup_token"])["status"] == "expired"


def test_failed_upload_releases_limit_and_cancel_is_idempotent(store):
    job = store.create("alice", "video.mp4", {})
    assert store.fail_upload(job["id"], "alice", "bad media")["status"] == "failed"
    assert store.active_count() == 0
    assert store.cancel(job["id"], "alice")["status"] == "failed"


def test_parakeet_requires_exact_profile_capability(store):
    german = queued(store, asr_engine="parakeet", parakeet_profile="german-primeline")
    multilingual = queued(store, asr_engine="parakeet", parakeet_profile="multilingual-v3")
    whisper = queued(store)
    assert store.claim("default")["id"] == whisper["id"]
    assert store.claim("engine-only", {"asr_engines": ["parakeet"]}) is None
    lease = store.claim(
        "multi", {"asr_engines": ["parakeet"], "parakeet_profiles": ["multilingual-v3"]}
    )
    assert lease["id"] == multilingual["id"]
    lease = store.claim(
        "german", {"asr_engines": ["parakeet"], "parakeet_profiles": ["german-primeline"]}
    )
    assert lease["id"] == german["id"]


def test_replaced_artifact_reference_is_retained_for_cleanup(store):
    lease = claimed(store)
    store.add_artifact(lease["id"], "worker", lease["lease_token"], "video", "old", 1)
    result = store.add_artifact(lease["id"], "worker", lease["lease_token"], "video", "new", 2)
    assert result["stale_refs"] == ["old"]
    assert result["artifacts"]["video"]["storage_ref"] == "new"


@pytest.mark.parametrize("bad_progress", [float("nan"), float("inf"), "50", True])
def test_invalid_progress_is_rejected(store, bad_progress):
    lease = claimed(store)
    with pytest.raises(ValueError):
        store.heartbeat(lease["id"], "worker", lease["lease_token"], bad_progress)


def test_input_validation(store):
    for options in ({"x": float("nan")}, {"x": "x" * 17000}, []):
        with pytest.raises(ValueError):
            store.create("alice", "video.mp4", options)
    job = store.create("alice", "video.mp4", {})
    for size in (-1, True, 1.5):
        with pytest.raises(ValueError):
            store.queue(job["id"], "alice", "input", size)
    with pytest.raises(ValueError):
        store.list_jobs("alice", limit=1000)


def test_expired_lease_cannot_be_revived_by_waiting_for_transaction_lock(store):
    from contextlib import contextmanager

    with patch("linguoai.web_jobs.time.time", return_value=100):
        lease = claimed(store)
    original_transaction = store._transaction
    clock = [109]

    @contextmanager
    def transaction_after_lock_wait():
        with original_transaction() as connection:
            clock[0] = 111
            yield connection

    with (
        patch.object(store, "_transaction", transaction_after_lock_wait),
        patch("linguoai.web_jobs.time.time", side_effect=lambda: clock[0]),
        pytest.raises(LeaseLost),
    ):
        store.heartbeat(lease["id"], "worker", lease["lease_token"])


def test_non_ascii_lease_token_is_rejected_as_a_lost_lease(store):
    lease = claimed(store)
    with pytest.raises(LeaseLost):
        store.verify_lease(lease["id"], "worker", "a\N{LATIN SMALL LETTER U WITH DIAERESIS}")


def test_concurrent_cleanup_claims_have_exactly_one_winner(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        lease = claimed(store)
        store.fail(lease["id"], "worker", lease["lease_token"], "failed")
    with (
        patch("linguoai.web_jobs.time.time", return_value=131),
        ThreadPoolExecutor(max_workers=8) as executor,
    ):
        results = list(executor.map(lambda _: store.expired_cleanup_records(), range(8)))
    assert sum(len(records) for records in results) == 1


def test_translation_and_tts_capabilities_prevent_unsupported_claims(store):
    unsupported_translation = queued(store, translation_service="nllb")
    unsupported_voice = queued(store, tts_engine="piper")
    default = queued(store)
    assert store.claim("default")["id"] == default["id"]
    assert store.claim("default") is None
    assert (
        store.claim("nllb", {"translation_services": ["nllb"]})["id"]
        == unsupported_translation["id"]
    )
    assert store.claim("piper", {"tts_engines": ["piper"]})["id"] == unsupported_voice["id"]


@pytest.mark.parametrize("capability", ["translation_services", "tts_engines"])
def test_explicit_empty_provider_capabilities_reject_default_jobs(store, capability):
    queued(store)
    assert store.claim("unavailable", {capability: []}) is None


def test_upload_attachment_atomically_removes_orphan_tracking(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        job = store.create("alice", "video.mp4", {})
        store.register_orphan("input-ref", delay_seconds=5)
        store.queue(job["id"], "alice", "input-ref", 3)
        lease = store.claim("worker")
        store.register_orphan("output-ref", delay_seconds=5)
        store.add_artifact(lease["id"], "worker", lease["lease_token"], "video", "output-ref", 3)
    with patch("linguoai.web_jobs.time.time", return_value=106):
        assert store.orphan_cleanup_records() == []


def test_failed_attachment_stays_durably_tracked_and_cleanup_is_fenced(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        store.register_orphan("unattached", delay_seconds=5)
        with pytest.raises(JobNotFound):
            store.queue("missing", "alice", "unattached", 3)
    with patch("linguoai.web_jobs.time.time", return_value=104):
        assert store.orphan_cleanup_records() == []
    with patch("linguoai.web_jobs.time.time", return_value=105):
        record = store.orphan_cleanup_records()[0]
        assert record["storage_ref"] == "unattached"
        assert store.orphan_cleanup_records() == []
        with pytest.raises(JobConflict):
            store.delete_orphan("unattached", "wrong-token")
    with patch("linguoai.web_jobs.time.time", return_value=406):
        replacement = store.orphan_cleanup_records()[0]
        with pytest.raises(JobConflict):
            store.delete_orphan("unattached", record["cleanup_token"])
        store.delete_orphan("unattached", replacement["cleanup_token"])
        assert store.orphan_cleanup_records() == []
        store.delete_orphan("unattached", replacement["cleanup_token"])


def test_orphan_claim_prevents_unsafe_late_attachment(store):
    with patch("linguoai.web_jobs.time.time", return_value=100):
        job = store.create("alice", "video.mp4", {})
        store.register_orphan("late-upload", delay_seconds=0)
        store.orphan_cleanup_records()
        with pytest.raises(JobConflict):
            store.queue(job["id"], "alice", "late-upload", 3)
    assert store.get(job["id"])["status"] == "uploading"


def test_already_attached_references_cannot_be_registered_as_orphans(store):
    lease = claimed(store)
    store.add_artifact(lease["id"], "worker", lease["lease_token"], "video", "output", 3)
    store.register_orphan("input-ref", delay_seconds=0)
    store.register_orphan("output", delay_seconds=0)
    assert store.orphan_cleanup_records() == []


def test_successful_compensation_forgets_orphan_without_claim(store):
    store.register_orphan("unattached", delay_seconds=0)
    store.delete_orphan("unattached")
    assert store.orphan_cleanup_records() == []
