import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useSearchParams } from "react-router-dom";
import { PublicGroupJoin } from "../../src/components/GroupPublicInvite";
import { ChatRoomConversation } from "../../src/components/ChatRooms";
import ProtectedRoute from "../../src/components/ProtectedRoute";
import Login from "../../src/pages/Login";
import { useAuthStore } from "../../src/stores/auth";
import "../../src/index.css";

const mode = new URLSearchParams(window.location.search).get("mode") || "manage";
const token = `test.${btoa(JSON.stringify({ sub: "inviter", exp: 4102444800 }))}.synthetic`;
if (mode === "manage" || mode === "join") localStorage.setItem("manor_token", token);
useAuthStore.setState({ token: mode === "auth" ? null : token, isLoading: false,
  user: mode === "auth" ? null : { id: "inviter", entity_id: "entity", role: "owner" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]> });
const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
function Join() {
  const [params] = useSearchParams();
  const [result, setResult] = useState("");
  return result ? <p role="status">{result}</p> : <PublicGroupJoin token={params.get("join") || ""} onClose={() => setResult("Closed")} onJoined={(room) => setResult(`Joined ${room.title}`)} />;
}
createRoot(document.getElementById("root")!).render(
  <MemoryRouter initialEntries={["/chat?join=synthetic-public-invitation-token-123456789"]}>
    <QueryClientProvider client={queries}>
      {mode === "manage" ? <main style={{ height: "100vh", display: "flex", flexDirection: "column" }}><ChatRoomConversation roomId="group" currentUserId="inviter" /></main> : <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<ProtectedRoute />}><Route path="/chat" element={<Join />} /></Route>
      </Routes>}
    </QueryClientProvider>
  </MemoryRouter>,
);
