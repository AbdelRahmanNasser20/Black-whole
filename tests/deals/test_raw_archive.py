import gzip, io, json
import pytest
from unittest.mock import patch
from deals import raw_archive as ra


def _rows(n=3):
    return [{"asset_id": i, "account_id": i * 10, "auction_id": 1,
             "raw": {"assetId": i, "currentBid": 1.5}} for i in range(1, n + 1)]


def test_serialize_parse_round_trip():
    rows = _rows()
    keys = ra.parse_batch(ra.serialize_batch(rows))
    assert keys == [(1, 10, 1), (2, 20, 1), (3, 30, 1)]


def test_serialize_preserves_the_blob_not_just_the_key():
    blob = ra.serialize_batch(_rows(1))
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as gz:
        rec = json.loads(gz.read())
    assert rec["raw"] == {"assetId": 1, "currentBid": 1.5}


class FakeS3:
    """Minimal in-memory S3: put stores, get returns. Records the bucket and
    the put kwargs so tests can prove WHERE (and how) the blob landed."""
    def __init__(self, corrupt=False, fail=False):
        self.store, self.corrupt, self.fail = {}, corrupt, fail
        self.puts = []

    def put_object(self, Bucket, Key, Body, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.puts.append((Bucket, Key, kw))
        self.store[(Bucket, Key)] = ra.serialize_batch(_rows(1)) if self.corrupt else Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.store[(Bucket, Key)])}


PUBLIC, PRIVATE = "blackwhole-images", "blackwhole-archive"
CFG = {"bucket": PUBLIC, "public_base": "https://pub.r2.dev",
       "account": "a", "access_key": "k", "secret_key": "s", "endpoint": "e"}


@pytest.fixture(autouse=True)
def _buckets(monkeypatch):
    monkeypatch.setenv("R2_BUCKET", PUBLIC)
    monkeypatch.setenv("LOT_ARCHIVE_R2_BUCKET", PRIVATE)


def _run(fake, **kw):
    with patch.object(ra.r2_images, "env_config", return_value=CFG), \
         patch.object(ra.r2_images, "client", return_value=fake), \
         patch.object(ra.db, "fetch_one", return_value={"n": 3}), \
         patch.object(ra.db, "fetch_all", return_value=_rows()), \
         patch.object(ra.db, "executemany") as em:
        return ra.run_archive_raw(**kw), em


def test_happy_path_exports_verifies_then_nulls():
    meter, em = _run(FakeS3())
    assert meter["exported"] == 3 and meter["nulled"] == 3
    assert meter["key"].startswith(ra.ARCHIVE_PREFIX)
    em.assert_called_once()


def test_upload_failure_nulls_nothing():
    with pytest.raises(RuntimeError, match="nothing nulled"):
        _run(FakeS3(fail=True))


def test_readback_mismatch_nulls_nothing():
    """The whole safety property: bytes that land corrupted must not be
    treated as a durable archive."""
    with pytest.raises(RuntimeError, match="readback mismatch"):
        _run(FakeS3(corrupt=True))


def test_refuses_to_run_without_r2_configured():
    with patch.object(ra.r2_images, "env_config", return_value=None):
        with pytest.raises(RuntimeError, match="refusing to archive"):
            ra.run_archive_raw()


def test_empty_backlog_is_a_noop():
    with patch.object(ra.r2_images, "env_config", return_value=CFG), \
         patch.object(ra.db, "fetch_one", return_value={"n": 0}), \
         patch.object(ra.db, "fetch_all", return_value=[]):
        meter = ra.run_archive_raw()
    assert meter["exported"] == 0 and meter["nulled"] == 0


def test_writes_to_the_private_bucket_never_the_public_one():
    fake = FakeS3()
    _run(fake)
    assert [b for b, _, _ in fake.puts] == [PRIVATE]
    # No public-serving cache header on a private object.
    assert "CacheControl" not in fake.puts[0][2]


@pytest.mark.parametrize("value", ["", PUBLIC])
def test_raises_when_private_bucket_unset_or_public(monkeypatch, value):
    """Unset (or pointed at the public image bucket) is a hard stop BEFORE any
    DB read or upload — never a fallback to R2_BUCKET."""
    monkeypatch.setenv("LOT_ARCHIVE_R2_BUCKET", value)
    fake = FakeS3()
    with pytest.raises(ra.r2_images.PrivateBucketNotConfigured):
        _run(fake)
    assert fake.puts == []


def test_put_private_object_refuses_the_public_bucket():
    with pytest.raises(ra.r2_images.PrivateBucketNotConfigured):
        ra.r2_images.put_private_object(FakeS3(), bucket=PUBLIC, path="x",
                                        data=b"", content_type="text/plain")
