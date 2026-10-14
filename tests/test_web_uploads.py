import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from linguoai.web_uploads import (
    MAX_CHUNK_BYTES,
    ChunkUploadStore,
    UploadConflict,
    UploadLimitReached,
    UploadNotFound,
)

PRINCIPAL = "user:alice"
DATA = b"abcdefghij"


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path):
    return ChunkUploadStore(
        tmp_path,
        chunk_bytes=4,
        ttl_seconds=40,
        max_age_seconds=100,
        completed_retention_seconds=30,
        finalize_lease_seconds=10,
    )


def create(store, *, principal=PRINCIPAL, data=DATA, **kwargs):
    return store.create(principal, "job-one", "input", "video.mp4", len(data), **kwargs)


def append_all(store, row, data=DATA, principal=PRINCIPAL, fence_tag=""):
    for offset in range(0, len(data), row["chunk_bytes"]):
        chunk = data[offset : offset + row["chunk_bytes"]]
        store.append(row["id"], principal, offset, chunk, digest(chunk), fence_tag=fence_tag)


def full_upload(store, **kwargs):
    row = create(store, **kwargs)
    append_all(
        store,
        row,
        principal=kwargs.get("principal", PRINCIPAL),
        fence_tag=kwargs.get("fence_tag", ""),
    )
    return row


def test_upload_survives_restart_and_chunk_size_is_persisted(store):
    row = create(store)
    store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    restarted = ChunkUploadStore(store.state_directory)
    assert restarted.get(row["id"], PRINCIPAL)["chunk_bytes"] == 4
    assert restarted.get(row["id"], PRINCIPAL)["offset"] == 4
    restarted.append(row["id"], PRINCIPAL, 4, DATA[4:8], digest(DATA[4:8]))
    assert restarted.private_file_path(row["id"], PRINCIPAL).read_bytes() == DATA[:8]


def test_owner_and_worker_fence_are_isolated_and_not_exposed(store):
    raw_lease = "private-worker-lease-do-not-store"
    fence = digest(raw_lease.encode())
    row = create(store, principal="worker:cpu-one", fence_tag=fence)
    for principal, tag in (
        (PRINCIPAL, fence),
        ("worker:cpu-one", ""),
        ("worker:cpu-one", digest(b"another-lease")),
    ):
        with pytest.raises(UploadNotFound):
            store.get(row["id"], principal, fence_tag=tag)
    public = store.get(row["id"], "worker:cpu-one", fence_tag=fence)
    assert (
        not {"principal", "fence_tag", "storage_ref", "file_path", "finalization_token"}
        & public.keys()
    )
    with store._connection() as connection:
        assert connection.execute("SELECT fence_tag FROM upload_sessions").fetchone()[0] == fence
    assert raw_lease.encode() not in store.db_path.read_bytes()


def test_idempotent_request_id_and_metadata_conflicts(store):
    row = create(store, request_id="f3df446e-7e58-4626-a331-f60f403c7686")
    again = create(store, request_id=row["request_id"])
    assert again["id"] == row["id"]
    assert store.find_by_request_id(PRINCIPAL, row["request_id"])["id"] == row["id"]
    assert store.find_by_request_id("user:bob", row["request_id"]) is None
    with pytest.raises(UploadConflict):
        create(store, request_id=row["request_id"], data=DATA + b"different")


def test_concurrent_same_request_creates_one_session(store):
    with ThreadPoolExecutor(max_workers=12) as executor:
        rows = list(executor.map(lambda _: create(store, request_id="same-request"), range(12)))
    assert len({row["id"] for row in rows}) == 1
    assert len(list(store.root.glob("*.part"))) == 1


def test_concurrent_create_limits_are_atomic(store):
    def attempt(_):
        try:
            return create(store)
        except UploadLimitReached:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        rows = list(executor.map(attempt, range(12)))
    assert sum(row is not None for row in rows) == 3
    store.abort(next(row for row in rows if row)["id"], PRINCIPAL)
    assert create(store)["status"] == "open"


def test_total_pending_limit_applies_across_principals(tmp_path):
    store = ChunkUploadStore(tmp_path, max_pending_total=1)
    create(store)
    with pytest.raises(UploadLimitReached):
        create(store, principal="user:bob")


