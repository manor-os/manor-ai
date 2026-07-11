import {
  DIAGRAM_VERSION,
  cloneDiagramDocument,
  createDiagramId,
  getElementBounds,
  type DiagramConnectorElement,
  type DiagramConnectorRouting,
  type DiagramElement,
  type DiagramShapeElement,
  type DiagramTextElement,
  type EditableDiagramDocument,
} from "./schema";

const DEFAULT_LAYER_LABELS = [
  "Fuzzification Layer",
  "Spatial Firing Layer",
  "Normalize Layer",
  "Defuzzification Layer",
];

export function generateDiagramDraftFromPrompt(prompt: string): EditableDiagramDocument {
  const lowered = prompt.toLowerCase();
  if (/circuit|schematic|electronics|电路|电子/.test(lowered)) return circuitDiagram(prompt);
  const agentArchitecture = /agent|智能体|代理|multi-agent|multi agent/.test(lowered);
  const scientific = !agentArchitecture && /paper|论文|fuzzy|kalman|neural|layer|system|architecture|网络|系统|结构|架构/.test(lowered);
  const layerLabels = extractLayerLabels(prompt);
  return scientific ? layeredScientificDiagram(prompt, layerLabels) : genericFlowDiagram(prompt);
}

export function adjustDiagramDraftFromPrompt(current: EditableDiagramDocument, prompt: string): EditableDiagramDocument {
  const instruction = prompt.trim();
  if (!instruction) return cloneDiagramDocument(current);
  if (!current.elements.length) return generateDiagramDraftFromPrompt(instruction);
  if (isReplacementDiagramRequest(instruction)) return generateDiagramDraftFromPrompt(instruction);

  let next = cloneDiagramDocument(current);
  next.prompt = instruction;

  const before = diagramVisibleSignature(next);
  const addedLabels = extractAddLabels(instruction);
  const flowLabels = extractFlowLabels(instruction);
  const labelsToEnsure = addedLabels.length ? addedLabels : flowLabels;

  if (labelsToEnsure.length) {
    ensureNodes(next, labelsToEnsure);
  } else if (isFullDiagramRequest(instruction) && !hasExplicitDiagramEdit(instruction)) {
    rewriteCurrentDiagramForTopic(next, instruction);
  } else if (isFullDiagramRequest(instruction)) {
    mergeGeneratedTopic(next, generateDiagramDraftFromPrompt(instruction));
  }

  applyRenameInstruction(next, instruction);
  applyDeleteInstruction(next, instruction);
  applyColorInstruction(next, instruction);

  const shouldConnect = /connect|connector|line|arrow|flow|连线|连接|箭头|流程|到|至|->|→/.test(instruction.toLowerCase());
  if (flowLabels.length >= 2) {
    connectSequence(next, flowLabels);
  } else if (shouldConnect && labelsToEnsure.length >= 2) {
    connectSequence(next, labelsToEnsure);
  } else if (shouldConnect && labelsToEnsure.length === 1 && !applyConnectionInstruction(next, instruction, labelsToEnsure[0])) {
    connectLastShapeTo(next, labelsToEnsure[0]);
  } else if (shouldConnect) {
    applyConnectionInstruction(next, instruction);
  }

  applyRoutingInstruction(next, instruction);

  if (/layout|align|tidy|arrange|horizontal|vertical|整理|布局|排列|对齐|水平|横向|竖直|纵向/.test(instruction.toLowerCase())) {
    layoutDiagram(next, /vertical|竖直|纵向|上下/.test(instruction.toLowerCase()) ? "vertical" : "horizontal");
  }

  if (diagramVisibleSignature(next) === before) return current;
  return next;
}

function diagramVisibleSignature(document: EditableDiagramDocument): string {
  return JSON.stringify({
    title: document.title,
    canvas: document.canvas,
    elements: document.elements,
    groups: document.groups || [],
    constraints: document.constraints || [],
  });
}

