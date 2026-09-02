import os

import pytest

from packages.core.services.office_process import office_conversion_file_limit


def test_office_conversion_file_limit_applies_hard_posix_limit(monkeypatch):
    callback = office_conversion_file_limit(12345)
    if os.name != "posix":
        assert callback is None
        pytest.skip("POSIX file-size limits are unavailable")

    import resource

    observed = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: observed.append((kind, limits)))

    assert callback is not None
    callback()

    assert observed == [(resource.RLIMIT_FSIZE, (12345, 12345))]
