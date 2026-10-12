"""Presentation-only checks for Manor's existing branded email templates."""

from html.parser import HTMLParser

import pytest

from packages.core.services import email_service


APP_URL = "https://manor-template.test"


class _EmailElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@pytest.mark.parametrize("content", [
    '<p><strong>TEST ONLY — agreement.pdf</strong> has been signed.</p>',
    '<h1>Account update</h1><p>A synthetic notification.</p><ul><li>Existing content remains readable.</li></ul>',
    '<p>测试通知：文件已保存。</p><p>Résumé &amp; information.</p>',
])
def test_manor_common_email_preserves_generic_content_and_brand(monkeypatch, content):
    monkeypatch.setattr(email_service, "_template_cache", {})
    rendered = email_service._render("common", content=content, app_url=APP_URL, year="2026")
    assert content in rendered
    assert "{{" not in rendered and "}}" not in rendered
    assert "&copy; Manor AI 2026" in rendered
    elements = _EmailElements()
    elements.feed(rendered)
    assert not {"script", "iframe", "object"} & {tag for tag, _attrs in elements.elements}
    images = [attrs for tag, attrs in elements.elements if tag == "img"]
    assert len(images) == 1
    assert images[0]["src"] == f"{APP_URL}/email-logo.png"
    assert images[0]["alt"] == "Manor AI"
    assert "Plus Jakarta Sans" in rendered
    assert "Empowering Progress, Simplifying Success" in rendered
    assert "View in a web browser" in rendered
    assert "max-width: 600px" in rendered
    assert "@media screen and (max-width: 640px)" in rendered


def test_manor_common_email_keeps_existing_content_links_and_brand_link(monkeypatch):
    monkeypatch.setattr(email_service, "_template_cache", {})
    url = "https://synthetic.example.test/preview?mode=test&amp;page=2#example"
    content = f'<p><a href="{url}">Existing synthetic link</a></p>'
    rendered = email_service._render("common", content=content, app_url=APP_URL, year="2026")
    assert content in rendered
    elements = _EmailElements()
    elements.feed(rendered)
    links = [attrs for tag, attrs in elements.elements if tag == "a"]
    assert [link["href"] for link in links] == [APP_URL, url.replace("&amp;", "&")]
    assert links[1] == {"href": url.replace("&amp;", "&")}


def test_manor_common_email_keeps_long_document_names_intact(monkeypatch):
    monkeypatch.setattr(email_service, "_template_cache", {})
    filename = "synthetic_contract_" * 24 + ".pdf"
    content = f"<p><strong>{filename}</strong></p>"
    rendered = email_service._render("common", content=content, app_url=APP_URL, year="2026")
    assert filename in rendered
    assert content in rendered
    assert "{{" not in rendered and "}}" not in rendered


def test_manor_verification_email_keeps_code_expiry_security_copy_and_brand(monkeypatch):
    monkeypatch.setattr(email_service, "_template_cache", {})
    rendered = email_service._render("verification", code="123456", app_url=APP_URL, year="2026")
    assert "{{" not in rendered and "}}" not in rendered
    assert rendered.count("123456") == 1
    assert "&copy; Manor AI 2026" in rendered
    assert "This code expires in 10 minutes." in rendered
    assert "Please do not share this code with anyone." in rendered
    assert "never ask you for your verification code via email or phone" in rendered
    assert "If you didn't request this code, please ignore this email." in rendered
    elements = _EmailElements()
    elements.feed(rendered)
    assert not {"script", "iframe", "object", "a"} & {tag for tag, _attrs in elements.elements}
    images = [attrs for tag, attrs in elements.elements if tag == "img"]
    assert len(images) == 1
    assert images[0]["src"] == f"{APP_URL}/email-logo.png"
    assert images[0]["alt"] == "Manor AI"
    assert "Verify Your Email" in rendered
    assert "Security Reminder" in rendered
    assert "Empowering Progress, Simplifying Success" in rendered
    assert "max-width: 600px" in rendered
    assert "@media screen and (max-width: 640px)" in rendered
