import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import ShareDialog from "../../src/components/permissions/ShareDialog";
import { capabilitiesToShareRole, shareRoleCapabilities } from "../../src/lib/shareRoles";
import "../../src/index.css";

function Fixture() {
  const [capabilities, setCapabilities] = useState(["view", "comment", "edit", "manage_metadata", "grant_access", "share_internal"]);
  const role = capabilitiesToShareRole(capabilities);
  return <>
    <ShareDialog open onClose={() => {}} resourceType="document" resourceId="roles-fixture"
      resourceName="TEST ONLY - Document sharing" classification="internal" visibility="private"
      internalGrants={[{ id: "colleague", user_email: "colleague@example.test", user_name: "Test colleague", role, source: "explicit" }]}
      onUpdateInternalRole={async (_id, nextRole) => setCapabilities(shareRoleCapabilities(nextRole))} />
    <output hidden data-testid="selected-capabilities">{JSON.stringify(capabilities)}</output>
  </>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
