import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";

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

interface UseCodeProjectWorkspaceOptions {
  enabled: boolean;
  mainPath: string | null | undefined;
  mainName: string;
  mainContent: string;
  mainSaveStatus: Exclude<CodeWorkspaceSaveStatus, "error">;
  onMainContentChange: (content: string) => void;
  onSaveMain: (content: string) => Promise<boolean>;
}

const AUXILIARY_AUTOSAVE_DELAY = 3000;

function readErrorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "Unable to open this file";
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
}: UseCodeProjectWorkspaceOptions) {
  const normalizedMainPath = enabled && mainPath ? mainPath.replace(/^\/+/, "") : null;
  const [activePath, setActivePath] = useState<string | null>(normalizedMainPath);
  const [auxiliaryOrder, setAuxiliaryOrder] = useState<string[]>([]);
  const [auxiliaryTabs, setAuxiliaryTabs] = useState<Record<string, AuxiliaryCodeWorkspaceTab>>({});
  const auxiliaryTabsRef = useRef(auxiliaryTabs);
  const saveTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const generationRef = useRef(0);

  useEffect(() => {
    auxiliaryTabsRef.current = auxiliaryTabs;
  }, [auxiliaryTabs]);

  useEffect(() => {
    generationRef.current += 1;
    saveTimersRef.current.forEach((timer) => clearTimeout(timer));
    saveTimersRef.current.clear();
    setAuxiliaryOrder([]);
    setAuxiliaryTabs({});
    setActivePath(normalizedMainPath);
  }, [normalizedMainPath]);

  useEffect(() => () => {
    generationRef.current += 1;
    saveTimersRef.current.forEach((timer) => clearTimeout(timer));
    saveTimersRef.current.clear();
  }, []);

  const saveAuxiliaryFile = useCallback(async (path: string, explicitContent?: string): Promise<boolean> => {
    const tab = auxiliaryTabsRef.current[path];
    if (!tab || tab.loading || tab.readError) return false;
    const contentToSave = explicitContent ?? tab.content;
    const timer = saveTimersRef.current.get(path);
    if (timer) clearTimeout(timer);
    saveTimersRef.current.delete(path);
    setAuxiliaryTabs((current) => ({
      ...current,
      [path]: { ...current[path], status: "saving", error: null },
    }));
    try {
      await api.fs.write(path, contentToSave);
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
      return true;
    } catch (error) {
      setAuxiliaryTabs((current) => {
        const currentTab = current[path];
        if (!currentTab) return current;
        return {
          ...current,
          [path]: { ...currentTab, status: "error", error: readErrorMessage(error) },
        };
      });
      return false;
    }
  }, []);

  const scheduleAuxiliarySave = useCallback((path: string, contentToSave: string) => {
    const existing = saveTimersRef.current.get(path);
    if (existing) clearTimeout(existing);
    const timer = setTimeout(() => {
      saveTimersRef.current.delete(path);
      void saveAuxiliaryFile(path, contentToSave);
    }, AUXILIARY_AUTOSAVE_DELAY);
    saveTimersRef.current.set(path, timer);
  }, [saveAuxiliaryFile]);

  const openFile = useCallback(async (file: CodeWorkspaceFileReference) => {
    const path = file.path.replace(/^\/+/, "");
    if (path === normalizedMainPath) {
      setActivePath(path);
      return;
    }
    const existing = auxiliaryTabsRef.current[path];
    if (existing && !existing.error) {
      setActivePath(path);
      return;
    }

    const generation = generationRef.current;
    setAuxiliaryOrder((current) => current.includes(path) ? current : [...current, path]);
    setAuxiliaryTabs((current) => ({
      ...current,
      [path]: {
        path,
        name: file.name,
        content: current[path]?.content || "",
        status: "saved",
        loading: true,
        error: null,
        readError: false,
      },
    }));
    setActivePath(path);

    try {
      const result = await api.fs.read(path);
      if (!/^utf-?8$/i.test(result.encoding || "utf-8")) {
        throw new Error("This file is not editable as text");
      }
      if (generation !== generationRef.current) return;
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
      if (generation !== generationRef.current) return;
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
    const timer = saveTimersRef.current.get(path);
    if (timer) clearTimeout(timer);
    saveTimersRef.current.delete(path);
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
    saveActive,
    saveAll,
    previewTextOverrides,
    hasPendingWrites,
  };
}
