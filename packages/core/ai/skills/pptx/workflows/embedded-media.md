# Embedded video in a PPTX

Use this only when the user asks for playable video inside the presentation.
The video must already be a local, authorized file under the current project;
an external URL or a private file path is not an import grant. Reserve a
rectangle in the source SVG with `data-video-slot="<id>"` and add a matching
entry to `<project>/embedded_media.json`:

```json
{
  "version": 1,
  "slides": {
    "P02": [{
      "id": "demo-video",
      "type": "video",
      "source": "media/demo.mp4",
      "slot": "demo-video",
      "position": {"x": 100, "y": 190, "width": 960, "height": 400},
      "fit": "contain",
      "play": "on_click"
    }]
  }
}
```

Use a local poster image when available. `scripts/svg_to_pptx.py` validates
the media, slot and poster and embeds a real PowerPoint video object. The final
quality gate verifies the declared count. If the current sandbox cannot
receive the source video through its authorized file boundary, tell the user
the video cannot be embedded in this run; do not claim that a link is an
embedded video or try an unapproved private source.
