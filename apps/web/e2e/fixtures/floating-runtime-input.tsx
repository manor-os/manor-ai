import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import FloatingChat from "../../src/components/FloatingChat";
import { useAuthStore } from "../../src/stores/auth";
import { useChatStreamStore } from "../../src/stores/chatStream";
import { setLocale } from "../../src/lib/i18n";
import "../../src/index.css";

// A durable run parked on `request_input`, already in the stream store when the panel opens.
await setLocale("en");
// A token naming its owner: the panel resets the stream store when it cannot tell who is signed in.
const claims = btoa(JSON.stringify({ sub: "user_1", entity_id: "entity_1", exp: 4102444800 }));
useAuthStore.setState({ token: `h.${claims}.s`, isLoading: false });
const key = "floating-question";
useChatStreamStore.setState((state) => ({
  latestSessionKey: key,
  sessions: {
    ...state.sessions,
    [key]: {
      ...state.sessions[key],
      key,
      streaming: true,
      runtimeRunId: "run_1",
      messages: [
        { id: "user_1", role: "user", content: "Write the weekly report." },
        { id: "assistant_1", role: "assistant", content: "" },
      ],
      runtimeHumanInput: {
        requestId: "request_1",
        status: "waiting",
        question: "Should the report include live clips?",
        options: ["Include them", "Leave them out"],
      },
    },
  } as ReturnType<typeof useChatStreamStore.getState>["sessions"],
}));

createRoot(document.getElementById("root")!).render(
  <MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <FloatingChat />
  </QueryClientProvider></MemoryRouter>,
);
