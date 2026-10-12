from __future__ import annotations

import pytest

from packages.core.services.site_analytics import page_address


@pytest.mark.parametrize(("raw", "expected"), [
    ("/", "/"),
    ("/?utm_source=newsletter", "/"),
    ("/about/", "/about"),
    ("/about?x=1", "/about"),
    ("/%E5%85%B3%E4%BA%8E", "/关于"),
    ("", "/"),
    (None, "/"),
])
def test_page_address_groups_one_page_under_one_address(raw, expected):
    assert page_address(raw) == expected
