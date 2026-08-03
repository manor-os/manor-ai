"""ComfyUI workflow (JSON) importer.

ComfyUI has two export shapes:
  * **UI format**  — ``{"nodes": [...], "links": [[id, from, from_slot, to, to_slot, type]]}``
  * **API format** — ``{"<id>": {"class_type": ..., "inputs": {p: val | [src_id, slot]}}}``

ComfyUI nodes are diffusion-specific (KSampler, CheckpointLoader, …) and manor
has no diffusion execution backend. We still want imported graphs to *run*, so
the importer is **intent-extracting**: it traces the graph feeding each
generative output node (Save/Preview) to recover the positive prompt and aspect
ratio, and emits an executable manor ``image`` / ``video`` / ``audio`` node from
them. The diffusion internals (samplers / loaders / VAE) are preserved as
``unsupported`` placeholders so the graph stays intact for display, but the
output node alone reproduces the generation through ``generate_file``.
"""
from __future__ import annotations

from .base import WorkflowImporter
from .model import GraphNode, ImportReport, WorkflowGraph

# ComfyUI generative output nodes -> manor media node types. The graph's
# generation *intent* (image / video / audio) becomes an executable media node.
COMFY_MEDIA_MAP = {
    "SaveImage": "image", "PreviewImage": "image", "SaveImageWebsocket": "image",
    "VHS_VideoCombine": "video", "SaveAnimatedWEBP": "video", "SaveAnimatedPNG": "video",
    "SaveVideo": "video", "SaveWEBM": "video", "VHS_SaveVideo": "video",
    "SaveAudio": "audio", "PreviewAudio": "audio", "VHS_SaveAudio": "audio",
}

# Pure-annotation nodes (no execution, no edges) — dropped on import.
COMFY_DROP = {"Note", "MarkdownNote"}


def _is_text_encode(ct: str) -> bool:
    """Any CLIP/T5 text-encode class carries a prompt in its ``text`` widget."""
    return "TextEncode" in ct or ct in ("BNK_CLIPTextEncodeAdvanced", "T5TextEncode")


def _is_latent_size(ct: str) -> bool:
    """An ``Empty*Latent*`` source carries the target resolution — covers SD3,
    Cosmos, Hunyuan, LTXV, Flux and the classic ``EmptyLatentImage``."""
    return ct.startswith("Empty") and "Latent" in ct


def _is_output(ct: str) -> bool:
    """A generative output node (Save / Preview / video-combine)."""
    return ct in COMFY_MEDIA_MAP or ct.startswith(("Save", "Preview")) or "VideoCombine" in ct


def _media_kind(ct: str, latent_ct: str | None) -> str:
    """image / video / audio for an output node, from its own name and the
    latent type feeding it (a ``*LatentVideo`` source ⇒ video)."""
    if ct in COMFY_MEDIA_MAP:
        return COMFY_MEDIA_MAP[ct]
    hay = f"{ct} {latent_ct or ''}"
    if any(w in hay for w in ("Video", "Animated", "WEBM", "WEBP", "GIF", "Hunyuan", "Cosmos", "LTXV")):
        return "video"
    if "Audio" in hay:
        return "audio"
    return "image"


def _aspect_ratio(width: float, height: float) -> str | None:
    """Map ComfyUI's exact dims to the closest manor aspect ratio."""
    if not width or not height:
        return None
    r = float(width) / float(height)
    if r > 1.15:
        return "16:9"
    if r < 0.87:
        return "9:16"
    return "1:1"


class _Node:
    """Normalised view of a ComfyUI node across UI / API formats."""

    __slots__ = ("class_type", "edges", "literals")

    def __init__(self, class_type: str) -> None:
        self.class_type = class_type
        self.edges: dict[str, str] = {}      # input_name -> source node id
        self.literals: dict[str, object] = {}  # input_name -> literal value (text/width/height/…)