def test_chunk_retry_is_atomic_idempotent_and_verifies_existing_bytes(store):
    row = create(store)
    chunk = DATA[:4]
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(
            executor.map(
                lambda _: store.append(row["id"], PRINCIPAL, 0, chunk, digest(chunk)), range(8)
            )
        )
    assert {result["offset"] for result in responses} == {4}
    assert store.private_file_path(row["id"], PRINCIPAL).read_bytes() == chunk
    with pytest.raises(UploadConflict):
        store.append(row["id"], PRINCIPAL, 0, b"xxxx", digest(b"xxxx"))
    with pytest.raises(UploadConflict):
        store.append(row["id"], PRINCIPAL, 1, b"bcde", digest(b"bcde"))
    path = store.private_file_path(row["id"], PRINCIPAL)
    path.write_bytes(b"xxxx")
    with pytest.raises(UploadConflict):
        store.append(row["id"], PRINCIPAL, 0, chunk, digest(chunk))


def test_chunks_require_exact_length_offset_and_sha256(store):
    row = create(store)
    for offset, chunk in ((4, DATA[4:8]), (0, b"ab"), (0, DATA[:5])):
        with pytest.raises(UploadConflict):
            store.append(row["id"], PRINCIPAL, offset, chunk, digest(chunk))
    with pytest.raises(UploadConflict):
        store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(b"wrong"))
    assert store.get(row["id"], PRINCIPAL)["offset"] == 0
    append_all(store, row)
    assert store.get(row["id"], PRINCIPAL)["offset"] == len(DATA)


@pytest.mark.parametrize("offset", [-1, True, 1.2])
def test_invalid_offset_is_rejected(store, offset):
    row = create(store)
    with pytest.raises(ValueError):
        store.append(row["id"], PRINCIPAL, offset, DATA[:4], digest(DATA[:4]))


def test_chunk_maximum_is_8_mib(store):
    row = create(store)
    with pytest.raises(ValueError):
        store.append(row["id"], PRINCIPAL, 0, b"x" * (MAX_CHUNK_BYTES + 1), digest(b"unused"))
    with pytest.raises(ValueError):
        store.append(row["id"], PRINCIPAL, 0, b"", digest(b""))
    with pytest.raises(ValueError):
        store.append(row["id"], PRINCIPAL, 0, b"abcd", "invalid-hash")


def test_uncommitted_tail_is_truncated_before_retry(store):
    row = create(store)
    store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    path = store.private_file_path(row["id"], PRINCIPAL)
    with path.open("ab") as file:
        file.write(b"uncommitted-tail")
    restarted = ChunkUploadStore(store.state_directory)
    restarted.append(row["id"], PRINCIPAL, 4, DATA[4:8], digest(DATA[4:8]))
    assert path.read_bytes() == DATA[:8]


def test_sql_failure_after_fsync_is_recoverable(store):
    row = create(store)
    original = store._transaction

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, values=()):
            if query.startswith("INSERT INTO upload_chunks"):
                raise sqlite3.OperationalError("simulated metadata failure")
            return self.connection.execute(query, values)

    @contextmanager
    def failing_transaction():
        with original() as connection:
            yield FailingConnection(connection)

    with (
        patch.object(store, "_transaction", failing_transaction),
        pytest.raises(sqlite3.OperationalError),
    ):
        store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    assert store.get(row["id"], PRINCIPAL)["offset"] == 0
    store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    assert store.private_file_path(row["id"], PRINCIPAL).read_bytes() == DATA[:4]


def test_truncated_committed_payload_cannot_be_completed(store):
    row = full_upload(store)
    store.private_file_path(row["id"], PRINCIPAL).write_bytes(b"short")
    with pytest.raises(UploadConflict):
        store.claim_completion(row["id"], PRINCIPAL)


def test_completion_requires_full_upload_and_optional_whole_file_hash(store):
    row = create(store, expected_sha256=digest(DATA))
    with pytest.raises(UploadConflict):
        store.claim_completion(row["id"], PRINCIPAL)
    append_all(store, row)
    claim = store.claim_completion(row["id"], PRINCIPAL)
    assert claim["status"] == "finalizing"
    assert claim["finalize_attempts"] == 1
    assert claim["principal"] == PRINCIPAL
    with pytest.raises(UploadConflict):
        store.claim_completion(row["id"], PRINCIPAL)
    bad = create(store, expected_sha256=digest(b"bad"))
    append_all(store, bad)
    with pytest.raises(UploadConflict):
        store.claim_completion(bad["id"], PRINCIPAL)


