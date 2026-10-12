import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { authPrincipalKey, getAuthToken } from "../../lib/authToken";
import { t } from "../../lib/i18n";
import type { Document } from "../../lib/types";
import Button from "./Button";
import Input from "./Input";
import Modal from "./Modal";

const PAGE_SIZE = 30;

/** Selection belongs to this input card, never to a global chat event. */
export default function HitlKnowledgePicker({ selectedIds, onToggle, onClose, restoreFocus, acceptDocument }: {
  selectedIds: Set<string>;
  onToggle: (document: Document) => void;
  onClose: () => void;
  restoreFocus: () => void;
  acceptDocument?: (document: Document) => boolean;
}) {
  const [search, setSearch] = useState("");
  const documents = useInfiniteQuery({
    queryKey: ["documents", "hitl-picker", authPrincipalKey(getAuthToken()), search.trim()],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.documents.list({
      search: search.trim() || undefined,
      include_generated_assets: true,
      limit: PAGE_SIZE,
      offset: pageParam,
    }),
    getNextPageParam: (lastPage, pages) => {
      const count = pages.reduce((total, page) => total + page.items.length, 0);
      return lastPage.items.length > 0 && count < lastPage.total ? count : undefined;
    },
  });
  const items = (documents.data?.pages.flatMap((page) => page.items) || [])
    .filter((document) => !acceptDocument || acceptDocument(document));

  return (
    <Modal
      open
      onClose={onClose}
      restoreFocusFallback={restoreFocus}
      title={t("component.chat_input_footer.add_from_knowledge_base")}
      maxWidth="520px"
      bodyClassName="chat-hitl-knowledge-body"
      footer={<Button onClick={onClose}>{t("action.done")}</Button>}
    >
      <Input
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder={t("component.chat_input_footer.search_knowledge_base")}
        ariaLabel={t("component.chat_input_footer.search_knowledge_base")}
        autoFocus
      />
      <div className="chat-hitl-knowledge-list" aria-busy={documents.isFetching}>
        {documents.isPending && <p role="status">{t("status.loading")}</p>}
        {!documents.isPending && !documents.isError && items.length === 0 && !documents.hasNextPage && (
          <p role="status">{t("component.chat_input_footer.no_documents_found")}</p>
        )}
        {items.map((document) => (
          <button
            key={document.id}
            type="button"
            className="chat-hitl-knowledge-item"
            aria-pressed={selectedIds.has(document.id)}
            onClick={() => onToggle(document)}
          >
            <span className="chat-hitl-knowledge-name">{document.name}</span>
            <span className="chat-hitl-knowledge-type">{document.file_type || document.mime_type}</span>
            <span className="chat-hitl-knowledge-check" aria-hidden="true">{selectedIds.has(document.id) ? "✓" : ""}</span>
          </button>
        ))}
        {documents.isError && (
          <div className="chat-hitl-knowledge-error" role="alert">
            <p>{t("component.chat_action_card.knowledge_load_failed")}</p>
            <Button variant="ghost" onClick={() => {
              if (documents.isFetchNextPageError) void documents.fetchNextPage();
              else void documents.refetch();
            }}>{t("page.dashboard.retry")}</Button>
          </div>
        )}
        {documents.hasNextPage && !documents.isError && (
          <Button variant="ghost" loading={documents.isFetchingNextPage} onClick={() => void documents.fetchNextPage()}>
            {t("webchat_page.load_more_documents")}
          </Button>
        )}
      </div>
    </Modal>
  );
}