class ComfyUIImporter(WorkflowImporter):
    source_tool = "comfyui"

    @classmethod
    def detect(cls, raw: dict) -> bool:
        if not isinstance(raw, dict):
            return False
        # UI format
        if isinstance(raw.get("nodes"), list) and isinstance(raw.get("links"), list):
            return True
        # API format: dict of {id: {class_type, inputs}}
        vals = [v for v in raw.values() if isinstance(v, dict)]
        return bool(vals) and all("class_type" in v for v in vals)

    def parse(self, raw: dict, name: str | None = None) -> tuple[WorkflowGraph, ImportReport]:
        report = ImportReport(source_tool=self.source_tool)
        wf_name = name or "Imported ComfyUI workflow"
        report.warnings.append(
            "ComfyUI nodes are diffusion-specific; the graph is collapsed to its "
            "generative intent — an executable image/video/audio node with the "
            "prompt + aspect recovered from the graph. The diffusion plumbing "
            "(loaders / samplers / VAE / conditioning) is handled internally by "
            "manor's media node, so it's dropped rather than kept as placeholders."
        )

        if isinstance(raw.get("nodes"), list) and isinstance(raw.get("links"), list):
            order, index, succ = self._normalise_ui(raw)
        else:
            order, index, succ = self._normalise_api(raw)

        # Predecessor map for backward tracing from each output node.
        pred: dict[str, list[str]] = {}
        for src, dsts in succ.items():
            for d in dsts:
                pred.setdefault(d, []).append(src)

        # Collapse: emit one media node per distinct generation (drop the
        # diffusion plumbing entirely). Multiple output formats of the same
        # generation (e.g. SaveAnimatedWEBP + SaveAnimatedPNG) dedup to one node.
        nodes: list[GraphNode] = []
        seen: set = set()
        for nid in order:
            nd = index[nid]
            if not _is_output(nd.class_type):
                continue
            gen = self._extract_generation(nid, index, pred)
            kind = _media_kind(nd.class_type, gen.pop("_latent_ct", None))
            config: dict = {"kind": kind}
            config.update({k: v for k, v in gen.items() if v})
            key = (kind, config.get("prompt"))
            if key in seen:
                continue
            seen.add(key)
            nodes.append(GraphNode(
                id=nid, type=kind, name=nd.class_type, config=config, next=[],
                meta={"source_tool": self.source_tool, "original_type": nd.class_type},
            ))

        # No explicit Save/Preview output → synthesise one media node from the
        # sampler's (or last node's) generation intent, so the graph still
        # imports as something runnable rather than empty.
        if not nodes and order:
            anchor = next((nid for nid in order if "Sampler" in index[nid].class_type), order[-1])
            gen = self._extract_generation(anchor, index, pred)
            kind = _media_kind(index[anchor].class_type, gen.pop("_latent_ct", None))
            config = {"kind": kind}
            config.update({k: v for k, v in gen.items() if v})
            nodes.append(GraphNode(
                id=anchor, type=kind, name=index[anchor].class_type, config=config, next=[],
                meta={"source_tool": self.source_tool, "original_type": index[anchor].class_type},
            ))

        self._finalize(report, nodes)
        return WorkflowGraph(name=wf_name, source_tool=self.source_tool, nodes=nodes), report

    # ── Generation-intent extraction ─────────────────────────────────────

    def _extract_generation(
        self, media_id: str, index: dict[str, _Node], pred: dict[str, list[str]],
    ) -> dict:
        """Trace backward from a Save/Preview node to recover prompt + aspect."""
        ancestors = self._ancestors(media_id, pred)
        sampler = next(
            (a for a in ancestors if "Sampler" in index[a].class_type),
            None,
        )
        out: dict = {}
        if sampler is not None:
            s = index[sampler]
            out["prompt"] = self._text_of(s.edges.get("positive"), index)
            neg = self._text_of(s.edges.get("negative"), index)
            if neg:
                out["negative_prompt"] = neg
            lat = s.edges.get("latent_image") or s.edges.get("latent")
            if lat and lat in index:
                out["_latent_ct"] = index[lat].class_type
                w, h = index[lat].literals.get("width"), index[lat].literals.get("height")
                out["aspect_ratio"] = _aspect_ratio(w or 0, h or 0)

        # Fallbacks: first text-encode / first latent among ancestors.
        if not out.get("prompt"):
            for a in ancestors:
                if _is_text_encode(index[a].class_type):
                    out["prompt"] = self._text_of(a, index)
                    if out["prompt"]:
                        break
        for a in ancestors:
            if _is_latent_size(index[a].class_type):
                out.setdefault("_latent_ct", index[a].class_type)
                if not out.get("aspect_ratio"):
                    out["aspect_ratio"] = _aspect_ratio(
                        index[a].literals.get("width") or 0, index[a].literals.get("height") or 0,
                    )
                break
        return out

    @staticmethod
    def _text_of(node_id: str | None, index: dict[str, _Node]) -> str | None:
        """Resolve a text-encode node's prompt, following one edge if the text
        itself comes from an upstream primitive/string node."""
        seen: set[str] = set()
        cur = node_id
        while cur and cur in index and cur not in seen:
            seen.add(cur)
            n = index[cur]
            txt = n.literals.get("text") or n.literals.get("string") or n.literals.get("value")
            if isinstance(txt, str) and txt.strip():
                return txt.strip()
            cur = n.edges.get("text") or n.edges.get("string")
        return None

    @staticmethod
    def _ancestors(start: str, pred: dict[str, list[str]]) -> list[str]:
        """All transitive predecessors of ``start`` (BFS, order = proximity)."""
        out: list[str] = []
        seen = {start}
        queue = list(pred.get(start, []))
        while queue:
            nid = queue.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            out.append(nid)
            queue.extend(pred.get(nid, []))
        return out

    # ── Format normalisers → (order, {id: _Node}, succ) ──────────────────

    def _normalise_api(self, raw: dict) -> tuple[list[str], dict[str, _Node], dict[str, list[str]]]:
        order: list[str] = []
        index: dict[str, _Node] = {}
        succ: dict[str, list[str]] = {}
        for nid, body in raw.items():
            if not isinstance(body, dict):
                continue
            if (body.get("class_type") or "") in COMFY_DROP:
                continue
            sid = str(nid)
            order.append(sid)
            nd = _Node(body.get("class_type") or "unknown")
            for k, v in (body.get("inputs") or {}).items():
                if isinstance(v, list) and v and isinstance(v[0], (str, int)):
                    src = str(v[0])
                    nd.edges[k] = src
                    succ.setdefault(src, []).append(sid)
                else:
                    nd.literals[k] = v
            index[sid] = nd
        return order, index, succ

    def _normalise_ui(self, raw: dict) -> tuple[list[str], dict[str, _Node], dict[str, list[str]]]:
        # Flatten reusable subgraphs (new-format node expansion).
        subgraph_map = {
            str(sg.get("id")): sg
            for sg in ((raw.get("definitions") or {}).get("subgraphs") or [])
        }
        order: list[str] = []
        index: dict[str, _Node] = {}
        succ: dict[str, list[str]] = {}
        self._flatten_ui(raw, "", subgraph_map, set(), order, index, succ)
        return order, index, succ

    def _flatten_ui(
        self, container: dict, prefix: str, subgraph_map: dict, seen: set,
        order: list[str], index: dict[str, _Node], succ: dict[str, list[str]],
    ) -> None:
        # link id -> source node id (for resolving named input slots)
        link_src: dict[str, str] = {}
        for link in (container.get("links") or []):
            if isinstance(link, list) and len(link) >= 5:
                link_src[str(link[0])] = f"{prefix}{link[1]}"
                succ.setdefault(f"{prefix}{link[1]}", []).append(f"{prefix}{link[3]}")

        for rn in (container.get("nodes") or []):
            nid = f"{prefix}{rn.get('id')}"
            ctype = rn.get("type") or "unknown"
            sub = subgraph_map.get(ctype)
            if sub is not None and ctype not in seen:
                self._flatten_ui(sub, f"{nid}/", subgraph_map, seen | {ctype}, order, index, succ)
                continue
            if ctype in COMFY_DROP:
                continue
            nd = _Node(ctype)
            # named input slots -> source nodes via links
            for inp in (rn.get("inputs") or []):
                if isinstance(inp, dict) and inp.get("link") is not None and inp.get("name"):
                    src = link_src.get(str(inp["link"]))
                    if src is not None:
                        nd.edges[str(inp["name"])] = src
            # positional widgets -> named literals for the classes we read
            self._map_widgets(ctype, rn.get("widgets_values"), nd)
            index[nid] = nd
            order.append(nid)

    @staticmethod
    def _map_widgets(ctype: str, widgets, nd: _Node) -> None:
        """ComfyUI UI nodes store widget values positionally; name the few we use."""
        if not isinstance(widgets, list) or not widgets:
            return
        if _is_text_encode(ctype) and isinstance(widgets[0], str):
            nd.literals["text"] = widgets[0]
        elif _is_latent_size(ctype):
            if len(widgets) >= 1 and isinstance(widgets[0], (int, float)):
                nd.literals["width"] = widgets[0]
            if len(widgets) >= 2 and isinstance(widgets[1], (int, float)):
                nd.literals["height"] = widgets[1]
        elif ctype in ("PrimitiveNode", "PrimitiveString", "String", "Text") and isinstance(widgets[0], str):
            nd.literals["value"] = widgets[0]
