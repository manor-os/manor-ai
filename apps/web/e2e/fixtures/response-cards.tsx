import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import AssistantMessageBlocks from "../../src/components/AssistantMessageBlocks";
import { setLocale } from "../../src/lib/i18n";
import { RESPONSE_CARD_SAMPLES } from "./response-card-samples";
import "../../src/index.css";

await setLocale("en");
const card = (id: string, title: string, template: string, props: Record<string, unknown>) => ({
  id, type: "surface", version: 1, title, fallback_markdown: `${title} as text`,
  render: { kind: "template", template_id: template, template_version: 1, props },
  display: { preferred: "inline", inline_height: 360, focusable: false }, actions: [],
});
const blocks = [
  ...RESPONSE_CARD_SAMPLES.map((sample, index) => card(`card-${index}`, sample.name, sample.template, sample.props)),
  card("broken", "Broken card", "card.email", { subject: "No content" }),
];

createRoot(document.getElementById("root")!).render(
  <MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <main style={{ maxWidth: 760, margin: "0 auto", padding: 16 }}>
      <AssistantMessageBlocks blocks={blocks as never} keyPrefix="cards" sourceMessageId="message_1" hideProcess />
      <section id="ordered">
        {/* The tool ran before the reply's words were written. */}
        <AssistantMessageBlocks keyPrefix="ordered" sourceMessageId="message_2" hideProcess blocks={[
          card("ordered-card", "Ordered card", "card.delivery", { items: [{ label: "Email", status: "sent" }] }),
          { id: "ordered-text", type: "text", phase: "final", text: "It went out to everyone." },
        ] as never} />
      </section>
    </main>
  </QueryClientProvider></MemoryRouter>,
);
