import pytest
from mantau_agent.clips import ClipRecorder
from mantau_agent.legacy_clips import LegacyClipMigrator

MP4 = b"\0\0\0\x18ftypmp42" + b"x" * 64


class Uploader:
    def __init__(self, approved):
        self.approved = approved
        self.checked = []
        self.unavailable = False
    async def legacy_claims(self, ids):
        self.checked.append(ids)
        if self.unavailable:
            raise OSError("offline fixture")
        return [{"event_id": id, "occurred_at_ms": 10_000} for id in ids if id in self.approved]
    async def close(self): pass


def setup(tmp_path, approved=("owned",)):
    legacy = tmp_path / "old"
    legacy.mkdir()
    (legacy / "owned.mp4").write_bytes(MP4)
    (legacy / "foreign.mp4").write_bytes(MP4)
    uploader = Uploader(approved)
    recorder = ClipRecorder(spool_dir=tmp_path / "new", uploader=uploader,
        encode_jpeg=lambda image: image, retain_local=True, max_pending_files=5)
    return legacy, uploader, recorder, LegacyClipMigrator(legacy, recorder)


async def test_migration_keeps_originals_and_only_imports_server_approved_events(tmp_path):
    legacy, uploader, recorder, migration = setup(tmp_path)
    await migration.step()
    assert (legacy / "owned.mp4").read_bytes() == MP4
    assert (legacy / "foreign.mp4").read_bytes() == MP4
    assert (recorder.spool_dir / "owned.mp4").read_bytes() == MP4
    assert not (recorder.spool_dir / "foreign.mp4").exists()
    resumed = LegacyClipMigrator(legacy, recorder)
    await resumed.step()
    assert len(list(recorder.spool_dir.glob("*.mp4"))) == 1
    assert "owned" not in uploader.checked[-1]
    await recorder.close()


async def test_unavailable_backend_and_interrupted_enrollment_preserve_legacy_files(tmp_path):
    legacy, uploader, recorder, migration = setup(tmp_path)
    uploader.unavailable = True
    with pytest.raises(OSError): await migration.step()
    assert not list(recorder.spool_dir.glob("*.mp4"))
    uploader.unavailable = False
    recorder._closed = True
    migration = LegacyClipMigrator(legacy, recorder)
    await migration.step()
    assert (legacy / "owned.mp4").read_bytes() == MP4
    assert not list(recorder.spool_dir.glob("*.mp4"))
    await recorder.close()


async def test_conflicting_copy_is_not_overwritten_or_acknowledged(tmp_path):
    legacy, uploader, recorder, migration = setup(tmp_path)
    (recorder.spool_dir / "owned.mp4").write_bytes(MP4 + b"different")
    with pytest.raises(ValueError): await migration.step()
    assert (recorder.spool_dir / "owned.mp4").read_bytes() == MP4 + b"different"
    assert (legacy / "owned.mp4").read_bytes() == MP4
    assert not (recorder.spool_dir / "legacy-migration.json").exists()
    await recorder.close()


async def test_completed_imports_do_not_resurrect_pruned_clips(tmp_path):
    legacy, uploader, recorder, migration = setup(tmp_path)
    await migration.step()
    (recorder.spool_dir / "owned.mp4").unlink()
    await LegacyClipMigrator(legacy, recorder).step()
    assert "owned" not in uploader.checked[-1]
    assert (legacy / "owned.mp4").read_bytes() == MP4
    assert not (recorder.spool_dir / "owned.mp4").exists()
    await recorder.close()


async def test_oversized_journal_preserves_sources_without_network(tmp_path):
    legacy, uploader, recorder, migration = setup(tmp_path)
    (recorder.spool_dir / "legacy-migration.json").write_bytes(b"x" * (32 * 1024 + 1))
    with pytest.raises(ValueError, match="journal too large"):
        await migration.step()
    assert uploader.checked == []
    assert (legacy / "owned.mp4").read_bytes() == MP4
    await recorder.close()


@pytest.mark.parametrize("agent_id", ["../foreign", "a/b", "a\\b", ".", "..", "x" * 129])
def test_agent_storage_namespace_rejects_traversal(agent_id):
    from mantau_agent.config import Settings
    with pytest.raises(ValueError):
        Settings(agent_id=agent_id)