def test_recovery_requires_client_confirmation_and_preserves_storage_ref(store):
    with patch("linguoai.web_uploads.time.time", return_value=100):
        row = full_upload(store)
        assert store.recoverable_completions() == []
        first = store.claim_completion(row["id"], PRINCIPAL)
        store.set_storage_ref(row["id"], PRINCIPAL, first["finalization_token"], "durable-ref")
        assert store.recoverable_completions() == []
    with patch("linguoai.web_uploads.time.time", return_value=111):
        recovered = store.recoverable_completions()
        assert recovered[0]["storage_ref"] == "durable-ref"
        assert recovered[0]["principal"] == PRINCIPAL
        second = store.claim_completion(row["id"], PRINCIPAL)
        assert second["finalization_token"] != first["finalization_token"]
        assert second["finalize_attempts"] == 2
        for operation in (store.finish, store.renew_completion):
            with pytest.raises(UploadConflict):
                operation(row["id"], PRINCIPAL, first["finalization_token"])
        with pytest.raises(UploadConflict):
            store.set_storage_ref(row["id"], PRINCIPAL, first["finalization_token"], "wrong-ref")


def test_completion_renewal_prevents_recovery_until_lease_expires(store):
    with patch("linguoai.web_uploads.time.time", return_value=100):
        row = full_upload(store)
        first = store.claim_completion(row["id"], PRINCIPAL)
    with patch("linguoai.web_uploads.time.time", return_value=109):
        assert (
            store.renew_completion(row["id"], PRINCIPAL, first["finalization_token"])[
                "finalization_until"
            ]
            == 119
        )
    with patch("linguoai.web_uploads.time.time", return_value=111):
        assert store.recoverable_completions() == []
    with patch("linguoai.web_uploads.time.time", return_value=119):
        with pytest.raises(UploadConflict):
            store.renew_completion(row["id"], PRINCIPAL, first["finalization_token"])
        assert len(store.recoverable_completions()) == 1


def test_release_retries_preserve_attempts_and_permanent_failure_is_aborted(store):
    row = full_upload(store)
    first = store.claim_completion(row["id"], PRINCIPAL)
    store.set_storage_ref(row["id"], PRINCIPAL, first["finalization_token"], "durable-ref")
    store.release_completion(row["id"], PRINCIPAL, first["finalization_token"], "Temporary failure")
    assert store.get(row["id"], PRINCIPAL)["status"] == "ready"
    store.private_file_path(row["id"], PRINCIPAL).unlink()
    second = store.claim_completion(row["id"], PRINCIPAL)
    assert second["storage_ref"] == "durable-ref"
    assert second["finalize_attempts"] == 2
    store.release_completion(
        row["id"], PRINCIPAL, second["finalization_token"], "Failed", retry=False
    )
    record = store.expired_cleanup_records()[0]
    assert record["status"] == "aborted"
    assert record["storage_ref"] == "durable-ref"
    assert record["error"] == "Failed"


def test_completion_is_idempotent_releases_payload_and_retains_metadata(store):
    with patch("linguoai.web_uploads.time.time", return_value=100):
        row = full_upload(store, request_id="completed-request")
        path = store.private_file_path(row["id"], PRINCIPAL)
        claim = store.claim_completion(row["id"], PRINCIPAL)
        token = claim["finalization_token"]
        with pytest.raises(UploadConflict):
            store.finish(row["id"], PRINCIPAL, token)
        store.set_storage_ref(row["id"], PRINCIPAL, token, "attached-ref")
        with pytest.raises(UploadConflict):
            store.set_storage_ref(row["id"], PRINCIPAL, token, "different-ref")
        finished = store.finish(row["id"], PRINCIPAL, token)
        assert finished["status"] == "completed"
        assert finished["expires_at"] == 130
        assert not path.exists()
        assert store.finish(row["id"], PRINCIPAL, token)["status"] == "completed"
        completed = store.claim_completion(row["id"], PRINCIPAL)
        assert completed["finalize_attempts"] == 1
        assert completed["storage_ref"] == "attached-ref"
        assert store.abort(row["id"], PRINCIPAL)["status"] == "completed"
        assert create(store, request_id="completed-request")["id"] == row["id"]
        assert not {"storage_ref", "principal", "fence_tag", "finalization_token"} & finished.keys()
    with patch("linguoai.web_uploads.time.time", return_value=130):
        record = store.expired_cleanup_records()[0]
        assert record["storage_ref"] is None
        store.cleanup_ack(row["id"], record["cleanup_token"])
        assert store.find_by_request_id(PRINCIPAL, "completed-request") is None


