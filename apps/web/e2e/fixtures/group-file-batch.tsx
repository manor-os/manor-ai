import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ChatRoomConversation } from "../../src/components/ChatRooms";
import { useAuthStore } from "../../src/stores/auth";
import "../../src/index.css";

const token = `test.${btoa(JSON.stringify({ sub: "member", entity_id: "fixture", exp: 4102444800 }))}.synthetic`;
localStorage.setItem("manor_token", token);
useAuthStore.setState({ token, isLoading: false,
  user: { id: "member", entity_id: "fixture", role: "owner" } as NonNullable<ReturnType<typeof useAuthStore.getState>["user"]> });
const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function Fixture() {
  const [roomId, setRoomId] = useState("group");
  return <main style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
    <button type="button" onClick={() => setRoomId("other")}>Switch fixture room</button>
    <ChatRoomConversation roomId={roomId} currentUserId="member" />
  </main>;
}

createRoot(document.getElementById("root")!).render(<MemoryRouter><QueryClientProvider client={queries}><Fixture /></QueryClientProvider></MemoryRouter>);
