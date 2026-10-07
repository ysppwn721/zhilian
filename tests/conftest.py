"""Keep test quotas separate from user data and independent across tests."""
import pytest

from zhilian import quota


@pytest.fixture(autouse=True)
def isolated_quota_file(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, '_PATH', tmp_path / 'quota.json')