def test_cleanup_of_completed_metadata_preserves_attached_ref_after_unlink_failure(store):
    with patch("linguoai.web_uploads.time.time", return_value=100):
        row = full_upload(store)
        path = store.private_file_path(row["id"], PRINCIPAL)
        claim = store.claim_completion(row["id"], PRINCIPAL)
        store.set_storage_ref(row["id"], PRINCIPAL, claim["finalization_token"], "attached")
        with patch.object(Path, "unlink", side_effect=PermissionError("busy file")):
            assert (
                store.finish(row["id"], PRINCIPAL, claim["finalization_token"])["status"]
                == "completed"
            )
        assert path.exists()
    with patch("linguoai.web_uploads.time.time", return_value=131):
        cleanup = store.expired_cleanup_records()[0]
        assert cleanup["storage_ref"] is None
        store.cleanup_ack(row["id"], cleanup["cleanup_token"])
        assert not path.exists()


def test_abort_fences_inflight_completion_and_exposes_private_cleanup_metadata(store):
    fence = digest(b"worker-lease")
    row = full_upload(store, principal="worker:one", fence_tag=fence)
    claim = store.claim_completion(row["id"], "worker:one", fence_tag=fence)
    store.set_storage_ref(
        row["id"], "worker:one", claim["finalization_token"], "unattached", fence_tag=fence
    )
    aborted = store.abort(row["id"], "worker:one", fence_tag=fence, error="Job cancelled")
    assert aborted["error"] == "Job cancelled"
    with pytest.raises(UploadConflict):
        store.finish(row["id"], "worker:one", claim["finalization_token"], fence_tag=fence)
    cleanup = store.expired_cleanup_records()[0]
    assert cleanup["principal"] == "worker:one"
    assert cleanup["fence_tag"] == fence
    assert cleanup["job_id"] == "job-one"
    assert cleanup["kind"] == "input"
    assert cleanup["storage_ref"] == "unattached"
    assert cleanup["file_path"].parent == store.root


def test_cleanup_claim_is_retryable_and_fenced(store):
    with patch("linguoai.web_uploads.time.time", return_value=100):
        row = create(store)
    with patch("linguoai.web_uploads.time.time", return_value=141):
        first = store.expired_cleanup_records()[0]
        assert store.expired_cleanup_records() == []
        with pytest.raises(UploadConflict):
            store.cleanup_ack(row["id"], "wrong-token")
    with patch("linguoai.web_uploads.time.time", return_value=442):
        second = store.expired_cleanup_records()[0]
        with pytest.raises(UploadConflict):
            store.cleanup_ack(row["id"], first["cleanup_token"])
        store.cleanup_ack(row["id"], second["cleanup_token"])
        store.cleanup_ack(row["id"], second["cleanup_token"])
        assert store.expired_cleanup_records() == []
    with pytest.raises(UploadNotFound):
        store.get(row["id"], PRINCIPAL)


def test_inactivity_expiry_and_maximum_age_cannot_be_extended(store):
    with patch("linguoai.web_uploads.time.time", return_value=100):
        row = create(store)
    with patch("linguoai.web_uploads.time.time", return_value=130):
        store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    with patch("linguoai.web_uploads.time.time", return_value=160):
        store.append(row["id"], PRINCIPAL, 4, DATA[4:8], digest(DATA[4:8]))
    with patch("linguoai.web_uploads.time.time", return_value=190):
        assert (
            store.append(row["id"], PRINCIPAL, 8, DATA[8:], digest(DATA[8:]))["expires_at"] == 200
        )
    with patch("linguoai.web_uploads.time.time", return_value=199):
        assert (
            store.append(row["id"], PRINCIPAL, 8, DATA[8:], digest(DATA[8:]))["expires_at"] == 200
        )
    with (
        patch("linguoai.web_uploads.time.time", return_value=200),
        pytest.raises(UploadConflict),
    ):
        store.claim_completion(row["id"], PRINCIPAL)