function extractLayerLabels(prompt: string): string[] {
  const quoted = Array.from(prompt.matchAll(/["“]([^"”]{2,40})["”]/g)).map((match) => match[1].trim());
  if (quoted.length >= 2) return quoted.slice(0, 6);
  const layerMentions = Array.from(prompt.matchAll(/([A-Za-z][A-Za-z\s-]{2,32} Layer)/g)).map((match) => match[1].trim());
  if (layerMentions.length >= 2) return layerMentions.slice(0, 6);
  return DEFAULT_LAYER_LABELS;
}

function extractQuotedLabels(prompt: string): string[] {
  return Array.from(prompt.matchAll(/["“'`]([^"”'`]{2,48})["”'`]/g))
    .map((match) => cleanLabel(match[1]))
    .filter(Boolean);
}

function extractFlowLabels(prompt: string): string[] {
  const quoted = extractQuotedLabels(prompt);
  if (quoted.length >= 2 && /->|→|>|到|至|connect|连接|连线|flow|流程/i.test(prompt)) return uniqueLabels(quoted);
  if (!/->|→|>|=>|,|，|;|；|\n/.test(prompt)) return [];
  const parts = prompt
    .replace(/(?:connect|连接|连线|流程|flow|pipeline|with|和|以及)/gi, " ")
    .split(/(?:->|→|=>|>|,|，|;|；|\n)/)
    .map(cleanLabel)
    .filter((label) => label.length >= 2 && label.length <= 42);
  return parts.length >= 2 ? uniqueLabels(parts).slice(0, 10) : [];
}

function extractAddLabels(prompt: string): string[] {
  const quoted = extractQuotedLabels(prompt);
  if (quoted.length && /add|create|insert|append|新增|添加|加入|插入|加一个|加上/.test(prompt.toLowerCase())) {
    return uniqueLabels(quoted).slice(0, 8);
  }
  const match = prompt.match(/(?:add|create|insert|append|新增|添加|加入|插入|加一个|加上)\s*([^。.\n]+)/i);
  if (!match) return [];
  const addOnly = match[1].split(/(?:and\s+)?(?:connect|link|连接|连线|连到|连接到)\s*(?:to|到|至)?/i)[0];
  const labels = addOnly
    .split(/,|，|;|；|和|以及|and|with|\+/i)
    .map(cleanLabel)
    .filter((label) => label.length >= 2 && label.length <= 42);
  return uniqueLabels(labels).slice(0, 8);
}

function cleanLabel(raw: string): string {
  return raw
    .replace(/^(?:a|an|the|一个|一份|新的|new)\s+/i, "")
    .replace(/(?:node|节点|模块|module|box|框|shape|图形|element|元素|diagram|图|设计图)$/i, "")
    .replace(/^(?:node|节点|模块|module|box|框)\s+/i, "")
    .replace(/\s+/g, " ")
    .trim();
}

function uniqueLabels(labels: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  labels.forEach((label) => {
    const key = normalizeLabel(label);
    if (!key || seen.has(key)) return;
    seen.add(key);
    result.push(label);
  });
  return result;
}

function normalizeLabel(label: string): string {
  return label.toLowerCase().replace(/[\s_-]+/g, "").trim();
}

function elementText(element: DiagramShapeElement | DiagramTextElement): string {
  return element.kind === "shape" ? element.text || element.name || "" : element.text || "";
}

function editableObjects(document: EditableDiagramDocument): Array<DiagramShapeElement | DiagramTextElement> {
  return document.elements.filter((element): element is DiagramShapeElement | DiagramTextElement => element.kind !== "connector");
}

function flowObjects(document: EditableDiagramDocument): Array<DiagramShapeElement | DiagramTextElement> {
  return editableObjects(document).filter((element) => {
    const text = elementText(element);
    const isLargeTitle = element.kind === "text" && element.w > 420 && element.h <= 64 && element.y < 120;
    return Boolean(text.trim()) && !isLargeTitle;
  });
}

function findObjectByLabel(document: EditableDiagramDocument, label: string): DiagramShapeElement | DiagramTextElement | undefined {
  const target = normalizeLabel(label);
  return editableObjects(document).find((element) => {
    const current = normalizeLabel(elementText(element));
    return current === target || current.includes(target) || target.includes(current);
  });
}

function findObjectForConnection(document: EditableDiagramDocument, label: string): DiagramShapeElement | DiagramTextElement | undefined {
  const direct = findObjectByLabel(document, label);
  if (direct) return direct;
  const normalized = normalizeLabel(label);
  const objects = flowObjects(document);
  if (/^(output|response|result|输出|结果)/.test(normalized)) {
    return objects.find((element) => element.id.startsWith("output")) || objects[objects.length - 1];
  }
  if (/^(input|source|start|输入|开始)/.test(normalized)) {
    return objects.find((element) => element.id.startsWith("input")) || objects[0];
  }
  return undefined;
}

function ensureNodes(document: EditableDiagramDocument, labels: string[]): string[] {
  const existingOrCreatedIds: string[] = [];
  const bounds = diagramObjectBounds(document);
  const startX = bounds ? bounds.x + bounds.w + 90 : 120;
  const startY = bounds ? Math.max(bounds.y, 180) : 280;
  labels.forEach((label, index) => {
    const existing = findObjectByLabel(document, label);
    if (existing) {
      existingOrCreatedIds.push(existing.id);
      return;
    }
    const node: DiagramShapeElement = {
      id: createDiagramId("node"),
      kind: "shape",
      shape: "roundRect",
      x: startX + (index % 3) * 230,
      y: startY + Math.floor(index / 3) * 130,
      w: 178,
      h: 84,
      fill: index % 2 === 0 ? "#e8eff4" : "#f3ecd6",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: label,
      textStyle: { fontSize: 19, fontWeight: 700, color: "#1c1917", align: "center" },
    };
    document.elements.push(node);
    existingOrCreatedIds.push(node.id);
  });
  return existingOrCreatedIds;
}

function connectSequence(document: EditableDiagramDocument, labels: string[]): void {
  const ids = labels.map((label) => findObjectForConnection(document, label)?.id).filter(Boolean) as string[];
  ids.slice(0, -1).forEach((from, index) => ensureConnector(document, from, ids[index + 1]));
}

function connectLastShapeTo(document: EditableDiagramDocument, label: string): void {
  const target = findObjectByLabel(document, label);
  if (!target) return;
  const objects = flowObjects(document).filter((element) => element.id !== target.id);
  const source = objects[objects.length - 1];
  if (source) ensureConnector(document, source.id, target.id);
}

function applyConnectionInstruction(document: EditableDiagramDocument, prompt: string, fallbackSourceLabel?: string): boolean {
  const direct = prompt.match(/(?:connect|link|连接|连线)\s+["“']?([^"”'，,。.\n]{2,42})["”']?\s*(?:to|->|→|到|至)\s*["“']?([^"”'，,。.\n]{2,42})["”']?/i);
  if (direct) {
    const source = findObjectForConnection(document, cleanLabel(direct[1]));
    const target = findObjectForConnection(document, cleanLabel(direct[2]));
    if (source && target) {
      ensureConnector(document, source.id, target.id);
      return true;
    }
  }
  if (!fallbackSourceLabel) return false;
  const targetOnly = prompt.match(/(?:connect|link|连接|连线|连到|连接到)\s*(?:to|到|至)?\s*["“']?([^"”'，,。.\n]{2,42})["”']?/i);
  if (!targetOnly) return false;
  const source = findObjectForConnection(document, fallbackSourceLabel);
  const target = findObjectForConnection(document, cleanLabel(targetOnly[1]));
  if (!source || !target) return false;
  ensureConnector(document, source.id, target.id);
  return true;
}

function ensureConnector(document: EditableDiagramDocument, from: string, to: string): void {
  if (from === to) return;
  const exists = document.elements.some((element) => (
    element.kind === "connector"
    && element.from.bind?.elementId === from
    && element.to.bind?.elementId === to
  ));
  if (exists) return;
  document.elements.push(connector(from, to, "right", "left"));
}

function applyRenameInstruction(document: EditableDiagramDocument, prompt: string): void {
  const renameMatch = prompt.match(/(?:rename|重命名|把|将)\s*["“']?([^"”'，,。.\n]{2,42})["”']?\s*(?:to|as|改成|改为|改名为|叫做)\s*["“']?([^"”'，,。.\n]{2,42})["”']?/i);
  if (!renameMatch) return;
  const target = findObjectByLabel(document, cleanLabel(renameMatch[1]));
  if (!target) return;
  const nextLabel = cleanLabel(renameMatch[2]);
  if (target.kind === "shape") target.text = nextLabel;
  else target.text = nextLabel;
}

function applyDeleteInstruction(document: EditableDiagramDocument, prompt: string): void {
  if (!/delete|remove|删除|移除|去掉/.test(prompt.toLowerCase())) return;
  const quoted = extractQuotedLabels(prompt);
  const labels = quoted.length ? quoted : prompt
    .replace(/delete|remove|删除|移除|去掉/gi, "")
    .split(/,|，|;|；|和|以及|and/i)
    .map(cleanLabel)
    .filter((label) => label.length >= 2 && label.length <= 42);
  const ids = new Set(labels.map((label) => findObjectByLabel(document, label)?.id).filter(Boolean) as string[]);
  if (!ids.size) return;
  document.elements = document.elements.filter((element) => {
    if (ids.has(element.id)) return false;
    if (element.kind !== "connector") return true;
    return !ids.has(element.from.bind?.elementId || "") && !ids.has(element.to.bind?.elementId || "");
  });
}

function applyRoutingInstruction(document: EditableDiagramDocument, prompt: string): void {
  const lowered = prompt.toLowerCase();
  let routing: DiagramConnectorRouting | null = null;
  if (/curve|curved|曲线|弧线/.test(lowered)) routing = "curve";
  else if (/orthogonal|正交/.test(lowered)) routing = "orthogonal";
  else if (/elbow|折线|拐角/.test(lowered)) routing = "elbow";
  else if (/straight|直线/.test(lowered)) routing = "straight";
  if (!routing) return;
  document.elements = document.elements.map((element) => element.kind === "connector" ? { ...element, routing } : element);
}

function applyColorInstruction(document: EditableDiagramDocument, prompt: string): void {
  const lowered = prompt.toLowerCase();
  const color = [
    [/blue|蓝色/, "#e3e9f1"],
    [/green|绿色/, "#e4efe8"],
    [/yellow|黄色/, "#f3ecd6"],
    [/orange|橙色/, "#ecdac2"],
    [/red|红色/, "#f1dddb"],
    [/purple|紫色/, "#ece9f5"],
    [/gray|grey|灰色/, "#e7e5e4"],
    [/white|白色/, "#ffffff"],
  ].find(([pattern]) => (pattern as RegExp).test(lowered))?.[1] as string | undefined;
  if (!color || !/color|fill|颜色|填充|变成/.test(lowered)) return;
  document.elements = document.elements.map((element) => {
    if (element.kind === "shape") return { ...element, fill: color };
    return element;
  });
}

function layoutDiagram(document: EditableDiagramDocument, direction: "horizontal" | "vertical"): void {
  const objects = flowObjects(document);
  if (!objects.length) return;
  const bounds = diagramObjectBounds(document);
  const startX = bounds ? bounds.x : 120;
  const startY = bounds ? Math.max(bounds.y, 180) : 260;
  document.elements = document.elements.map((element) => {
    const index = objects.findIndex((object) => object.id === element.id);
    if (index < 0 || element.kind === "connector") return element;
    return {
      ...element,
      x: direction === "horizontal" ? startX + index * 240 : startX,
      y: direction === "horizontal" ? startY : startY + index * 130,
    };
  });
}

function hasExplicitDiagramEdit(prompt: string): boolean {
  return /add|create|insert|append|新增|添加|加入|插入|加一个|加上|delete|remove|删除|移除|去掉|rename|重命名|改成|改为|connect|connector|line|arrow|连线|连接|箭头|layout|align|tidy|arrange|整理|布局|排列|对齐|curve|curved|曲线|elbow|折线|orthogonal|正交|straight|直线|color|fill|颜色|填充/.test(prompt.toLowerCase());
}

function isReplacementDiagramRequest(prompt: string): boolean {
  const lowered = prompt.toLowerCase();
  return /改成|改为|换成|变成|转换成|change\s+(?:it\s+)?to|convert\s+(?:it\s+)?to/.test(lowered)
    && /diagram|flow|architecture|circuit|schematic|图|流程|架构|电路/.test(lowered);
}

function isFullDiagramRequest(prompt: string): boolean {
  return /diagram|flow|architecture|系统|结构|架构|流程|设计图|画|生成|draw|create/i.test(prompt);
}

function rewriteCurrentDiagramForTopic(document: EditableDiagramDocument, prompt: string): void {
  const labels = inferGenericFlowLabels(prompt);
  if (!labels.length) return;

  document.title = conciseTitle(prompt);
  const bounds = diagramObjectBounds(document) || {
    x: document.canvas.originX ?? 0,
    y: document.canvas.originY ?? 0,
    w: Math.min(900, document.canvas.width),
    h: Math.min(520, document.canvas.height),
  };
  const title = ensureTitleElement(document, prompt, bounds);
  const slots = ensureTopicSlots(document, labels, bounds, title);
  const agentTopic = /agent|智能体|代理|multi-agent|multi agent|架构/.test(prompt.toLowerCase());
  const positions = agentTopic
    ? agentArchitecturePositions(bounds, title.y + title.h + 72)
    : defaultTopicPositions(bounds, title.y + title.h + 80, labels.length);

  slots.forEach((slot, index) => {
    const position = positions[index] || positions[positions.length - 1] || { x: bounds.x + index * 220, y: bounds.y + 120 };
    const shape = topicShapeForLabel(labels[index]);
    Object.assign(slot, {
      shape,
      x: position.x,
      y: position.y,
      w: position.w,
      h: position.h,
      fill: position.fill,
      stroke: "#1c1917",
      strokeWidth: 2,
      radius: shape === "roundRect" ? 16 : slot.radius,
      text: labels[index],
      textStyle: { fontSize: 18, fontWeight: 700, color: "#1c1917", align: "center" },
    } satisfies Partial<DiagramShapeElement>);
  });

  const slotIds = new Set(slots.map((slot) => slot.id));
  document.elements = document.elements.filter((element) => {
    if (element.kind === "connector") return false;
    if (element.id === title.id || slotIds.has(element.id)) return true;
    if (element.kind === "shape" && isPlaceholderObject(element)) return false;
    return true;
  });

  const connections = agentTopic
    ? [[0, 1], [1, 2], [2, 5], [1, 4], [2, 3], [3, 5]]
    : labels.slice(0, -1).map((_, index) => [index, index + 1]);
  connections.forEach(([fromIndex, toIndex]) => {
    const from = slots[fromIndex];
    const to = slots[toIndex];
    if (from && to) document.elements.push(topicConnector(from.id, to.id, fromIndex, toIndex));
  });
}

function ensureTitleElement(
  document: EditableDiagramDocument,
  prompt: string,
  bounds: { x: number; y: number; w: number; h: number },
): DiagramTextElement {
  const existing = editableObjects(document).find((element): element is DiagramTextElement => (
    element.kind === "text" && element.w >= 320 && element.h <= 80
  ));
  const title: DiagramTextElement = existing || {
    id: createDiagramId("title"),
    kind: "text",
    x: bounds.x,
    y: bounds.y - 90,
    w: Math.max(680, Math.min(1040, bounds.w)),
    h: 46,
    text: "",
  };
  Object.assign(title, {
    x: bounds.x,
    y: Math.max((document.canvas.originY ?? 0) + 32, bounds.y - 100),
    w: Math.max(680, Math.min(1100, bounds.w)),
    h: 48,
    text: conciseTitle(prompt),
    textStyle: { fontFamily: "Times New Roman, serif", fontSize: 28, fontWeight: 700, color: "#1c1917", align: "center" },
  } satisfies Partial<DiagramTextElement>);
  if (!existing) document.elements.unshift(title);
  return title;
}

function ensureTopicSlots(
  document: EditableDiagramDocument,
  labels: string[],
  bounds: { x: number; y: number; w: number; h: number },
  title: DiagramTextElement,
): DiagramShapeElement[] {
  const shapeSlots = document.elements
    .filter((element): element is DiagramShapeElement => element.kind === "shape")
    .sort((a, b) => (a.y - b.y) || (a.x - b.x));
  const slots = shapeSlots.slice(0, labels.length);
  while (slots.length < labels.length) {
    const slot: DiagramShapeElement = {
      id: createDiagramId("node"),
      kind: "shape",
      shape: "roundRect",
      x: bounds.x + slots.length * 220,
      y: title.y + title.h + 90,
      w: 178,
      h: 84,
      fill: "#e8eff4",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: labels[slots.length],
      textStyle: { fontSize: 18, fontWeight: 700, color: "#1c1917", align: "center" },
    };
    document.elements.push(slot);
    slots.push(slot);
  }
  return slots;
}

function agentArchitecturePositions(bounds: { x: number; y: number; w: number; h: number }, top: number): Array<{ x: number; y: number; w: number; h: number; fill: string }> {
  const left = bounds.x + 40;
  const gap = 230;
  return [
    { x: left, y: top + 135, w: 176, h: 82, fill: "#e8eff4" },
    { x: left + gap, y: top + 135, w: 190, h: 86, fill: "#f3ecd6" },
    { x: left + gap * 2, y: top + 135, w: 200, h: 86, fill: "#e4efe8" },
    { x: left + gap * 2, y: top + 280, w: 190, h: 82, fill: "#ece9f5" },
    { x: left + gap, y: top, w: 190, h: 82, fill: "#e3e9f1" },
    { x: left + gap * 3, y: top + 135, w: 176, h: 82, fill: "#f3ecd6" },
  ];
}

function defaultTopicPositions(bounds: { x: number; y: number; w: number; h: number }, top: number, count: number): Array<{ x: number; y: number; w: number; h: number; fill: string }> {
  const left = bounds.x + 40;
  const gap = 220;
  return Array.from({ length: count }, (_, index) => ({
    x: left + (index % 4) * gap,
    y: top + Math.floor(index / 4) * 140,
    w: 178,
    h: 84,
    fill: index % 2 === 0 ? "#e8eff4" : "#f3ecd6",
  }));
}

function topicShapeForLabel(label: string): DiagramShapeElement["shape"] {
  const normalized = normalizeLabel(label);
  if (/memory|store|knowledge|db|database|cache|记忆|知识|数据库/.test(normalized)) return "cylinder";
  if (/router|tool|工具|路由/.test(normalized)) return "hexagon";
  return "roundRect";
}

function topicConnector(from: string, to: string, fromIndex: number, toIndex: number): DiagramConnectorElement {
  const vertical = Math.abs(fromIndex - toIndex) > 2;
  return {
    id: createDiagramId("conn"),
    kind: "connector",
    from: { bind: { elementId: from, anchor: vertical ? "bottom" : "right" } },
    to: { bind: { elementId: to, anchor: vertical ? "top" : "left" } },
    routing: vertical ? "elbow" : "straight",
    stroke: "#008cad",
    strokeWidth: 4,
    arrowEnd: true,
  };
}

function isPlaceholderObject(element: DiagramShapeElement | DiagramTextElement): boolean {
  const text = normalizeLabel(elementText(element));
  return !text || /^(node|box|shape|input|analyze|transform|output)$/.test(text);
}

function mergeGeneratedTopic(document: EditableDiagramDocument, generated: EditableDiagramDocument): void {
  const generatedObjects = generated.elements.filter((element): element is DiagramShapeElement | DiagramTextElement => element.kind !== "connector");
  const generatedConnectors = generated.elements.filter((element): element is DiagramConnectorElement => element.kind === "connector");
  const bounds = diagramObjectBounds(document);
  const generatedBounds = generatedObjectsBounds(generatedObjects);
  const offsetX = bounds ? bounds.x + bounds.w + 120 - (generatedBounds?.x || 0) : 0;
  const offsetY = bounds ? Math.max(160, bounds.y) - (generatedBounds?.y || 0) : 0;
  const idMap = new Map<string, string>();

  generatedObjects.forEach((object, index) => {
    const text = elementText(object);
    if (!text.trim()) return;
    const existing = findObjectByLabel(document, text);
    if (existing) {
      idMap.set(object.id, existing.id);
      return;
    }
    const nextId = createDiagramId(object.kind === "text" ? "text" : "node");
    idMap.set(object.id, nextId);
    document.elements.push({
      ...object,
      id: nextId,
      x: object.x + offsetX + (index % 2) * 8,
      y: object.y + offsetY,
    });
  });

  generatedConnectors.forEach((generatedConnector) => {
    const from = generatedConnector.from.bind?.elementId ? idMap.get(generatedConnector.from.bind.elementId) : undefined;
    const to = generatedConnector.to.bind?.elementId ? idMap.get(generatedConnector.to.bind.elementId) : undefined;
    if (from && to) ensureConnector(document, from, to);
  });
}

function diagramObjectBounds(document: EditableDiagramDocument): { x: number; y: number; w: number; h: number } | null {
  return generatedObjectsBounds(editableObjects(document));
}

function generatedObjectsBounds(objects: Array<DiagramShapeElement | DiagramTextElement>): { x: number; y: number; w: number; h: number } | null {
  const boxes = objects.map((element) => getElementBounds(element)).filter(Boolean) as Array<{ x: number; y: number; w: number; h: number }>;
  if (!boxes.length) return null;
  const minX = Math.min(...boxes.map((box) => box.x));
  const minY = Math.min(...boxes.map((box) => box.y));
  const maxX = Math.max(...boxes.map((box) => box.x + box.w));
  const maxY = Math.max(...boxes.map((box) => box.y + box.h));
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

function layeredScientificDiagram(prompt: string, layerLabels: string[]): EditableDiagramDocument {
  const canvas = { width: 2400, height: 1600, unit: "px" as const, originX: -120, originY: -90 };
  const layerIds = layerLabels.map(() => createDiagramId("layer"));
  const left = 245;
  const widths = [700, 610, 520, 700, 620, 540];
  const yStart = 92;
  const gap = 132;
  const elements: DiagramElement[] = [
    titleElement(prompt),
    ...layerLabels.map((label, index) => layerShape(layerIds[index], left + index * 20, yStart + index * gap, widths[index] || 620, label, index)),
    ...layerLabels.map((label, index) => {
      const layerHeight = index === 0 ? 92 : 78;
      return layerLabel(label, left + index * 20 + (widths[index] || 620) - 210, yStart + index * gap + layerHeight + 6);
    }),
    ...layerIds.slice(0, -1).map((from, index) => connector(from, layerIds[index + 1])),
  ];

  const nodeY = yStart + 27;
  const nodeLabels = ["R₁¹", "R₁²", "…", "R₁ᵐ", "Rᵢ¹", "Rᵢ²", "…", "Rₙᵐ"];
  nodeLabels.forEach((label, index) => {
    elements.push({
      id: createDiagramId("rule"),
      kind: "shape",
      shape: label === "…" ? "rect" : "rect",
      x: 300 + index * 70,
      y: nodeY,
      w: label === "…" ? 38 : 54,
      h: 38,
      fill: label === "…" ? "transparent" : "#f5df9b",
      stroke: label === "…" ? "transparent" : "#1c1917",
      strokeWidth: label === "…" ? 0 : 2,
      text: label,
      textStyle: { fontFamily: "Times New Roman, serif", fontSize: 20, fontWeight: 700, color: "#1c1917", align: "center" },
    } satisfies DiagramShapeElement);
  });

  const normalizeY = yStart + gap * 2 + 20;
  ["N", "N", "N", "N"].forEach((label, index) => {
    elements.push({
      id: createDiagramId("norm"),
      kind: "shape",
      shape: "rect",
      x: 380 + index * 95,
      y: normalizeY,
      w: 50,
      h: 38,
      fill: "#ffffff",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: label,
      textStyle: { fontFamily: "Times New Roman, serif", fontSize: 20, fontWeight: 700, color: "#1c1917", align: "center" },
    } satisfies DiagramShapeElement);
  });

  const defuzzY = yStart + gap * 3 + 20;
  ["ĉ₁", "ĉᵢ", "ĉₙ"].forEach((label, index) => {
    elements.push({
      id: createDiagramId("defuzz"),
      kind: "shape",
      shape: "rect",
      x: 405 + index * 145,
      y: defuzzY,
      w: 58,
      h: 40,
      fill: "#f3a77f",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: label,
      textStyle: { fontFamily: "Times New Roman, serif", fontSize: 21, fontWeight: 700, color: "#1c1917", align: "center" },
    } satisfies DiagramShapeElement);
  });

  if (/kalman|smoothing|滤波|平滑/.test(prompt.toLowerCase())) {
    const kalmanId = createDiagramId("kalman");
    elements.push({
      id: kalmanId,
      kind: "shape",
      shape: "roundRect",
      x: 925,
      y: 255,
      w: 180,
      h: 88,
      fill: "#bfe1f0",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: "Kalman\nSmoothing",
      textStyle: { fontFamily: "Times New Roman, serif", fontSize: 26, fontWeight: 700, color: "#1c1917", align: "center" },
    } satisfies DiagramShapeElement);
    elements.push({
      id: createDiagramId("conn"),
      kind: "connector",
      from: { bind: { elementId: kalmanId, anchor: "bottom" } },
      to: { bind: { elementId: layerIds[layerIds.length - 1], anchor: "right" } },
      routing: "elbow",
      stroke: "#1c1917",
      strokeWidth: 3,
      arrowEnd: true,
    } satisfies DiagramConnectorElement);
  }

  return {
    version: DIAGRAM_VERSION,
    id: createDiagramId(),
    title: "AI editable diagram",
    prompt,
    canvas,
    theme: {
      fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      labelFontFamily: "Times New Roman, serif",
      palette: {
        line: "#1c1917",
        accent: "#008cad",
        containerStroke: "#55a9e6",
        cream: "#f5df9b",
        orange: "#f3a77f",
        blueFill: "#bfe1f0",
        paper: "#ffffff",
        text: "#1c1917",
        muted: "#78716c",
      },
    },
    elements,
    groups: [{ id: createDiagramId("group"), label: "Generated layers", elementIds: layerIds }],
    constraints: [{ type: "alignX", elementIds: layerIds }],
  };
}

function genericFlowDiagram(prompt: string): EditableDiagramDocument {
  const labels = prompt
    .split(/[,，;；>→\n]/)
    .map((part) => part.trim())
    .filter(Boolean)
    .slice(0, 5);
  const nodeLabels = labels.length >= 2 ? labels : inferGenericFlowLabels(prompt);
  const elements: DiagramElement[] = [titleElement(prompt || "Editable flow diagram")];
  const ids = nodeLabels.map(() => createDiagramId("node"));
  nodeLabels.forEach((label, index) => {
    elements.push({
      id: ids[index],
      kind: "shape",
      shape: "roundRect",
      x: 120 + index * 245,
      y: 300,
      w: 170,
      h: 82,
      fill: index % 2 === 0 ? "#e8eff4" : "#f3ecd6",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: label,
      textStyle: { fontSize: 19, fontWeight: 700, color: "#1c1917", align: "center" },
    } satisfies DiagramShapeElement);
    if (index > 0) elements.push(connector(ids[index - 1], ids[index], "right", "left"));
  });
  return {
    version: DIAGRAM_VERSION,
    id: createDiagramId(),
    title: "AI editable flow",
    prompt,
    canvas: { width: 2400, height: 1600, unit: "px", originX: -120, originY: -90 },
    theme: {
      fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      palette: { line: "#1c1917", accent: "#4f7d75", paper: "#ffffff", text: "#1c1917" },
    },
    elements,
    groups: [{ id: createDiagramId("group"), label: "Generated flow", elementIds: ids }],
  };
}

function circuitDiagram(prompt: string): EditableDiagramDocument {
  const battery = createDiagramId("battery");
  const switchId = createDiagramId("switch");
  const resistor = createDiagramId("resistor");
  const led = createDiagramId("led");
  const ground = createDiagramId("ground");
  const controller = createDiagramId("controller");
  const elements: DiagramElement[] = [
    titleElement(prompt || "Editable circuit diagram"),
    {
      id: battery,
      kind: "shape",
      shape: "roundRect",
      x: 110,
      y: 315,
      w: 150,
      h: 92,
      fill: "#f3ecd6",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: "Battery\n+  -",
      textStyle: { fontSize: 20, fontWeight: 700, color: "#1c1917", align: "center" },
    },
    {
      id: switchId,
      kind: "shape",
      shape: "parallelogram",
      x: 360,
      y: 320,
      w: 160,
      h: 82,
      fill: "#e8eff4",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: "Switch",
      textStyle: { fontSize: 19, fontWeight: 700, color: "#1c1917", align: "center" },
    },
    {
      id: resistor,
      kind: "shape",
      shape: "rect",
      x: 620,
      y: 320,
      w: 175,
      h: 82,
      fill: "#f9f4ec",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: "Resistor",
      textStyle: { fontSize: 19, fontWeight: 700, color: "#1c1917", align: "center" },
    },
    {
      id: led,
      kind: "shape",
      shape: "ellipse",
      x: 895,
      y: 310,
      w: 130,
      h: 100,
      fill: "#e4efe8",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: "LED",
      textStyle: { fontSize: 20, fontWeight: 700, color: "#1c1917", align: "center" },
    },
    {
      id: ground,
      kind: "shape",
      shape: "triangle",
      x: 930,
      y: 535,
      w: 80,
      h: 72,
      fill: "#fafaf9",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: "GND",
      textStyle: { fontSize: 15, fontWeight: 700, color: "#1c1917", align: "center" },
    },
    {
      id: controller,
      kind: "shape",
      shape: "rect",
      x: 500,
      y: 125,
      w: 245,
      h: 95,
      fill: "#ece9f5",
      stroke: "#1c1917",
      strokeWidth: 2,
      text: "Control signal",
      textStyle: { fontSize: 18, fontWeight: 700, color: "#1c1917", align: "center" },
    },
    circuitConnector(battery, switchId, "right", "left"),
    circuitConnector(switchId, resistor, "right", "left"),
    circuitConnector(resistor, led, "right", "left"),
    circuitConnector(led, ground, "bottom", "top", "elbow"),
    circuitConnector(controller, switchId, "bottom", "top", "elbow", "gate"),
  ];
  return {
    version: DIAGRAM_VERSION,
    id: createDiagramId(),
    title: "Editable circuit diagram",
    prompt,
    canvas: { width: 2400, height: 1600, unit: "px", originX: -120, originY: -90 },
    theme: {
      fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      palette: { line: "#1c1917", accent: "#4f7d75", paper: "#ffffff", text: "#1c1917" },
    },
    elements,
    groups: [{ id: createDiagramId("group"), label: "Circuit", elementIds: [battery, switchId, resistor, led, ground, controller] }],
  };
}

function circuitConnector(
  from: string,
  to: string,
  fromAnchor: "top" | "right" | "bottom" | "left" | "center",
  toAnchor: "top" | "right" | "bottom" | "left" | "center",
  routing: DiagramConnectorRouting = "straight",
  label?: string,
): DiagramConnectorElement {
  return {
    ...connector(from, to, fromAnchor, toAnchor),
    routing,
    label,
    stroke: "#436b65",
    strokeWidth: 4,
    arrowEnd: false,
  };
}

function inferGenericFlowLabels(prompt: string): string[] {
  const lowered = prompt.toLowerCase();
  if (/circuit|schematic|electronics|电路|电子/.test(lowered)) return ["Battery", "Switch", "Resistor", "LED", "Ground"];
  if (/agent|智能体|代理|架构/.test(lowered)) return ["User request", "Coordinator agent", "Specialist agents", "Tool router", "Memory", "Response"];
  if (/rag|knowledge|知识|检索/.test(lowered)) return ["Query", "Retriever", "Knowledge base", "Ranker", "Answer"];
  if (/pdf|document|文件|文档/.test(lowered)) return ["Upload", "Parse", "Edit", "Review", "Export"];
  if (/data|数据|pipeline|etl/.test(lowered)) return ["Source", "Ingest", "Transform", "Validate", "Publish"];
  return ["Input", "Analyze", "Transform", "Output"];
}

function titleElement(prompt: string): DiagramTextElement {
  return {
    id: createDiagramId("title"),
    kind: "text",
    x: 80,
    y: 28,
    w: 1040,
    h: 42,
    text: prompt.trim() ? conciseTitle(prompt) : "Editable diagram",
    textStyle: { fontFamily: "Times New Roman, serif", fontSize: 28, fontWeight: 700, color: "#1c1917", align: "center" },
  };
}

function conciseTitle(prompt: string): string {
  const cleaned = prompt.replace(/\s+/g, " ").trim();
  return cleaned.length > 72 ? `${cleaned.slice(0, 69)}...` : cleaned;
}

function layerShape(id: string, x: number, y: number, w: number, label: string, index: number): DiagramShapeElement {
  return {
    id,
    name: label,
    kind: "shape",
    shape: "roundRect",
    x,
    y,
    w,
    h: index === 0 ? 92 : 78,
    fill: "transparent",
    stroke: "#55a9e6",
    strokeWidth: 2,
    strokeDash: "dash",
    radius: 16,
    text: "",
    textStyle: { fontFamily: "Times New Roman, serif", fontSize: 25, fontWeight: 700, color: "#1c1917", align: "center" },
  };
}

function layerLabel(label: string, x: number, y: number): DiagramTextElement {
  return {
    id: createDiagramId("label"),
    kind: "text",
    x,
    y,
    w: 205,
    h: 48,
    text: label.replace(/\s+Layer$/, "\nLayer"),
    textStyle: { fontFamily: "Times New Roman, serif", fontSize: 21, fontWeight: 700, color: "#1c1917", align: "center" },
  };
}

function connector(
  from: string,
  to: string,
  fromAnchor: "top" | "right" | "bottom" | "left" | "center" = "bottom",
  toAnchor: "top" | "right" | "bottom" | "left" | "center" = "top",
): DiagramConnectorElement {
  return {
    id: createDiagramId("conn"),
    kind: "connector",
    from: { bind: { elementId: from, anchor: fromAnchor } },
    to: { bind: { elementId: to, anchor: toAnchor } },
    routing: "straight",
    stroke: "#008cad",
    strokeWidth: 5,
    arrowEnd: true,
  };
}
