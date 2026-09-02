import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import DiagramCanvas from "../components/diagram/DiagramCanvas";
import {
  createDefaultDiagramDocument,
  isDiagramDocument,
  parseDiagramDocument,
  serializeDiagramDocument,
  type EditableDiagramDocument,
} from "../lib/diagram/schema";
import { api } from "../lib/api";
import { useToastStore } from "../stores/toast";
import AiEditButton from "../components/ui/AiEditButton";
import AiEditPreviewControls from "../components/ui/AiEditPreviewControls";
import PageHeader from "../components/ui/PageHeader";
import {
  AiEditPreviewStatus,
  AiEditTargetKind,
  createAiEditCommitCoordinator,
  createEditorLiveAdapter,
  nextEditorLiveChangeCount,
  openEditorLiveChat,
  type EditorLiveApplyMeta,
} from "../lib/editorLiveChat";

type DiagramAiPreview = {
  baseline: EditableDiagramDocument;
  current: EditableDiagramDocument;
  status: AiEditPreviewStatus;
  changeCount: number;
};

function summarizeDiagramAiEdit(before: EditableDiagramDocument, after: EditableDiagramDocument) {
  const beforeElements = new Map(before.elements.map((element) => [element.id, JSON.stringify(element)]));
  const afterElements = new Map(after.elements.map((element) => [element.id, JSON.stringify(element)]));
  const added = after.elements.filter((element) => !beforeElements.has(element.id)).length;
  const removed = before.elements.filter((element) => !afterElements.has(element.id)).length;
  const updated = after.elements.filter((element) => {
    const previous = beforeElements.get(element.id);
    return previous !== undefined && previous !== JSON.stringify(element);
  }).length;
  const parts = [
    added ? `+${added} object${added === 1 ? "" : "s"}` : "",
    removed ? `-${removed} object${removed === 1 ? "" : "s"}` : "",
    updated ? `${updated} updated` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "diagram updated";
}

export default function DiagramStudio() {
  const navigate = useNavigate();
  const toast = useToastStore();
  const initial = useMemo(() => createDefaultDiagramDocument("Diagram canvas"), []);
  const [diagram, setDiagram] = useState<EditableDiagramDocument>(initial);
  const [knowledgeDocId, setKnowledgeDocId] = useState<string | null>(null);
  const [isSavingKnowledge, setIsSavingKnowledge] = useState(false);
  const [saveLabel, setSaveLabel] = useState("Not saved");
  const diagramRef = useRef(diagram);
  const [aiPreview, setAiPreview] = useState<DiagramAiPreview | null>(null);
  const aiPreviewRef = useRef<DiagramAiPreview | null>(null);
  const aiEditCommitCoordinator = useRef(createAiEditCommitCoordinator()).current;
  const aiEditDraftTargetId = useRef(
    `diagram-draft-${Date.now()}-${Math.random().toString(36).slice(2)}`,
  ).current;

  useEffect(() => {
    diagramRef.current = diagram;
  }, [diagram]);

  const diagramFileName = useMemo(
    () => `${diagram.title || "diagram"}.diagram.json`.replace(/[^\w\u4e00-\u9fa5.-]+/g, "-"),
    [diagram.title],
  );

  const diagramFile = useCallback((source = diagramRef.current) => {
    const fileName = `${source.title || "diagram"}.diagram.json`.replace(/[^\w\u4e00-\u9fa5.-]+/g, "-");
    return new File(
      [serializeDiagramDocument(source)],
      fileName,
      { type: "application/json;charset=utf-8" },
    );
  }, []);

  const downloadJson = useCallback(() => {
    const blob = diagramFile();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = diagramFileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }, [diagramFile, diagramFileName]);

  const resetCanvas = useCallback(() => {
    if (aiPreviewRef.current) return;
    setDiagram(createDefaultDiagramDocument("Diagram canvas"));
    setKnowledgeDocId(null);
    setSaveLabel("Not saved");
  }, []);

  const handleDiagramChange = useCallback((next: EditableDiagramDocument) => {
    if (aiPreviewRef.current) return;
    setDiagram(next);
    setSaveLabel((current) => knowledgeDocId && current !== "Saving..." ? "Unsaved changes" : current);
  }, [knowledgeDocId]);

  const saveToKnowledge = useCallback(async (source = diagramRef.current) => {
    setIsSavingKnowledge(true);
    setSaveLabel("Saving...");
    try {
      const file = diagramFile(source);
      const doc = knowledgeDocId
        ? await api.documents.replaceFile(knowledgeDocId, file)
        : await api.documents.upload(file);
      setKnowledgeDocId(doc.id);
      setSaveLabel(`Saved as ${doc.name || file.name}`);
      toast.success("Diagram saved to Knowledge", doc.name || file.name);
      return doc;
    } catch (error) {
      setSaveLabel("Save failed");
      toast.error("Could not save diagram", error instanceof Error ? error.message : undefined);
      return null;
    } finally {
      setIsSavingKnowledge(false);
    }
  }, [diagramFile, knowledgeDocId, toast]);

  const rollbackAiPreview = useCallback(() => {
    if (aiEditCommitCoordinator.isCommitting()) return;
    const preview = aiPreviewRef.current;
    if (!preview) return;
    diagramRef.current = preview.baseline;
    setDiagram(preview.baseline);
    aiPreviewRef.current = null;
    setAiPreview(null);
    setSaveLabel("AI preview discarded");
  }, [aiEditCommitCoordinator]);

  const completeAiPreview = useCallback((_content: string, meta: EditorLiveApplyMeta) => {
    if (meta.signal?.aborted) return false;
    const preview = aiPreviewRef.current;
    if (!preview) return false;
    const ready = { ...preview, status: AiEditPreviewStatus.Ready };
    aiPreviewRef.current = ready;
    setAiPreview(ready);
    return true;
  }, []);

  const beginAiEditTurn = useCallback((meta: EditorLiveApplyMeta) => {
    if (meta.signal?.aborted) return false;
    const preview = aiPreviewRef.current;
    if (!preview) return true;
    const pending = { ...preview, status: AiEditPreviewStatus.Animating };
    aiPreviewRef.current = pending;
    setAiPreview(pending);
    return true;
  }, []);

  const acceptAiPreview = useCallback(async () => {
    if (aiEditCommitCoordinator.isCommitting()) return;
    const preview = aiPreviewRef.current;
    if (!preview || preview.status !== AiEditPreviewStatus.Ready) return;
    await aiEditCommitCoordinator.run(async () => {
      const saved = await saveToKnowledge(preview.current);
      if (!saved) return;
      aiPreviewRef.current = null;
      setAiPreview(null);
    });
  }, [aiEditCommitCoordinator, saveToKnowledge]);

  const openLiveEdit = useCallback(() => {
    const documentId = knowledgeDocId || undefined;
    const targetId = knowledgeDocId || aiEditDraftTargetId;
    const documentName = diagramFileName;
    const applyDiagramPreview = (next: string, meta: EditorLiveApplyMeta) => {
      if (meta.signal?.aborted) return false;
      let raw: unknown;
      try {
        raw = JSON.parse(next);
      } catch {
        setSaveLabel("AI edit returned invalid diagram JSON");
        return false;
      }
      if (!isDiagramDocument(raw)) {
        setSaveLabel("AI edit returned a response, but not a diagram");
        return false;
      }
      const previousPreview = aiPreviewRef.current;
      const previous = diagramRef.current;
      const parsed = parseDiagramDocument(next, diagramRef.current.title || "Diagram canvas");
      const preview: DiagramAiPreview = {
        baseline: previousPreview?.baseline ?? structuredClone(previous),
        current: parsed,
        status: AiEditPreviewStatus.Animating,
        changeCount: nextEditorLiveChangeCount(previousPreview, meta),
      };
      aiPreviewRef.current = preview;
      setAiPreview(preview);
      diagramRef.current = parsed;
      setDiagram(parsed);
      setSaveLabel(`AI updated diagram · ${summarizeDiagramAiEdit(previous, parsed)}`);
      return true;
    };
    openEditorLiveChat({
      documentId,
      documentName,
      fileType: "diagram",
      mimeType: "application/json",
      editorType: "Diagram",
      adapter: createEditorLiveAdapter({
        target: { kind: AiEditTargetKind.Diagram, id: targetId },
        read: () => serializeDiagramDocument(diagramRef.current),
        getTurnPreviewState: () => ({
          changeCount: aiPreviewRef.current?.changeCount || 0,
        }),
        beginTurn: beginAiEditTurn,
        preview: applyDiagramPreview,
        complete: completeAiPreview,
        rollback: rollbackAiPreview,
        commitCoordinator: aiEditCommitCoordinator,
      }),
    });
  }, [aiEditCommitCoordinator, aiEditDraftTargetId, beginAiEditTurn, completeAiPreview, diagramFileName, knowledgeDocId, rollbackAiPreview]);

  return (
    <div className="manor-editor-shell">
      <PageHeader
        title="Diagram Canvas"
        subtitle={`${diagram.elements.length} editable objects`}
        actions={(
          <div className="manor-editor-actions">
            <AiEditButton
              onClick={() => void openLiveEdit()}
              disabled={isSavingKnowledge}
            />
            <button onClick={resetCanvas} disabled={Boolean(aiPreview)} className="btn-manor-ghost" style={{ fontSize: 12, padding: "6px 12px" }}>
              New file
            </button>
            <button
              onClick={() => { void saveToKnowledge(); }}
              disabled={isSavingKnowledge || Boolean(aiPreview)}
              className="btn-manor-ghost"
              style={{ fontSize: 12, padding: "6px 12px", opacity: isSavingKnowledge ? 0.6 : 1 }}
            >
              {knowledgeDocId ? "Save changes" : "Save to Knowledge"}
            </button>
            {knowledgeDocId && (
              <button disabled={Boolean(aiPreview)} onClick={() => navigate(`/editor/${knowledgeDocId}`, { state: { knowledgeReturnTo: "/knowledge" } })} className="btn-manor-ghost" style={{ fontSize: 12, padding: "6px 12px" }}>
                Open saved
              </button>
            )}
            <button onClick={downloadJson} className="btn-manor" style={{ fontSize: 12, padding: "6px 14px" }}>
              JSON
            </button>
          </div>
        )}
      />
      {saveLabel && (
        <div className="manor-editor-substatus">
          {saveLabel}
        </div>
      )}
      {aiPreview && (
        <div className="manor-editor-ai-preview-row">
          <AiEditPreviewControls
            status={aiPreview.status}
            changeCount={aiPreview.changeCount}
            accepting={isSavingKnowledge}
            onAccept={acceptAiPreview}
            onDiscard={rollbackAiPreview}
          />
        </div>
      )}
      <DiagramCanvas document={diagram} onChange={handleDiagramChange} />
    </div>
  );
}