def test_reserved_bytes_and_pending_records_track_only_pending_uploads(store):
    one = create(store)
    two = create(store)
    assert store.reserved_remaining_bytes() == 20
    store.append(one["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    assert store.reserved_remaining_bytes() == 16
    pending = store.pending_records("job-one")
    assert {row["id"] for row in pending} == {one["id"], two["id"]}
    assert all(row["principal"] == PRINCIPAL and "finalization_token" not in row for row in pending)
    assert store.pending_records("another-job") == []
    store.abort(two["id"], PRINCIPAL)
    assert store.reserved_remaining_bytes() == 6


def test_symlink_payload_is_rejected_without_touching_target(store, tmp_path):
    row = create(store)
    path = store.private_file_path(row["id"], PRINCIPAL)
    outside = tmp_path / "outside-file"
    outside.write_bytes(b"safe")
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable on this host")
    with pytest.raises(UploadConflict):
        store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    assert outside.read_bytes() == b"safe"
    store.abort(row["id"], PRINCIPAL)
    cleanup = store.expired_cleanup_records()[0]
    store.cleanup_ack(row["id"], cleanup["cleanup_token"])
    assert outside.read_bytes() == b"safe"
    assert not path.is_symlink()


@pytest.mark.parametrize(
    "filename", ["../video.mp4", "..\\video.mp4", "a/b.mp4", "a:stream", "bad\x00.mp4"]
)
def test_unsafe_filename_is_rejected(store, filename):
    with pytest.raises(ValueError):
        store.create(PRINCIPAL, "job", "input", filename, 10)


@pytest.mark.parametrize("size", [0, -1, True, 1.2, 1024**3 + 1])
def test_invalid_expected_size_is_rejected(store, size):
    with pytest.raises(ValueError):
        store.create(PRINCIPAL, "job", "input", "video.mp4", size)


def test_artifact_names_are_bounded_and_lease_hash_only_is_accepted(store):
    with pytest.raises(ValueError):
        store.create("worker:one", "job", "artifact", "v.mp4", 1, artifact_name="unexpected")
    with pytest.raises(ValueError):
        create(store, fence_tag="raw-worker-lease")
    with pytest.raises(ValueError):
        create(store, request_id="../request")
    row = store.create(
        "worker:one",
        "job",
        "artifact",
        "v.mp4",
        1,
        artifact_name="video",
        fence_tag=digest(b"lease"),
    )
    assert row["artifact_name"] == "video"


def test_missing_zero_byte_payload_is_recreated_without_losing_committed_data(store):
    row = create(store)
    store.private_file_path(row["id"], PRINCIPAL).unlink()
    store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    assert store.private_file_path(row["id"], PRINCIPAL).read_bytes() == DATA[:4]


def test_hard_link_payload_is_rejected_without_touching_target(store, tmp_path):
    import os

    row = create(store)
    path = store.private_file_path(row["id"], PRINCIPAL)
    outside = tmp_path / "outside-hard-link"
    outside.write_bytes(b"safe")
    path.unlink()
    try:
        os.link(outside, path)
    except OSError:
        pytest.skip("Hard links unavailable on this host")
    with pytest.raises(UploadConflict):
        store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    assert outside.read_bytes() == b"safe"


def test_finalization_cannot_be_revived_after_waiting_for_transaction_lock(store):
    with patch("linguoai.web_uploads.time.time", return_value=100):
        row = full_upload(store)
        first = store.claim_completion(row["id"], PRINCIPAL)
    original = store._transaction
    clock = [109]

    @contextmanager
    def after_lock_wait():
        with original() as connection:
            clock[0] = 111
            yield connection

    with (
        patch.object(store, "_transaction", after_lock_wait),
        patch("linguoai.web_uploads.time.time", side_effect=lambda: clock[0]),
        pytest.raises(UploadConflict),
    ):
        store.renew_completion(row["id"], PRINCIPAL, first["finalization_token"])


def test_aborted_session_exposes_safe_error_until_cleanup_ack(store):
    row = create(store)
    store.abort(row["id"], PRINCIPAL, error="Job cancelled")
    public = store.get(row["id"], PRINCIPAL)
    assert public["status"] == "aborted"
    assert public["error"] == "Job cancelled"
    assert "storage_ref" not in public
    cleanup = store.expired_cleanup_records()[0]
    store.cleanup_ack(row["id"], cleanup["cleanup_token"])
    with pytest.raises(UploadNotFound):
        store.get(row["id"], PRINCIPAL)


def test_real_8_mib_chunk_boundary_and_short_final_chunk(tmp_path):
    store = ChunkUploadStore(tmp_path)
    data = b"x" * MAX_CHUNK_BYTES
    row = create(store, data=data + b"tail")
    assert row["chunk_bytes"] == MAX_CHUNK_BYTES
    store.append(row["id"], PRINCIPAL, 0, data, digest(data))
    store.append(row["id"], PRINCIPAL, MAX_CHUNK_BYTES, b"tail", digest(b"tail"))
    assert store.private_file_path(row["id"], PRINCIPAL).stat().st_size == MAX_CHUNK_BYTES + 4
    assert store.claim_completion(row["id"], PRINCIPAL)["offset"] == MAX_CHUNK_BYTES + 4


def test_concurrent_completion_claims_have_one_winner(store):
    row = full_upload(store)

    def claim(_):
        try:
            return store.claim_completion(row["id"], PRINCIPAL)
        except UploadConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(claim, range(8)))
    assert sum(result is not None for result in results) == 1
    assert next(result for result in results if result)["finalize_attempts"] == 1


def test_concurrent_cleanup_claims_have_one_winner(store):
    row = create(store)
    store.abort(row["id"], PRINCIPAL)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: store.expired_cleanup_records(), range(8)))
    assert sum(len(records) for records in results) == 1


