import { expect, test } from "@playwright/test";
import JSZip from "jszip";

test("an inserted DOCX table cell stays editable after the first save", async ({ page }) => {
  await page.goto("/");

  const savedBytes = await page.evaluate(async () => {
    const engine = await import("/src/lib/documentOoxml.ts");
    const originalFile = await engine.buildDocumentFile("<p>Anchor</p>", "table-repeat.docx");
    const originalBuffer = await originalFile.arrayBuffer();
    const sources = await engine.extractDocumentParagraphSources(originalBuffer);
    const baselineHtml = engine.annotateDocumentHtml("<p>Anchor</p>", sources);
    const insertedHtml = `${baselineHtml}<table><tbody><tr><th>Header</th><td>One</td></tr></tbody></table>`;

    const firstSave = await engine.editDocumentFileWithSnapshot(
      originalBuffer,
      baselineHtml,
      insertedHtml,
      "table-repeat.docx",
    );
    const editedAgain = firstSave.savedHtml.replace("One", "Changed");
    const secondSave = await engine.editDocumentFileWithSnapshot(
      firstSave.savedBuffer,
      firstSave.savedHtml,
      editedAgain,
      "table-repeat.docx",
    );

    return Array.from(new Uint8Array(secondSave.savedBuffer));
  });

  const zip = await JSZip.loadAsync(Uint8Array.from(savedBytes));
  const documentXml = await zip.file("word/document.xml")?.async("text");
  expect(documentXml).toBeTruthy();
  expect(documentXml?.match(/<w:tbl>/g)).toHaveLength(1);
  expect(documentXml).toContain("Changed");
  expect(documentXml).not.toContain(">One<");
});

test("an inserted DOCX image is replaced instead of duplicated on its next edit", async ({ page }) => {
  await page.goto("/");

  const savedBytes = await page.evaluate(async () => {
    const engine = await import("/src/lib/documentOoxml.ts");
    const originalFile = await engine.buildDocumentFile("<p>Anchor</p>", "image-repeat.docx");
    const originalBuffer = await originalFile.arrayBuffer();
    const sources = await engine.extractDocumentParagraphSources(originalBuffer);
    const baselineHtml = engine.annotateDocumentHtml("<p>Anchor</p>", sources);
    const imageSource = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    const insertedHtml = `${baselineHtml}<figure class="doc-editor-media"><img src="${imageSource}" alt="Photo"><figcaption>Photo</figcaption></figure>`;

    const firstSave = await engine.editDocumentFileWithSnapshot(
      originalBuffer,
      baselineHtml,
      insertedHtml,
      "image-repeat.docx",
    );
    const editedAgain = firstSave.savedHtml.replaceAll("Photo", "Renamed");
    const secondSave = await engine.editDocumentFileWithSnapshot(
      firstSave.savedBuffer,
      firstSave.savedHtml,
      editedAgain,
      "image-repeat.docx",
    );

    return Array.from(new Uint8Array(secondSave.savedBuffer));
  });

  const zip = await JSZip.loadAsync(Uint8Array.from(savedBytes));
  const documentXml = await zip.file("word/document.xml")?.async("text");
  const relationshipsXml = await zip.file("word/_rels/document.xml.rels")?.async("text");
  const mediaParts = Object.keys(zip.files).filter((part) => /^word\/media\/[^/]+$/.test(part));
  expect(documentXml).toBeTruthy();
  expect(documentXml?.match(/<w:drawing/g)).toHaveLength(1);
  expect(documentXml).toContain("Renamed");
  expect(documentXml).not.toContain(">Photo<");
  expect(relationshipsXml?.match(/relationships\/image/g)).toHaveLength(1);
  expect(mediaParts).toHaveLength(1);
});

test("inserted DOCX quote and code blocks stay editable after the first save", async ({ page }) => {
  await page.goto("/");

  const savedBytes = await page.evaluate(async () => {
    const engine = await import("/src/lib/documentOoxml.ts");
    const originalFile = await engine.buildDocumentFile("<p>Anchor</p>", "blocks-repeat.docx");
    const originalBuffer = await originalFile.arrayBuffer();
    const sources = await engine.extractDocumentParagraphSources(originalBuffer);
    const baselineHtml = engine.annotateDocumentHtml("<p>Anchor</p>", sources);
    const insertedHtml = `${baselineHtml}<blockquote>Original quote</blockquote><pre>const before = true;</pre>`;

    const firstSave = await engine.editDocumentFileWithSnapshot(
      originalBuffer,
      baselineHtml,
      insertedHtml,
      "blocks-repeat.docx",
    );
    const editedAgain = firstSave.savedHtml
      .replace("Original quote", "Revised quote")
      .replace("const before = true;", "const after = true;");
    const secondSave = await engine.editDocumentFileWithSnapshot(
      firstSave.savedBuffer,
      firstSave.savedHtml,
      editedAgain,
      "blocks-repeat.docx",
    );

    return Array.from(new Uint8Array(secondSave.savedBuffer));
  });

  const zip = await JSZip.loadAsync(Uint8Array.from(savedBytes));
  const documentXml = await zip.file("word/document.xml")?.async("text");
  expect(documentXml).toContain("Revised quote");
  expect(documentXml).toContain("const after = true;");
  expect(documentXml).not.toContain("Original quote");
  expect(documentXml).not.toContain("const before = true;");
});

