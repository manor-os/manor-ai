import { api } from "../lib/api";
import type { AttachedItem } from "./ChatInputFooter";

export type TemplateRemixSampleLike = {
  title: string;
  prompt: string;
  previewContent?: {
    sampleSrc?: string;
    videoSrc?: string;
  };
};

type TemplateSourceKind =
  | "document"
  | "presentation"
  | "spreadsheet"
  | "pdf"
  | "website"
  | "image"
  | "video";

type TemplateSourceSpec = {
  extension: string;
  mimeType: string;
  kind: TemplateSourceKind;
};

const TEMPLATE_SOURCE_SPECS: Record<string, TemplateSourceSpec> = {
  doc: { extension: "doc", mimeType: "application/msword", kind: "document" },
  docx: {
    extension: "docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    kind: "document",
  },
  ppt: { extension: "ppt", mimeType: "application/vnd.ms-powerpoint", kind: "presentation" },
  pptx: {
    extension: "pptx",
    mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    kind: "presentation",
  },
  xls: { extension: "xls", mimeType: "application/vnd.ms-excel", kind: "spreadsheet" },
  xlsx: {
    extension: "xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    kind: "spreadsheet",
  },
  pdf: { extension: "pdf", mimeType: "application/pdf", kind: "pdf" },
  html: { extension: "html", mimeType: "text/html", kind: "website" },
  htm: { extension: "html", mimeType: "text/html", kind: "website" },
  jpg: { extension: "jpg", mimeType: "image/jpeg", kind: "image" },
  jpeg: { extension: "jpg", mimeType: "image/jpeg", kind: "image" },
  png: { extension: "png", mimeType: "image/png", kind: "image" },
  webp: { extension: "webp", mimeType: "image/webp", kind: "image" },
  gif: { extension: "gif", mimeType: "image/gif", kind: "image" },
  svg: { extension: "svg", mimeType: "image/svg+xml", kind: "image" },
  mp4: { extension: "mp4", mimeType: "video/mp4", kind: "video" },
  mov: { extension: "mov", mimeType: "video/quicktime", kind: "video" },
  webm: { extension: "webm", mimeType: "video/webm", kind: "video" },
};

