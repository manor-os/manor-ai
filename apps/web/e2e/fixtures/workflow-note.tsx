import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import WorkflowCanvas from "../../src/components/workflows/WorkflowCanvas";
import "../../src/index.css";
import "../../src/pages/Flows.css";

function Fixture() {
  const [steps, setSteps] = useState<any[]>([
    { id: "note-1", type: "note", name: "Note", config: { text: "Remember this" }, position: { x: 80, y: 80 } },
  ]);
  return (
    <div className="workflow-editor-body" style={{ width: 900, height: 600 }}>
      <WorkflowCanvas
        steps={steps}
        onStepsChange={(next) => {
          (window as any).__savedSteps = next;
          setSteps(next);
        }}
      />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