def test_cleanup_directory_sync_failure_retains_metadata_for_retry(store):
    row = create(store)
    path = store.private_file_path(row["id"], PRINCIPAL)
    store.abort(row["id"], PRINCIPAL)
    record = store.expired_cleanup_records()[0]
    with (
        patch.object(store, "_sync_directory", side_effect=OSError("directory busy")),
        pytest.raises(OSError),
    ):
        store.cleanup_ack(row["id"], record["cleanup_token"])
    assert not path.exists()
    assert store.get(row["id"], PRINCIPAL)["status"] == "aborted"
    with patch.object(store, "_sync_directory", wraps=store._sync_directory) as sync:
        store.cleanup_ack(row["id"], record["cleanup_token"])
    sync.assert_called_once_with()
    with pytest.raises(UploadNotFound):
        store.get(row["id"], PRINCIPAL)


def test_completed_payload_release_syncs_directory_and_stays_idempotent(store):
    row = full_upload(store)
    claim = store.claim_completion(row["id"], PRINCIPAL)
    store.set_storage_ref(row["id"], PRINCIPAL, claim["finalization_token"], "attached")
    with patch.object(store, "_sync_directory", wraps=store._sync_directory) as sync:
        store.finish(row["id"], PRINCIPAL, claim["finalization_token"])
    sync.assert_called_once_with()
    with patch.object(store, "_sync_directory", side_effect=OSError("directory busy")):
        assert store.finish(row["id"], PRINCIPAL, "retry-token")["status"] == "completed"
    assert store.get(row["id"], PRINCIPAL)["status"] == "completed"


def test_interrupted_partial_payload_write_rolls_back_to_committed_offset(store):
    row = create(store)
    store.append(row["id"], PRINCIPAL, 0, DATA[:4], digest(DATA[:4]))
    payload = store._payload

    class InterruptedFile:
        def __init__(self, file):
            self.file = file
            self.writes = 0

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, data):
            self.writes += 1
            if self.writes == 1:
                return self.file.write(data[:2])
            raise OSError("simulated interrupted write")

    @contextmanager
    def interrupted_payload(row, **kwargs):
        with payload(row, **kwargs) as file:
            yield InterruptedFile(file)

    with (
        patch.object(store, "_payload", interrupted_payload),
        pytest.raises(OSError),
    ):
        store.append(row["id"], PRINCIPAL, 4, DATA[4:8], digest(DATA[4:8]))
    assert store.get(row["id"], PRINCIPAL)["offset"] == 4
    assert store.private_file_path(row["id"], PRINCIPAL).read_bytes() == DATA[:4]
    store.append(row["id"], PRINCIPAL, 4, DATA[4:8], digest(DATA[4:8]))
    assert store.private_file_path(row["id"], PRINCIPAL).read_bytes() == DATA[:8]
