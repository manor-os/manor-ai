"""Regression checks for XML parsers used by bundled document skills."""
from pathlib import Path
import re

import pytest
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException


SKILL_ROOT = Path(__file__).parents[1] / "packages" / "core" / "ai" / "skills"


def test_untrusted_skill_xml_has_no_stdlib_parse_calls():
    unsafe = re.compile(r"(?<!Safe)ET\.(?:parse|fromstring)\(")
    offenders = []
    for path in SKILL_ROOT.rglob("*.py"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if unsafe.search(line):
                offenders.append(f"{path.relative_to(SKILL_ROOT)}:{line_number}")
    assert offenders == []


def test_safe_parser_rejects_external_entity_payload():
    payload = b'<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><r>&xxe;</r>'
    with pytest.raises(DefusedXmlException):
        SafeET.fromstring(payload)