test("root-level pasted images and links remain native DOCX resources", async ({ page }) => {
  await page.goto("/");

  const savedBytes = await page.evaluate(async () => {
    const engine = await import("/src/lib/documentOoxml.ts");
    const originalFile = await engine.buildDocumentFile("<p>Anchor</p>", "pasted-resources.docx");
    const originalBuffer = await originalFile.arrayBuffer();
    const sources = await engine.extractDocumentParagraphSources(originalBuffer);
    const baselineHtml = engine.annotateDocumentHtml("<p>Anchor</p>", sources);
    const imageSource = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    const insertedHtml = `${baselineHtml}<img src="${imageSource}" alt="Pasted photo"><a href="https://example.com/old">Old link</a>`;

    const firstSave = await engine.editDocumentFileWithSnapshot(
      originalBuffer,
      baselineHtml,
      insertedHtml,
      "pasted-resources.docx",
    );
    const editedAgain = firstSave.savedHtml
      .replace("Pasted photo", "Renamed photo")
      .replace("https://example.com/old", "https://example.com/new")
      .replace("Old link", "New link");
    const secondSave = await engine.editDocumentFileWithSnapshot(
      firstSave.savedBuffer,
      firstSave.savedHtml,
      editedAgain,
      "pasted-resources.docx",
    );

    return Array.from(new Uint8Array(secondSave.savedBuffer));
  });

  const zip = await JSZip.loadAsync(Uint8Array.from(savedBytes));
  const documentXml = await zip.file("word/document.xml")?.async("text");
  const relationshipsXml = await zip.file("word/_rels/document.xml.rels")?.async("text");
  expect(documentXml?.match(/<w:drawing/g)).toHaveLength(1);
  expect(documentXml?.match(/<w:hyperlink/g)).toHaveLength(1);
  expect(documentXml).toContain("Renamed photo");
  expect(documentXml).toContain("New link");
  expect(documentXml).not.toContain("Old link");
  expect(relationshipsXml).toContain("https://example.com/new");
  expect(relationshipsXml).not.toContain("https://example.com/old");
});

test("root-level DOCX image attributes save without another resource changing", async ({ page }) => {
  await page.goto("/");

  const savedBytes = await page.evaluate(async () => {
    const engine = await import("/src/lib/documentOoxml.ts");
    const originalFile = await engine.buildDocumentFile("<p>Anchor</p>", "image-attributes.docx");
    const originalBuffer = await originalFile.arrayBuffer();
    const sources = await engine.extractDocumentParagraphSources(originalBuffer);
    const baselineHtml = engine.annotateDocumentHtml("<p>Anchor</p>", sources);
    const imageSource = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    const insertedHtml = `${baselineHtml}<img src="${imageSource}" alt="Original alt" width="10">`;

    const firstSave = await engine.editDocumentFileWithSnapshot(
      originalBuffer,
      baselineHtml,
      insertedHtml,
      "image-attributes.docx",
    );
    const editedAgain = firstSave.savedHtml.replace(
      'alt="Original alt" width="10"',
      'alt="Changed alt" width="20"',
    );
    const secondSave = await engine.editDocumentFileWithSnapshot(
      firstSave.savedBuffer,
      firstSave.savedHtml,
      editedAgain,
      "image-attributes.docx",
    );

    return Array.from(new Uint8Array(secondSave.savedBuffer));
  });

  const zip = await JSZip.loadAsync(Uint8Array.from(savedBytes));
  const documentXml = await zip.file("word/document.xml")?.async("text");
  expect(documentXml).toContain('name="Changed alt"');
  expect(documentXml).toContain('cx="190500" cy="190500"');
  expect(documentXml).not.toContain("Original alt");
});

test("inserted DOCX page breaks and dividers can be deleted after saving", async ({ page }) => {
  await page.goto("/");

  const savedBytes = await page.evaluate(async () => {
    const engine = await import("/src/lib/documentOoxml.ts");
    const originalFile = await engine.buildDocumentFile("<p>Anchor</p>", "structural-objects.docx");
    const originalBuffer = await originalFile.arrayBuffer();
    const sources = await engine.extractDocumentParagraphSources(originalBuffer);
    const baselineHtml = engine.annotateDocumentHtml("<p>Anchor</p>", sources);
    const insertedHtml = `${baselineHtml}<div class="doc-editor-page-break" data-docx-page-break="true" contenteditable="false"><span>Page break</span></div><hr>`;

    const firstSave = await engine.editDocumentFileWithSnapshot(
      originalBuffer,
      baselineHtml,
      insertedHtml,
      "structural-objects.docx",
    );
    const parsed = new DOMParser().parseFromString(`<div id="root">${firstSave.savedHtml}</div>`, "text/html");
    const root = parsed.getElementById("root");
    root?.querySelector("[data-docx-page-break='true']")?.remove();
    root?.querySelector("hr")?.remove();
    const secondSave = await engine.editDocumentFileWithSnapshot(
      firstSave.savedBuffer,
      firstSave.savedHtml,
      root?.innerHTML || baselineHtml,
      "structural-objects.docx",
    );

    return Array.from(new Uint8Array(secondSave.savedBuffer));
  });

  const zip = await JSZip.loadAsync(Uint8Array.from(savedBytes));
  const documentXml = await zip.file("word/document.xml")?.async("text");
  expect(documentXml).not.toContain('w:type="page"');
  expect(documentXml).not.toContain("<w:pBdr>");
});