function sourceSpecForUrl(sourceUrl: string): TemplateSourceSpec | null {
  const cleanPath = sourceUrl.split(/[?#]/, 1)[0].toLowerCase();
  const extension = cleanPath.match(/\.([a-z0-9]+)$/)?.[1] || "";
  return TEMPLATE_SOURCE_SPECS[extension] || null;
}

function templateSource(sample: TemplateRemixSampleLike) {
  const candidates = [
    sample.previewContent?.sampleSrc?.trim(),
    sample.previewContent?.videoSrc?.trim(),
  ].filter((source): source is string => Boolean(source));

  for (const sourceUrl of candidates) {
    const spec = sourceSpecForUrl(sourceUrl);
    if (spec) return { sourceUrl, spec };
  }
  return null;
}

function attachmentSpec(attachment: AttachedItem): TemplateSourceSpec | null {
  const candidates = [attachment.fileType, attachment.name]
    .filter((value): value is string => Boolean(value))
    .map((value) => value.toLowerCase());
  for (const candidate of candidates) {
    const direct = TEMPLATE_SOURCE_SPECS[candidate.replace(/^\./, "")];
    if (direct) return direct;
    const extension = candidate.match(/\.([a-z0-9]+)$/)?.[1];
    if (extension && TEMPLATE_SOURCE_SPECS[extension]) {
      return TEMPLATE_SOURCE_SPECS[extension];
    }
  }
  const mimeMatch = Object.values(TEMPLATE_SOURCE_SPECS).find(
    (spec) => spec.mimeType === attachment.mimeType,
  );
  return mimeMatch || null;
}

function sourceTemplateInstruction(
  attachment: AttachedItem,
): string {
  const spec = attachmentSpec(attachment);
  const sourcePath = JSON.stringify(attachment.fsPath || attachment.name);
  const shared = [
    `Use the attached ${spec?.extension.toUpperCase() || "file"} as the actual read-only source template, not merely as a topic reference.`,
    `Its exact Manor workspace path is ${sourcePath}. Use that exact attachment directly instead of searching the document catalog for another copy.`,
    "Create a separate new artifact for the user's remix request. Do not edit, rename, overwrite, or simply return the attached source template.",
  ];

  switch (spec?.kind) {
    case "document":
      return [
        ...shared,
        "Invoke the document creation skill and inspect the source document's page sizes, styles, typography, headers, footers, tables, images, spacing, and section structure before authoring.",
        "Preserve the recognizable editorial system while replacing all sample copy, data, brand details, and imagery for the user's request.",
        "Render every final page, inspect it at full size, and repair clipping, overflow, awkward page breaks, or weak image crops before delivery.",
      ].join(" ");
    case "spreadsheet":
      return [
        ...shared,
        "Inspect every source sheet, formula, chart, validation, conditional format, merge, style, row height, and column width before authoring.",
        "Preserve the layout, visual language, calculation patterns, and workbook behaviors that fit the request while replacing the sample domain, labels, data, formulas, and sheet structure as required.",
        "Use the spreadsheet creation workflow to build a distinctly named workbook, recalculate formulas, reopen it to verify integrity, render the relevant sheets, and fix visible defects before delivery.",
      ].join(" ");
    case "presentation":
      return [
        ...shared,
        "Invoke the presentation skill, import the attached deck as the source template, and inspect every source slide plus its master and layout relationships before authoring.",
        "Build the new deck from the closest source layouts so its typography, grid, image frames, graphic language, and pacing remain recognizable while all sample content is replaced.",
        "Keep the final deck editable, use coherent replacement imagery, render every slide, and fix all overflow or overlap before delivery.",
      ].join(" ");
    case "pdf":
      return [
        ...shared,
        "Invoke the PDF skill, inspect every source page, extract its text and embedded image inventory, record every page size, and render all source pages to PNG before authoring.",
        "Preserve the source template's page proportions, grid, typography hierarchy, image crops, color relationships, spacing, metric treatment, section rhythm, and page-to-page pacing while replacing all sample content.",
        "Use high-resolution real or generated imagery wherever the source uses photography. Render every final page with Poppler and repair clipping, overlap, low contrast, or weak image crops before delivery.",
      ].join(" ");
    case "website":
      return [
        ...shared,
        "Inspect the source HTML, content hierarchy, CSS, typography, spacing, imagery, responsive behavior, and interaction patterns before authoring.",
        "Create a separate responsive website artifact that preserves the source's recognizable visual system while replacing the sample brand, copy, data, links, and media.",
        "Verify the final page at desktop and mobile widths and repair overflow, broken interactions, inaccessible controls, or weak responsive layouts before delivery.",
      ].join(" ");
    case "image":
      return [
        ...shared,
        "Use the image generation workflow with the attached original as the visual reference.",
        "Preserve the source's aspect ratio, composition, art direction, lighting, color relationships, material treatment, and level of finish while replacing the sample subject, brand, copy, and product details.",
        "Generate a separate high-resolution image. Do not return a resized copy, an empty placeholder, or a screenshot of the source.",
      ].join(" ");
    case "video":
      return [
        ...shared,
        "Use the video creation workflow and inspect the source's framing, scene rhythm, transitions, typography, motion language, audio treatment, and duration before authoring.",
        "Create a separate video that preserves the recognizable pacing and production quality while replacing the sample story, brand, copy, footage, and audio as required.",
        "Preview the full result, verify timing and legibility, and repair clipped text, abrupt cuts, poor crops, or broken audio before delivery.",
      ].join(" ");
    default:
      return shared.join(" ");
  }
}

export async function uploadTemplateRemixSource(
  sample: TemplateRemixSampleLike,
): Promise<AttachedItem | undefined> {
  const source = templateSource(sample);
  if (!source) return undefined;

  const response = await fetch(source.sourceUrl, { credentials: "same-origin" });
  if (!response.ok) {
    throw new Error(`Template download failed (${response.status})`);
  }
  const blob = await response.blob();
  const safeTitle = sample.title.replace(/[\\/:*?"<>|]+/g, "-").trim() || "Template";
  const templateFile = new File(
    [blob],
    `${safeTitle}.${source.spec.extension}`,
    { type: blob.type || source.spec.mimeType },
  );
  const document = await api.documents.upload(templateFile);
  if (!document?.id) {
    throw new Error("Template upload did not create a document");
  }

  return {
    type: "knowledge",
    id: document.id,
    name: document.name || templateFile.name,
    fsPath: document.fs_path,
    fileType: document.file_type || source.spec.extension,
    mimeType: document.mime_type || source.spec.mimeType,
  };
}

export function buildTemplateRemixPrompt(
  sample: TemplateRemixSampleLike,
  requirements: string,
  attachment?: AttachedItem,
): string {
  const normalizedRequirements = requirements.trim();
  const requirementsInstruction = normalizedRequirements
    ? `User requirements for the new artifact (these requirements take priority over the sample content):\n${normalizedRequirements}`
    : "";
  return [
    attachment ? `#${attachment.name}` : "",
    sample.prompt,
    requirementsInstruction,
    attachment ? sourceTemplateInstruction(attachment) : "",
  ].filter(Boolean).join("\n\n");
}

export async function prepareTemplateRemix(
  sample: TemplateRemixSampleLike,
  requirements = "",
) {
  const attachment = await uploadTemplateRemixSource(sample);
  return {
    prompt: buildTemplateRemixPrompt(sample, requirements, attachment),
    attachments: attachment ? [attachment] : [],
    attachedSource: Boolean(attachment),
  };
}
