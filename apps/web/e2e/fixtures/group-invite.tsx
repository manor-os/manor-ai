import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ChatRoomConversation, ChatRoomNewConversation } from "../../src/components/ChatRooms";
import { useAuthStore } from "../../src/stores/auth";
import Integrations from "../../src/pages/Integrations";
import DetailDrawer from "../../src/components/ui/DetailDrawer";
import "../../src/index.css";

useAuthStore.setState({ token: "synthetic-group-invite-token", isLoading: false,
  user: { id: "inviter", entity_id: "entity", role: "owner" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]> });
const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const mode = new URLSearchParams(window.location.search).get("mode");
createRoot(document.getElementById("root")!).render(
  <MemoryRouter initialEntries={[mode === "email" ? "/integrations" : "/"]}><QueryClientProvider client={queries}>
    <main style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      {mode === "email" ? <Integrations /> : mode === "existing" ? <ChatRoomConversation roomId="group" currentUserId="inviter" />
        : <ChatRoomNewConversation initialKind="group" rooms={[]} agents={[]} currentUserId="inviter"
          onClose={() => {}} onCreated={() => {}} onSelectAgent={() => {}} />}
    </main>
    <DetailDrawer />
  </QueryClientProvider></MemoryRouter>,
);
