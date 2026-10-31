import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { api, type DocumentUploadProgress, type WorkspaceAppAction } from "../../src/lib/api";
import ChatActionCard from "../../src/components/ui/ChatActionCard";
import WorkspaceAppActionFlow from "../../src/components/workspaces/WorkspaceAppActionFlow";
import "../../src/index.css";
import "../../src/components/workspaces/WorkspaceAppView.css";

// Isolated UI fixture: no real Workflow, provider, or billable API calls.
const params = new URLSearchParams(location.search);
const workspaceId = "inputs-fixture";
const actions: WorkspaceAppAction[] = [{
  key: "analyze", kind: "workflow", title: "Analyze Reel or Post", binding_id: "analyze-binding", workflow_id: "analyze-flow",
  inputs: [
    { key: "source", label: "Source caption", type: "string", required: true, options: [], schema: { type: "string", "x-ui": { control: "textarea" } } },
    { key: "mode", label: "Analysis mode", type: "string", required: true, options: [], default: "text_only", schema: { type: "string", enum: ["text_only", "full_media"] } },
  ],
}, {
  key: "discover", kind: "workflow", title: "Discover content", binding_id: "discover-binding", workflow_id: "discover-flow",
  inputs: [{ key: "days", label: "Lookback days", type: "number", required: true, options: [], default: 14, schema: { type: "integer", minimum: 1, maximum: 28 } }],
}, {
  key: "upload-materials", kind: "workflow", title: "Upload materials", binding_id: "upload-materials-binding", workflow_id: "upload-materials-flow",
  inputs: [{
    key: "materials", label: "Materials", type: "json", required: true, options: [],
    schema: { type: "array", items: { type: "object" }, "x-ui": { control: "documents" } },
  }],
}, {
  key: "upload-two-materials", kind: "workflow", title: "Upload two material sets", binding_id: "upload-two-materials-binding", workflow_id: "upload-two-materials-flow",
  inputs: [{
    key: "materials", label: "Materials", type: "json", required: true, options: [], schema: {
      type: "object", properties: {
        sources: { type: "array", title: "Source files", items: { type: "object" }, "x-ui": { control: "documents" } },
        references: { type: "array", title: "Reference files", items: { type: "object" }, "x-ui": { control: "documents" } },
      }, required: ["sources", "references"],
    },
  }],
}, { key: "review", kind: "workflow", title: "Review saved content", binding_id: "review-binding", workflow_id: "review-flow", inputs: [] }];

type PendingUpload = {
  name: string;
  uploadKey: string | undefined;
  folderId: string | null;
  progress?: (event: DocumentUploadProgress) => void;
  resolve: (document: any) => void;
  reject: (reason: Error) => void;
};

type UploadFixtureControls = {
  started: () => Array<{ name: string; uploadKey: string | undefined; folderId: string | null }>;
  progress: (name: string, percent: number, phase?: DocumentUploadProgress["phase"]) => void;
  succeed: (name: string) => void;
  fail: (name: string, message?: string) => void;
  finishLinking: () => void;
  finishNextLink: () => void;
  linkCalls: () => number;
  uploadKey: (name: string) => string | undefined;
};

declare global {
  interface Window { workspaceAppUploadFixture: UploadFixtureControls; }
}

const pendingUploads: PendingUpload[] = [];
let linkedDocumentIds: string[] = [];
let linkCalls = 0;
const linkResolvers: Array<() => void> = [];

const workspaceRootFolderId = "fixture-workspace-root";
api.documents.upload = ((file: File, folderId?: string | null, _options?: unknown, requestOptions?: { onProgress?: (event: DocumentUploadProgress) => void; idempotencyKey?: string }) => new Promise((resolve, reject) => {
  pendingUploads.push({ name: file.name, uploadKey: requestOptions?.idempotencyKey, folderId: folderId ?? null, progress: requestOptions?.onProgress, resolve, reject });
})) as typeof api.documents.upload;
api.workspaces.get = (async (id: string) => ({ id, artifact_folder_id: workspaceRootFolderId })) as typeof api.workspaces.get;
api.workspaces.documents = (async () => [{ id: "fixture-materials", is_default_collection: true, documents: linkedDocumentIds.map((id) => ({ id })) }]) as typeof api.workspaces.documents;
api.workspaces.knowledge.addDocuments = (async (_workspaceId: string, _groupId: string, documentIds: string[]) => {
  linkCalls += 1;
  await new Promise<void>((resolve) => linkResolvers.push(resolve));
  linkedDocumentIds = [...new Set([...linkedDocumentIds, ...documentIds])];
  return { added: documentIds.length, skipped: [], total: linkedDocumentIds.length };
}) as typeof api.workspaces.knowledge.addDocuments;

