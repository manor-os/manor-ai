import { create } from "zustand";
import { api } from "../lib/api";
import {
  getAuthToken,
} from "../lib/authToken";
import { setPreferredTimeZone } from "../lib/format";
import type { User } from "../lib/types";
import { useChatStreamStore } from "./chatStream";


interface AuthState {
  user: User | null;
  token: string | null;
  isLoading: boolean;
  pendingVerificationEmail: string | null;
  pending2fa: { email: string; password: string; rememberMe?: boolean } | null;
  login: (email: string, password: string, rememberMe?: boolean) => Promise<void>;
  login2fa: (totpCode: string) => Promise<void>;
  register: (
    email: string,
    password: string,
    entityName?: string,
    invitationCode?: string,
    inviteToken?: string,
  ) => Promise<void>;
  acceptInvite: (
    token: string,
    name?: string,
    phone?: string,
  ) => Promise<void>;
  verifyEmail: (email: string, code: string) => Promise<void>;
  resendVerification: (email: string) => Promise<void>;
  switchEntity: (entityId: string) => Promise<User>;
  logout: () => void;
  checkAuth: () => Promise<void>;
}

function clearVolatileUserState() {
  useChatStreamStore.getState().reset();
  localStorage.removeItem("manor_pending_chat_retry");
}

function rememberUser(user: User | null) {
  setPreferredTimeZone(user?.timezone);
  if (user) localStorage.setItem("manor_user", JSON.stringify(user));
  else localStorage.removeItem("manor_user");
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: getAuthToken(),
  isLoading: true,
  pendingVerificationEmail: null,
  pending2fa: null,

  login: async (email, password, rememberMe) => {
    const res: any = await api.auth.login({ email, password, remember_me: rememberMe });
    if (res.requires_verification) {
      set({ pendingVerificationEmail: res.email });
      return;
    }
    if (res.requires_2fa) {
      set({ pending2fa: { email, password, rememberMe } });
      return;
    }
    clearVolatileUserState();
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token });
    const user = await api.auth.me();
    rememberUser(user);
    set({ user });
  },

  login2fa: async (totpCode) => {
    const state = useAuthStore.getState();
    if (!state.pending2fa) throw new Error("No pending 2FA");
    const { email, password, rememberMe } = state.pending2fa;
    const res: any = await api.auth.login({ email, password, remember_me: rememberMe, totp_code: totpCode });
    if (res.requires_2fa) throw new Error("Invalid 2FA code");
    clearVolatileUserState();
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token, pending2fa: null });
    const user = await api.auth.me();
    rememberUser(user);
    set({ user });
  },

  register: async (email, password, entityName, invitationCode, inviteToken) => {
    const registerPayload: {
      email: string;
      password: string;
      entity_name?: string;
      invitation_code?: string;
      invite_token?: string;
    } = {
      email, password,
      entity_name: entityName,
      invitation_code: invitationCode || undefined,
      invite_token: inviteToken || undefined,
    };
    const res: any = await api.auth.register(registerPayload);
    if (res.requires_verification) {
      set({ pendingVerificationEmail: res.email });
      return;
    }
    clearVolatileUserState();
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token });
    const user = await api.auth.me();
    rememberUser(user);
    set({ user });
  },

  acceptInvite: async (token, name, phone) => {
    const res = await api.auth.acceptInvite({
      token,
      name: name || undefined,
      phone: phone || undefined,
    });
    if (res.access_token) {
      localStorage.setItem("manor_token", res.access_token);
      set({ token: res.access_token });
    }
    const user = await api.auth.me();
    rememberUser(user);
    set({ user });
  },

  verifyEmail: async (email, code) => {
    const res = await api.auth.verifyEmail(email, code);
    clearVolatileUserState();
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token, pendingVerificationEmail: null });
    const user = await api.auth.me();
    rememberUser(user);
    set({ user });
  },

  resendVerification: async (email) => {
    await api.auth.resendVerification(email);
  },

  switchEntity: async (entityId) => {
    const res = await api.auth.switchEntity(entityId);
    clearVolatileUserState();
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token });
    const user = await api.auth.me();
    rememberUser(user);
    set({ user });
    return user;
  },

  logout: () => {
    clearVolatileUserState();
    localStorage.removeItem("manor_token");
    rememberUser(null);
    set({ token: null, user: null, pendingVerificationEmail: null });
  },

  checkAuth: async () => {
    const token = getAuthToken();
    if (!token) {
      rememberUser(null);
      set({ isLoading: false });
      return;
    }
    const state = get();
    if (state.user && state.token === token) {
      set({ isLoading: false });
      return;
    }
    try {
      const user = await api.auth.me();
      rememberUser(user);
      set({ user, token, isLoading: false });
    } catch {
      clearVolatileUserState();
      localStorage.removeItem("manor_token");
      rememberUser(null);
      set({ token: null, user: null, isLoading: false });
    }
  },
}));
