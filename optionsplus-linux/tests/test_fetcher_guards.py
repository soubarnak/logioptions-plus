"""Seam: fetcher download size cap and the single-fetch lock."""
import fcntl
import os

import pytest

from opl_agent import fetch as fetchmod

from tests.test_fetcher import fetch, world  # noqa: F401  (world is a fixture)


class _Stream:
    def __init__(self, total):
        self.left, self.served = total, 0

    def read(self, n):
        n = min(n, self.left)
        self.left -= n
        self.served += n
        return b"x" * n

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, total):
    s = _Stream(total)
    monkeypatch.setattr(fetchmod.urllib.request, "urlopen", lambda *a, **k: s)
    return s


def test_oversize_stream_is_aborted_and_the_partial_file_deleted(monkeypatch, tmp_path):
    s = _serve(monkeypatch, 10 << 20)
    dest = tmp_path / "x.depot"
    with pytest.raises(fetchmod.FetchError, match="larger than the pinned 1000 bytes"):
        fetchmod.download("http://127.0.0.1/x", str(dest), lambda n: None, 1000)
    assert s.served < (1 << 20)         # stopped early, did not read to EOF
    assert not dest.exists()


def test_short_download_is_rejected_and_deleted(monkeypatch, tmp_path):
    _serve(monkeypatch, 999)
    dest = tmp_path / "x.depot"
    with pytest.raises(fetchmod.FetchError, match="999 bytes, expected the pinned 1000"):
        fetchmod.download("http://127.0.0.1/x", str(dest), lambda n: None, 1000)
    assert not dest.exists()


def test_exact_size_is_accepted(monkeypatch, tmp_path):
    _serve(monkeypatch, 1000)
    dest = tmp_path / "x.depot"
    fetchmod.download("http://127.0.0.1/x", str(dest), lambda n: None, 1000)
    assert dest.stat().st_size == 1000


def test_entry_without_size_is_capped_by_the_hard_max(monkeypatch, tmp_path):
    s = _serve(monkeypatch, 5 << 20)
    monkeypatch.setattr(fetchmod, "HARD_MAX", 1 << 20)
    with pytest.raises(fetchmod.FetchError, match="larger than"):
        fetchmod.download("http://127.0.0.1/x", str(tmp_path / "x"), lambda n: None)
    assert s.served < (2 << 20)


def test_oversize_depot_fails_the_cli_run_and_installs_nothing(world):
    world.srv.routes["/depots/test_mouse.depot"] += b"extra"
    r = fetch(world, "--accept-eula")
    assert r.returncode != 0
    assert "larger than the pinned" in r.stderr
    assert not world.payload.exists()


def test_second_concurrent_run_is_refused(world):
    parent = world.payload.parent
    parent.mkdir(parents=True)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        hits = len(world.srv.hits)
        r = fetch(world, "--accept-eula")
        assert r.returncode != 0
        assert "another fetch is running" in r.stderr
        assert len(world.srv.hits) == hits
        assert not world.payload.exists()
    finally:
        os.close(fd)
    assert fetch(world, "--accept-eula").returncode == 0     # once the holder is gone it proceeds


def test_lock_is_released_after_a_failed_fetch(tmp_path):
    dest = str(tmp_path / "data" / "payload")

    def boom(staging):
        raise fetchmod.FetchError("boom")

    with pytest.raises(fetchmod.FetchError, match="boom"):
        fetchmod.stage_and_swap(dest, boom)
    fd = os.open(os.path.dirname(dest), os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)      # raises if still held
    finally:
        os.close(fd)


def test_lock_is_held_during_collect(tmp_path):
    dest = str(tmp_path / "data" / "payload")
    seen = []

    def collect(staging):
        with pytest.raises(fetchmod.FetchError, match="another fetch is running"):
            fetchmod.stage_and_swap(dest, lambda s: seen.append(1))
        raise fetchmod.FetchError("stop")

    with pytest.raises(fetchmod.FetchError, match="stop"):
        fetchmod.stage_and_swap(dest, collect)
    assert not seen
