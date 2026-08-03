import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const editorSource = await readFile(
  new URL("../src/pages/DocEditor.tsx", import.meta.url),
  "utf8",
);

test("manual document saves report the persisted result", () => {
  assert.match(editorSource, /import \{ useToastStore \} from "\.\.\/stores\/toast"/);
  assert.match(
    editorSource,
    /const handleManualSave = useCallback\(async \(\) => \{[\s\S]*?await flushSave\(content\)[\s\S]*?showSaveSuccess\(t\("page\.blueprint_detail\.saved"\)\)[\s\S]*?showSaveError\(t\("page\.blueprint_detail\.save_failed"\)\)/,
  );
  assert.match(editorSource, /onClick=\{\(\) => void handleManualSave\(\)\}/);
});
