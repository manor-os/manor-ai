import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { useAuthStore } from "../stores/auth";

export interface EmailAccount {
  id: string;
  email: string;
  provider: "email" | "gmail" | "outlook";
  is_default: boolean;
}

export interface EmailAccountSettings {
  accounts: EmailAccount[];
  default_sender_id: string | null;
}

export function emailProviderLabel(provider: EmailAccount["provider"]) {
  return provider === "email" ? "SMTP" : provider === "gmail" ? "Gmail" : "Outlook";
}

export function useEmailAccounts(enabled = true) {
  const user = useAuthStore((state) => state.user);
  return useQuery({
    queryKey: ["email-accounts", user?.id, user?.entity_id],
    queryFn: api.emailAccounts.list,
    enabled,
  });
}
