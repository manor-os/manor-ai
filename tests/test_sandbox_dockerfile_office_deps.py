from pathlib import Path

from packages.core.ai.runtime.sandbox_toolset import load_manifest


def test_sandbox_image_installs_builtin_skill_system_dependencies():
    # The image installs every apt package of the toolset manifest
    # (tests/test_sandbox_toolset.py guards that wiring).
    apt = set(load_manifest().apt_packages())

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
        assert package in apt


def test_sandbox_image_uses_office_font_aliases_for_render_qa():
    dockerfile = Path("docker/Dockerfile.sandbox").read_text()
    fontconfig = Path("docker/fontconfig/70-manor-office-fonts.conf").read_text()
    apt = set(load_manifest().apt_packages())

    assert "70-manor-office-fonts.conf" in dockerfile
    assert "fc-cache -f" in dockerfile
    assert "fonts-crosextra-carlito" in apt
    assert "fonts-noto-core" in apt
    assert "fonts-noto-cjk" in apt
    assert "Aptos" in fontconfig
    assert "Carlito" in fontconfig
    # The font cache is rebuilt after the fonts are installed.
    assert dockerfile.index("/out/apt.txt") < dockerfile.index("fc-cache -f")
