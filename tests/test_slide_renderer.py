from pathlib import Path
from types import SimpleNamespace

from packages.core.services.slide_renderer import _render_first_page_sync


def test_render_first_page_copies_office_file_to_hint_extension_for_libreoffice(tmp_path, monkeypatch):
    source_path = tmp_path / "Personal Deck"
    source_path.write_bytes(b"PK\x03\x04pptx")
    out_dir = tmp_path / "thumbs"
    soffice_inputs: list[str] = []

    def fake_run(args, **kwargs):
        if args[0] == "soffice":
            soffice_input = args[-1]
            soffice_inputs.append(soffice_input)
            assert soffice_input.endswith(".pptx")
            assert Path(soffice_input).read_bytes() == source_path.read_bytes()
            pdf_path = Path(args[args.index("--outdir") + 1]) / f"{Path(soffice_input).stem}.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdftoppm":
            output_prefix = Path(args[-1])
            output_prefix.with_name(f"{output_prefix.name}-1.jpg").write_bytes(b"jpeg")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)

    rendered = _render_first_page_sync(str(source_path), str(out_dir), 150, source_ext=".pptx")

    assert Path(rendered).read_bytes() == b"jpeg"
    assert len(soffice_inputs) == 1
