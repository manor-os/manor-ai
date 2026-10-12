from pathlib import Path


def test_sandbox_image_installs_builtin_skill_system_dependencies():
    dockerfile = Path("docker/Dockerfile.sandbox").read_text()

    for package in (
        "ffmpeg",
        "gcc",
        "libreoffice-calc",
        "libreoffice-impress",
        "libreoffice-writer",
        "pandoc",
        "poppler-utils",
        "qpdf",
        "tesseract-ocr",
    ):
        assert package in dockerfile


def test_sandbox_image_uses_office_font_aliases_for_render_qa():
    dockerfile = Path("docker/Dockerfile.sandbox").read_text()
    fontconfig = Path("docker/fontconfig/70-manor-office-fonts.conf").read_text()

    assert "70-manor-office-fonts.conf" in dockerfile
    assert "fc-cache -f" in dockerfile
    assert "fonts-crosextra-carlito" in dockerfile
    assert "fonts-noto-core" in dockerfile
    assert "fonts-noto-cjk" in dockerfile
    assert "Aptos" in fontconfig
    assert "Carlito" in fontconfig
