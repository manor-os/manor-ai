import React from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import SignatureRequestDialog from "../../src/components/permissions/SignatureRequestDialog";
import "../../src/index.css";

createRoot(document.getElementById("root")!).render(
  <MemoryRouter>
    <SignatureRequestDialog open onClose={() => {}} documentId="geometry-fixture"
      documentName="TEST ONLY - Signature geometry.pdf" />
  </MemoryRouter>,
);
