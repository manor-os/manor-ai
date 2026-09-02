import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import {
  AUTH_TOKEN_CHANGED_EVENT,
  authEntityKey,
  authPrincipalKey,
  getAuthToken,
} from "./authToken";
import { enqueueAuxiliarySave } from "./auxiliarySaveQueue";
import { useAuthStore } from "../stores/auth";

export type CodeWorkspaceSaveStatus = "saved" | "saving" | "unsaved" | "error";

export interface CodeWorkspaceFileReference {
  path: string;
  name: string;
  mimeType?: string | null;
}

export interface CodeWorkspaceTab {
  path: string;
  name: string;
  content: string;
  status: CodeWorkspaceSaveStatus;
  loading: boolean;
  error: string | null;
  readError: boolean;
  isMain: boolean;
}

type AuxiliaryCodeWorkspaceTab = Omit<CodeWorkspaceTab, "isMain">;

type AuxiliaryAuthContext = {
  authToken: string | null;
  entityKey: string;
  principalKey: string;
};

type PendingAuxiliarySave = {
  authContext: AuxiliaryAuthContext;
  content: string;
  timer: ReturnType<typeof setTimeout>;
  flush: () => void;
};

interface UseCodeProjectWorkspaceOptions {
  enabled: boolean;
  mainPath: string | null | undefined;
  mainName: string;
  mainContent: string;
  mainSaveStatus: Exclude<CodeWorkspaceSaveStatus, "error">;
  onMainContentChange: (content: string) => void;
  onSaveMain: (content: string) => Promise<boolean>;
  onAuxiliarySaveError: (path: string, message: string) => void;
}

const AUXILIARY_AUTOSAVE_DELAY = 3000;
const AUXILIARY_SAVE_STALL_TIMEOUT = 30_000;
const AUXILIARY_SAVE_STALL_MESSAGE = "Saving timed out. Your changes are still unsaved; try again.";
const AUXILIARY_AUTH_CHANGED_MESSAGE = "Your account or workspace changed. Reopen this file before saving.";

function readErrorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "Unable to open this file";
}

function captureAuxiliaryAuthContext(): AuxiliaryAuthContext {
  const authToken = getAuthToken();
  return {
    authToken,
    entityKey: authEntityKey(authToken),
    principalKey: authPrincipalKey(authToken),
  };
}

function refreshAuxiliaryAuthContext(boundContext: AuxiliaryAuthContext): AuxiliaryAuthContext | null {
  const currentContext = captureAuxiliaryAuthContext();
  return currentContext.principalKey === boundContext.principalKey ? currentContext : null;
}

export function codeProjectDirectory(path: string | null | undefined): string | null {
  if (!path) return null;
  const normalized = path.replace(/^\/+|\/+$/g, "");
  const separator = normalized.lastIndexOf("/");
  return separator >= 0 ? normalized.slice(0, separator) || "." : ".";
}

export function codeProjectName(path: string | null | undefined): string {
  if (!path || path === ".") return "Project";
  const normalized = path.replace(/\/+$/g, "");
  return normalized.slice(normalized.lastIndexOf("/") + 1) || "Project";
}

