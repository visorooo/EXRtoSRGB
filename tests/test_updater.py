"""
The in-app updater.

This is the one code path that ends in running an executable, so the checks
around it are the feature. Three things have to hold before anything is run:
the URL is an asset of this repository over https, the length matches what the
API said, and the SHA-256 matches the digest the API published.

A failure must also leave nothing behind - a half-written installer sitting in
the temp folder is exactly what a later run would find and trust.
"""

import hashlib
import os

import pytest

import exr2srgb as app


PAYLOAD = b"MZ" + b"not really an installer" * 100


def _info(**kw):
    d = {
        "available": True,
        "latest": "9.9.9",
        "asset": app.ASSET_PREFIX + "v9.9.9/EXRtoSRGB_Setup_v9.9.9.exe",
        "size": len(PAYLOAD),
        "digest": hashlib.sha256(PAYLOAD).hexdigest(),
    }
    d.update(kw)
    return d


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}
        self._at = 0

    def read(self, n=-1):
        chunk = self._data[self._at:self._at + n]
        self._at += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def served(monkeypatch):
    """Serve PAYLOAD for any urlopen, and report what was asked for."""
    asked = {}

    def fake_urlopen(req, timeout=None):
        asked["url"] = req.full_url
        return FakeResponse(PAYLOAD)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return asked


@pytest.mark.parametrize("url", [
    "https://evil.example.com/EXRtoSRGB_Setup.exe",
    "http://github.com/visorooo/EXRtoSRGB/releases/download/v1/x.exe",
    "https://github.com/someone-else/EXRtoSRGB/releases/download/v1/x.exe",
    "https://github.com/visorooo/EXRtoSRGB/issues/1",
    "",
])
def test_refuses_anything_that_is_not_our_release_asset(url, served):
    """https, this repository, the releases path. Nothing else gets fetched."""
    with pytest.raises(ValueError):
        app.download_update(_info(asset=url))
    assert "url" not in served, "it should refuse before making a request"


def test_downloads_and_verifies(served, monkeypatch, tmp_path):
    monkeypatch.setattr(app.tempfile, "gettempdir", lambda: str(tmp_path))
    path = app.download_update(_info())
    assert os.path.isfile(path)
    with open(path, "rb") as f:
        assert f.read() == PAYLOAD


def test_a_wrong_checksum_is_refused_and_nothing_is_left(served, monkeypatch,
                                                         tmp_path):
    """
    The check that matters. A file that is not what the release published must
    not survive to be run - by this launch or a later one.
    """
    monkeypatch.setattr(app.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(IOError):
        app.download_update(_info(digest="00" * 32))
    assert list(tmp_path.iterdir()) == [], "the bad download was left behind"


def test_a_short_download_is_refused_and_nothing_is_left(served, monkeypatch,
                                                        tmp_path):
    monkeypatch.setattr(app.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(IOError):
        app.download_update(_info(size=len(PAYLOAD) + 1))
    assert list(tmp_path.iterdir()) == []


def test_progress_is_reported(served, monkeypatch, tmp_path):
    monkeypatch.setattr(app.tempfile, "gettempdir", lambda: str(tmp_path))
    seen = []
    app.download_update(_info(), on_progress=lambda g, t: seen.append((g, t)))
    assert seen, "no progress was reported for a 40 MB download"
    assert seen[-1][0] == len(PAYLOAD)


def test_install_refuses_a_missing_file(tmp_path):
    """Never hand a path to CreateProcess without knowing it is there."""
    if os.name != "nt":
        pytest.skip("Windows only")
    with pytest.raises(IOError):
        app.install_update(str(tmp_path / "nope.exe"))
