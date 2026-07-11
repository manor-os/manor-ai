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
import { adjustDiagramDraftFromPrompt } from "../lib/diagram/aiDraft";
import { api } from "../lib/api";
import { useToastStore } from "../stores/toast";
import AiEditButton from "../components/ui/AiEditButton";
import { openEditorLiveChat } from "../lib/editorLiveChat";

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

  useEffect(() => {
    diagramRef.current = diagram;
  }, [diagram]);

  const diagramFileName = useMemo(
    () => `${diagram.title || "diagram"}.diagram.json`.replace(/[^\w\u4e00-\u9fa5.-]+/g, "-"),
    [diagram.title],
  );

  const diagramFile = useCallback(() => {
    return new File(
      [serializeDiagramDocument(diagram)],
      diagramFileName,
      { type: "application/json;charset=utf-8" },
    );
  }, [diagram, diagramFileName]);

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
    setDiagram(createDefaultDiagramDocument("Diagram canvas"));
    setKnowledgeDocId(null);
    setSaveLabel("Not saved");
  }, []);

  const handleDiagramChange = useCallback((next: EditableDiagramDocument) => {
    setDiagram(next);
    setSaveLabel((current) => knowledgeDocId && current !== "Saving..." ? "Unsaved changes" : current);
  }, [knowledgeDocId]);

  const saveToKnowledge = useCallback(async () => {
    setIsSavingKnowledge(true);
    setSaveLabel("Saving...");
    try {
      const file = diagramFile();
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

  const openLiveEdit = useCallback(async () => {
    const savedDoc = knowledgeDocId ? null : await saveToKnowledge();
    const documentId = knowledgeDocId || savedDoc?.id;
    const documentName = savedDoc?.name || diagramFileName;
    if (!documentId) return;
    openEditorLiveChat({
      documentId,
      documentName,
      fileType: "diagram",
      mimeType: "application/json",
      editorType: "Diagram",
      getContent: () => serializeDiagramDocument(diagramRef.current),
      localEditContent: (userRequest, currentContent) => {
        const current = parseDiagramDocument(
          currentContent,
          diagramRef.current.title || "Diagram canvas",
        );
        const next = adjustDiagramDraftFromPrompt(current, userRequest);
        const currentSerialized = serializeDiagramDocument(current);
        const nextSerialized = serializeDiagramDocument(next);
        return currentSerialized === nextSerialized ? null : nextSerialized;
      },
      applyContent: (next) => {
        let raw: unknown;
        try {
          raw = JSON.parse(next);
        } catch {
          setSaveLabel("AI edit returned invalid diagram JSON");
          return;
        }
        if (!isDiagramDocument(raw)) {
          setSaveLabel("AI edit returned a response, but not a diagram");
          return;
        }
        const previous = diagramRef.current;
        const parsed = parseDiagramDocument(next, diagramRef.current.title || "Diagram canvas");
        setDiagram(parsed);
        setSaveLabel(`AI updated diagram · ${summarizeDiagramAiEdit(previous, parsed)}`);
      },
    });
  }, [diagramFileName, knowledgeDocId, saveToKnowledge]);

  return (
    <div className="manor-editor-shell">
      <div className="manor-editor-header">
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 className="manor-editor-title">
            Diagram Canvas
          </h1>
          <p className="manor-editor-subtitle">
            {diagram.elements.length} editable objects
          </p>
        </div>
        <div className="manor-editor-actions">
          <AiEditButton
            onClick={() => void openLiveEdit()}
            disabled={isSavingKnowledge}
          />
          <button onClick={resetCanvas} className="btn-manor-ghost" style={{ fontSize: 12, padding: "6px 12px" }}>
            New file
          </button>
          <button
            onClick={saveToKnowledge}
            disabled={isSavingKnowledge}
            className="btn-manor-ghost"
            style={{ fontSize: 12, padding: "6px 12px", opacity: isSavingKnowledge ? 0.6 : 1 }}
          >
            {knowledgeDocId ? "Save changes" : "Save to Knowledge"}
          </button>
          {knowledgeDocId && (
            <button onClick={() => navigate(`/editor/${knowledgeDocId}`, { state: { knowledgeReturnTo: "/knowledge" } })} className="btn-manor-ghost" style={{ fontSize: 12, padding: "6px 12px" }}>
              Open saved
            </button>
          )}
          <button onClick={downloadJson} className="btn-manor" style={{ fontSize: 12, padding: "6px 14px" }}>
            JSON
          </button>
        </div>
      </div>
      {saveLabel && (
        <div className="manor-editor-substatus">
          {saveLabel}
        </div>
      )}
      <DiagramCanvas document={diagram} onChange={handleDiagramChange} />
    </div>
  );
}