window.workspaceAppUploadFixture = {
  started: () => pendingUploads.map(({ name, uploadKey, folderId }) => ({ name, uploadKey, folderId })),
  progress: (name, percent, phase = "uploading") => {
    const upload = [...pendingUploads].reverse().find((item) => item.name === name);
    if (!upload) throw new Error(`No pending fixture upload named ${name}`);
    upload.progress?.({ phase, loaded: percent, total: 100, percent });
  },
  succeed: (name) => {
    const upload = [...pendingUploads].reverse().find((item) => item.name === name);
    if (!upload) throw new Error(`No pending fixture upload named ${name}`);
    upload.resolve({ id: `fixture-document-${name}`, name, fs_path: `/fixture/${name}` });
  },
  fail: (name, message = "Synthetic upload failure") => {
    const upload = [...pendingUploads].reverse().find((item) => item.name === name);
    if (!upload) throw new Error(`No pending fixture upload named ${name}`);
    upload.reject(new Error(message));
  },
  finishLinking: () => { while (linkResolvers.length) linkResolvers.shift()?.(); },
  finishNextLink: () => linkResolvers.shift()?.(),
  linkCalls: () => linkCalls,
  uploadKey: (name) => [...pendingUploads].reverse().find((item) => item.name === name)?.uploadKey,
};

let current: any;
let submitted: unknown;
api.workspaces.setupStatus = async () => ({ ready: !params.has("setup"), incomplete_checks: params.has("setup") ? [{ kind: "missing_skill", reason: "Connect the required Skill." }] : [] } as any);
api.workflows.listRuns = async () => [];
api.workflows.getRun = async () => current;
api.workflows.runBinding = async (bindingId, payload) => {
  submitted = { bindingId, ...payload };
  const action = actions.find((item) => item.binding_id === bindingId)!;
  current = { id: "fixture-run", workspace_id: workspaceId, workflow_id: action.workflow_id, binding_id: bindingId,
    status: "completed", variables: { __result: { summary: "The fixture run completed in the App." } } };
  return current;
};
function Fixture() {
  const [selected, setSelected] = useState(actions[0]);
  const [chatRequests, setChatRequests] = useState(0);
  if (params.has("mixed-card")) {
    return <main style={{ maxWidth: 720, padding: 24, margin: "auto" }}>
      <ChatActionCard
        action={{
          kind: "workflow_starter_input", title: "Upload workflow materials", options: ["run", "cancel"], inputs: [
            { key: "note", label: "Note", type: "string", required: true },
            { key: "cover", label: "Cover image", type: "string", required: true, schema: { type: "string", "x-ui": { control: "knowledge_file", file_kind: "image" } } },
            { key: "materials", label: "Materials", type: "json", required: true, schema: {
              type: "object", required: ["sources"], properties: {
                sources: { type: "array", title: "Source files", items: { type: "object" }, "x-ui": { control: "documents" } },
              },
            } },
          ],
        } as any}
        workspaceId={workspaceId}
        onResolve={(choice, _note, payload) => { submitted = { choice, payload }; }}
      />
    </main>;
  }
  return <main style={{ maxWidth: 720, padding: 24, margin: "auto" }}>
    <p>Isolated Workflow input test</p>
    <nav aria-label="Test workflows">{actions.map((action) => <button key={action.key} onClick={() => setSelected(action)}>{action.title}</button>)}</nav>
    <WorkspaceAppActionFlow key={selected.key} action={selected} workspaceId={workspaceId}
      canStart={!params.has("readonly")} chatAvailable={!params.has("nochat")}
      onUseChat={() => setChatRequests((count) => count + 1)} onBack={() => setSelected(actions[0])} />
    <p>Chat requests: {chatRequests}</p>
    <button onClick={() => document.getElementById("submitted")!.textContent = JSON.stringify(submitted)}>Show submitted request</button>
    <pre id="submitted" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }} />
  </main>;
}
createRoot(document.getElementById("root")!).render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Fixture /></QueryClientProvider></MemoryRouter>);