export function useCodeProjectWorkspace({
  enabled,
  mainPath,
  mainName,
  mainContent,
  mainSaveStatus,
  onMainContentChange,
  onSaveMain,
  onAuxiliarySaveError,
}: UseCodeProjectWorkspaceOptions) {
  const normalizedMainPath = enabled && mainPath ? mainPath.replace(/^\/+/, "") : null;
  const [activePath, setActivePath] = useState<string | null>(normalizedMainPath);
  const [auxiliaryOrder, setAuxiliaryOrder] = useState<string[]>([]);
  const [auxiliaryTabs, setAuxiliaryTabs] = useState<Record<string, AuxiliaryCodeWorkspaceTab>>({});
  const auxiliaryTabsRef = useRef(auxiliaryTabs);
  const auxiliaryAuthContextsRef = useRef<Map<string, AuxiliaryAuthContext>>(new Map());
  const saveTimersRef = useRef<Map<string, PendingAuxiliarySave>>(new Map());
  const generationRef = useRef(0);
  useAuthStore((state) => state.token);
  const [, setAuthStorageRevision] = useState(0);
  const activeAuthPrincipalKey = authPrincipalKey(getAuthToken());

  useEffect(() => {
    const refreshAuthScope = () => {
      setAuthStorageRevision((revision) => revision + 1);
    };
    window.addEventListener("storage", refreshAuthScope);
    window.addEventListener("focus", refreshAuthScope);
    window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, refreshAuthScope);
    return () => {
      window.removeEventListener("storage", refreshAuthScope);
      window.removeEventListener("focus", refreshAuthScope);
      window.removeEventListener(AUTH_TOKEN_CHANGED_EVENT, refreshAuthScope);
    };
  }, []);

  const flushPendingAuxiliarySaves = useCallback(() => {
    saveTimersRef.current.forEach((pending, path) => {
      clearTimeout(pending.timer);
      if (!refreshAuxiliaryAuthContext(pending.authContext)) {
        onAuxiliarySaveError(path, AUXILIARY_AUTH_CHANGED_MESSAGE);
        return;
      }
      pending.flush();
    });
    saveTimersRef.current.clear();
  }, [onAuxiliarySaveError]);

  useEffect(() => {
    auxiliaryTabsRef.current = auxiliaryTabs;
  }, [auxiliaryTabs]);

  useEffect(() => {
    flushPendingAuxiliarySaves();
    auxiliaryAuthContextsRef.current.clear();
    generationRef.current += 1;
    setAuxiliaryOrder([]);
    setAuxiliaryTabs({});
    setActivePath(normalizedMainPath);
  }, [activeAuthPrincipalKey, flushPendingAuxiliarySaves, normalizedMainPath]);

  useEffect(() => () => {
    flushPendingAuxiliarySaves();
    auxiliaryAuthContextsRef.current.clear();
    generationRef.current += 1;
  }, [flushPendingAuxiliarySaves]);

  const saveAuxiliaryFile = useCallback(async (
    path: string,
    explicitContent?: string,
    explicitAuthContext?: AuxiliaryAuthContext,
  ): Promise<boolean> => {
    const tab = auxiliaryTabsRef.current[path];
    if (!tab || tab.loading || tab.readError) return false;
    const pending = saveTimersRef.current.get(path);
    const contentToSave = explicitContent ?? pending?.content ?? tab.content;
    const boundAuthContext = explicitAuthContext
      ?? pending?.authContext
      ?? auxiliaryAuthContextsRef.current.get(path)
      ?? captureAuxiliaryAuthContext();
    const generation = generationRef.current;
    if (pending) clearTimeout(pending.timer);
    saveTimersRef.current.delete(path);
    const reportSaveError = (message: string) => {
      if (generation === generationRef.current) {
        setAuxiliaryTabs((current) => {
          const currentTab = current[path];
          if (!currentTab) return current;
          return {
            ...current,
            [path]: { ...currentTab, status: "error", error: message },
          };
        });
      } else {
        onAuxiliarySaveError(path, message);
      }
    };
    const authContext = refreshAuxiliaryAuthContext(boundAuthContext);
    if (!authContext) {
      onAuxiliarySaveError(path, AUXILIARY_AUTH_CHANGED_MESSAGE);
      return false;
    }
    return enqueueAuxiliarySave(authContext.entityKey, path, async ({ intent, isLatest, signal }) => {
      if (generation === generationRef.current && isLatest()) {
        setAuxiliaryTabs((current) => {
          const currentTab = current[path];
          if (!currentTab) return current;
          return {
            ...current,
            [path]: { ...currentTab, status: "saving", error: null },
          };
        });
      }
      try {
        await api.fs.write(path, contentToSave, {
          authToken: authContext.authToken,
          saveIntent: intent,
          signal,
        });
        if (signal.aborted) return false;
        if (generation === generationRef.current && isLatest()) {
          setAuxiliaryTabs((current) => {
            const currentTab = current[path];
            if (!currentTab) return current;
            return {
              ...current,
              [path]: {
                ...currentTab,
                status: currentTab.content === contentToSave ? "saved" : "unsaved",
                error: null,
              },
            };
          });
        }
        return true;
      } catch (error) {
        if (!signal.aborted && isLatest()) reportSaveError(readErrorMessage(error));
        return false;
      }
    }, {
      timeoutMs: AUXILIARY_SAVE_STALL_TIMEOUT,
      onTimeout: () => reportSaveError(AUXILIARY_SAVE_STALL_MESSAGE),
    });
  }, [onAuxiliarySaveError]);

  const scheduleAuxiliarySave = useCallback((path: string, contentToSave: string) => {
    const existing = saveTimersRef.current.get(path);
    if (existing) clearTimeout(existing.timer);
    const boundAuthContext = auxiliaryAuthContextsRef.current.get(path)
      ?? captureAuxiliaryAuthContext();
    const authContext = refreshAuxiliaryAuthContext(boundAuthContext) ?? boundAuthContext;
    let pending: PendingAuxiliarySave;
    const flush = () => {
      if (saveTimersRef.current.get(path) === pending) saveTimersRef.current.delete(path);
      void saveAuxiliaryFile(path, contentToSave, authContext);
    };
    pending = {
      authContext,
      content: contentToSave,
      timer: setTimeout(flush, AUXILIARY_AUTOSAVE_DELAY),
      flush,
    };
    saveTimersRef.current.set(path, pending);
  }, [saveAuxiliaryFile]);

  const openFile = useCallback(async (file: CodeWorkspaceFileReference) => {
    const path = file.path.replace(/^\/+/, "");
    if (path === normalizedMainPath) {
      setActivePath(path);
      return;
    }
    const existing = auxiliaryTabsRef.current[path];
    const existingAuthContext = auxiliaryAuthContextsRef.current.get(path);
    if (
      existing
      && !existing.readError
      && existingAuthContext
      && refreshAuxiliaryAuthContext(existingAuthContext)
    ) {
      setActivePath(path);
      return;
    }

    const generation = generationRef.current;
    const authContext = captureAuxiliaryAuthContext();
    auxiliaryAuthContextsRef.current.set(path, authContext);
    setAuxiliaryOrder((current) => current.includes(path) ? current : [...current, path]);
    setAuxiliaryTabs((current) => ({
      ...current,
      [path]: {
        path,
        name: file.name,
        content: existingAuthContext && refreshAuxiliaryAuthContext(existingAuthContext)
          ? current[path]?.content || ""
          : "",
        status: "saved",
        loading: true,
        error: null,
        readError: false,
      },
    }));
    setActivePath(path);

    try {
      const result = await api.fs.read(path, authContext.authToken);
      if (!/^utf-?8$/i.test(result.encoding || "utf-8")) {
        throw new Error("This file is not editable as text");
      }
      if (generation !== generationRef.current || !refreshAuxiliaryAuthContext(authContext)) return;
      setAuxiliaryTabs((current) => ({
        ...current,
        [path]: {
          path,
          name: file.name,
          content: result.content,
          status: "saved",
          loading: false,
          error: null,
          readError: false,
        },
      }));
    } catch (error) {
      if (generation !== generationRef.current || !refreshAuxiliaryAuthContext(authContext)) return;
      setAuxiliaryTabs((current) => ({
        ...current,
        [path]: {
          ...(current[path] || {
            path,
            name: file.name,
            content: "",
            status: "error" as const,
            readError: true,
          }),
          loading: false,
          status: "error",
          error: readErrorMessage(error),
          readError: true,
        },
      }));
    }
  }, [normalizedMainPath]);

  const changeActiveContent = useCallback((nextContent: string) => {
    if (!activePath || activePath === normalizedMainPath) {
      onMainContentChange(nextContent);
      return;
    }
    setAuxiliaryTabs((current) => {
      const tab = current[activePath];
      if (!tab || tab.loading) return current;
      return {
        ...current,
        [activePath]: { ...tab, content: nextContent, status: "unsaved", error: null, readError: false },
      };
    });
    scheduleAuxiliarySave(activePath, nextContent);
  }, [activePath, normalizedMainPath, onMainContentChange, scheduleAuxiliarySave]);

  const fileContent = useCallback((path: string) => {
    if (path === normalizedMainPath) return mainContent;
    return auxiliaryTabsRef.current[path]?.content;
  }, [mainContent, normalizedMainPath]);

  const previewFileContent = useCallback((path: string, nextContent: string) => {
    if (path === normalizedMainPath) return false;
    const currentTab = auxiliaryTabsRef.current[path];
    if (!currentTab || currentTab.loading || currentTab.readError) return false;
    const nextTabs = {
      ...auxiliaryTabsRef.current,
      [path]: { ...currentTab, content: nextContent },
    };
    auxiliaryTabsRef.current = nextTabs;
    setAuxiliaryTabs(nextTabs);
    return true;
  }, [normalizedMainPath]);

  const commitFileContent = useCallback((path: string, nextContent: string) => {
    if (path === normalizedMainPath) return false;
    const currentTab = auxiliaryTabsRef.current[path];
    if (!currentTab || currentTab.loading || currentTab.readError) return false;
    const nextTabs = {
      ...auxiliaryTabsRef.current,
      [path]: {
        ...currentTab,
        content: nextContent,
        status: "unsaved" as const,
        error: null,
      },
    };
    auxiliaryTabsRef.current = nextTabs;
    setAuxiliaryTabs(nextTabs);
    scheduleAuxiliarySave(path, nextContent);
    return true;
  }, [normalizedMainPath, scheduleAuxiliarySave]);

  const saveActive = useCallback(async (): Promise<boolean> => {
    if (!activePath || activePath === normalizedMainPath) return onSaveMain(mainContent);
    return saveAuxiliaryFile(activePath);
  }, [activePath, mainContent, normalizedMainPath, onSaveMain, saveAuxiliaryFile]);

  const saveAll = useCallback(async (): Promise<boolean> => {
    const auxiliaryPaths = Object.values(auxiliaryTabsRef.current)
      .filter((tab) => !tab.readError && tab.status !== "saved")
      .map((tab) => tab.path);
    const results = await Promise.all([
      onSaveMain(mainContent),
      ...auxiliaryPaths.map((path) => saveAuxiliaryFile(path)),
    ]);
    return results.every(Boolean);
  }, [mainContent, onSaveMain, saveAuxiliaryFile]);

  const closeFile = useCallback(async (path: string) => {
    if (path === normalizedMainPath) return;
    const tab = auxiliaryTabsRef.current[path];
    if (tab && !tab.readError && tab.status !== "saved") {
      const saved = await saveAuxiliaryFile(path);
      if (!saved) return;
    }
    const pending = saveTimersRef.current.get(path);
    if (pending) clearTimeout(pending.timer);
    saveTimersRef.current.delete(path);
    auxiliaryAuthContextsRef.current.delete(path);
    setAuxiliaryTabs((current) => {
      const next = { ...current };
      delete next[path];
      return next;
    });
    setAuxiliaryOrder((current) => {
      const index = current.indexOf(path);
      const next = current.filter((item) => item !== path);
      if (activePath === path) setActivePath(next[Math.max(0, index - 1)] || normalizedMainPath);
      return next;
    });
  }, [activePath, normalizedMainPath, saveAuxiliaryFile]);

  const tabs = useMemo<CodeWorkspaceTab[]>(() => {
    const mainTab: CodeWorkspaceTab[] = normalizedMainPath ? [{
      path: normalizedMainPath,
      name: mainName,
      content: mainContent,
      status: mainSaveStatus,
      loading: false,
      error: null,
      readError: false,
      isMain: true,
    }] : [];
    return [
      ...mainTab,
      ...auxiliaryOrder.flatMap((path) => auxiliaryTabs[path]
        ? [{ ...auxiliaryTabs[path], isMain: false }]
        : []),
    ];
  }, [auxiliaryOrder, auxiliaryTabs, mainContent, mainName, mainSaveStatus, normalizedMainPath]);

  const activeTab = tabs.find((tab) => tab.path === activePath) || tabs[0] || null;
  const previewTextOverrides = useMemo(() => Object.fromEntries(
    auxiliaryOrder.flatMap((path) => {
      const tab = auxiliaryTabs[path];
      return tab && !tab.loading && !tab.readError ? [[path, tab.content]] : [];
    }),
  ), [auxiliaryOrder, auxiliaryTabs]);
  const hasPendingWrites = auxiliaryOrder.some((path) => {
    const tab = auxiliaryTabs[path];
    if (tab?.readError) return false;
    const status = tab?.status;
    return status === "saving" || status === "unsaved" || status === "error";
  });

  return {
    activePath,
    activeTab,
    tabs,
    setActivePath,
    openFile,
    closeFile,
    changeActiveContent,
    fileContent,
    previewFileContent,
    commitFileContent,
    saveActive,
    saveAll,
    previewTextOverrides,
    hasPendingWrites,
  };
}
