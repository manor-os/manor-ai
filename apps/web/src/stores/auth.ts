import { accessTokenExpired, restorePersistentSession } from "../lib/persistentSession";
import { create } from "zustand";
import { api, ApiError } from "../lib/api";
import {
  clearAuthBrowserState,
  getAuthToken,
  isSameAuthIdentity,
} from "../lib/authToken";
import { setPreferredTimeZone } from "../lib/format";
import { normalizeLocale, setLocaleIfCurrent, type Locale } from "../lib/i18n";
import type { User } from "../lib/types";
import { useChatStreamStore } from "./chatStream";
import { useDisplayPreferences } from "./displayPreferences";


interface AuthState {
  user: User | null;
  token: string | null;
  isLoading: boolean;
  pendingVerificationEmail: string | null;
  pendingVerificationRememberMe: boolean;
  pending2fa: { email: string; password: string; rememberMe?: boolean } | null;
  login: (email: string, password: string, rememberMe?: boolean) => Promise<void>;
  login2fa: (totpCode: string) => Promise<void>;
  register: (
    email: string,
    password: string,
    entityName?: string,
    invitationCode?: string,
    inviteToken?: string,
    chatInviteToken?: string,
  ) => Promise<void>;
  acceptInvite: (
    token: string,
    name?: string,
    phone?: string,
  ) => Promise<void>;
  verifyEmail: (email: string, code: string) => Promise<void>;
  resendVerification: (email: string) => Promise<void>;
  switchEntity: (entityId: string) => Promise<User>;
  renewSession: () => Promise<void>;
  updateLocale: (locale: Locale) => Promise<User>;
  logout: () => Promise<void>;
  checkAuth: () => Promise<void>;
}

function clearVolatileUserState() {
  useChatStreamStore.getState().reset();
  localStorage.removeItem("manor_pending_chat_retry");
}

function forgetUser() {
  setPreferredTimeZone(undefined);
  localStorage.removeItem("manor_user");
}

let localeUpdateVersion = 0;

async function rememberUser(user: User, isCurrent: () => boolean, throwOnLocaleError = false): Promise<boolean> {
  if (user.locale) {
    try {
      if (!await setLocaleIfCurrent(normalizeLocale(user.locale), isCurrent)) return false;
    } catch {
      if (throwOnLocaleError) throw new Error("Could not load the selected language");
      if (!isCurrent()) return false;
    }
  }
  if (!isCurrent()) return false;
  useDisplayPreferences.getState().initializeForUser(user.created_at);
  setPreferredTimeZone(user.timezone);
  localStorage.setItem("manor_user", JSON.stringify(user));
  return true;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: getAuthToken(),
  isLoading: true,
  pendingVerificationEmail: null,
  pendingVerificationRememberMe: false,
  pending2fa: null,

  login: async (email, password, rememberMe) => {
    const res: any = await api.auth.login({ email, password, remember_me: rememberMe });
    if (res.requires_verification) {
      set({ pendingVerificationEmail: res.email, pendingVerificationRememberMe: Boolean(rememberMe) });
      return;
    }
    if (res.requires_2fa) {
      set({ pending2fa: { email, password, rememberMe } });
      return;
    }
    clearVolatileUserState();
    localStorage.removeItem("manor_session_signed_out");
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token });
    const user = await api.auth.me();
    const isCurrentSession = () => isSameAuthIdentity(res.access_token, getAuthToken())
      && isSameAuthIdentity(res.access_token, get().token);
    if (!await rememberUser(user, isCurrentSession) || !isCurrentSession()) return;
    set({ user });
  },

  login2fa: async (totpCode) => {
    const state = useAuthStore.getState();
    if (!state.pending2fa) throw new Error("No pending 2FA");
    const { email, password, rememberMe } = state.pending2fa;
    const res: any = await api.auth.login({ email, password, remember_me: rememberMe, totp_code: totpCode });
    if (res.requires_2fa) throw new Error("Invalid 2FA code");
    clearVolatileUserState();
    localStorage.removeItem("manor_session_signed_out");
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token, pending2fa: null });
    const user = await api.auth.me();
    const isCurrentSession = () => isSameAuthIdentity(res.access_token, getAuthToken())
      && isSameAuthIdentity(res.access_token, get().token);
    if (!await rememberUser(user, isCurrentSession) || !isCurrentSession()) return;
    set({ user });
  },

  register: async (email, password, entityName, invitationCode, inviteToken, chatInviteToken) => {
    const registerPayload: {
      email: string;
      password: string;
      entity_name?: string;
      invitation_code?: string;
      invite_token?: string;
      chat_invite_token?: string;
    } = {
      email, password,
      entity_name: entityName,
      invitation_code: invitationCode || undefined,
      invite_token: inviteToken || undefined,
      chat_invite_token: chatInviteToken || undefined,
    };
    const res: any = await api.auth.register(registerPayload);
    if (res.requires_verification) {
      set({ pendingVerificationEmail: res.email, pendingVerificationRememberMe: false });
      return;
    }
    clearVolatileUserState();
    localStorage.removeItem("manor_session_signed_out");
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token });
    const user = await api.auth.me();
    const isCurrentSession = () => isSameAuthIdentity(res.access_token, getAuthToken())
      && isSameAuthIdentity(res.access_token, get().token);
    if (!await rememberUser(user, isCurrentSession) || !isCurrentSession()) return;
    set({ user });
  },

  acceptInvite: async (token, name, phone) => {
    const res = await api.auth.acceptInvite({
      token,
      name: name || undefined,
      phone: phone || undefined,
    });
    if (res.access_token) {
      localStorage.removeItem("manor_session_signed_out");
      localStorage.setItem("manor_token", res.access_token);
      set({ token: res.access_token });
    }
    const user = await api.auth.me();
    const sessionToken = getAuthToken();
    const isCurrentSession = () => isSameAuthIdentity(sessionToken, getAuthToken())
      && isSameAuthIdentity(sessionToken, get().token);
    if (!await rememberUser(user, isCurrentSession) || !isCurrentSession()) return;
    set({ user });
  },

  verifyEmail: async (email, code) => {
    const res = await api.auth.verifyEmail(email, code, get().pendingVerificationRememberMe);
    clearVolatileUserState();
    localStorage.removeItem("manor_session_signed_out");
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token, pendingVerificationEmail: null, pendingVerificationRememberMe: false });
    const user = await api.auth.me();
    const isCurrentSession = () => isSameAuthIdentity(res.access_token, getAuthToken())
      && isSameAuthIdentity(res.access_token, get().token);
    if (!await rememberUser(user, isCurrentSession) || !isCurrentSession()) return;
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
    const isCurrentSession = () => isSameAuthIdentity(res.access_token, getAuthToken())
      && isSameAuthIdentity(res.access_token, get().token);
    if (!await rememberUser(user, isCurrentSession) || !isCurrentSession()) return user;
    set({ user });
    return user;
  },

  renewSession: async () => {
    const token = getAuthToken();
    if (!token) {
      await get().checkAuth();
      return;
    }
    const res = await api.auth.renew();
    // Logout, account switching, or another tab's renewal owns the newer state.
    if (getAuthToken() !== token || !isSameAuthIdentity(token, get().token)) return;
    localStorage.setItem("manor_token", res.access_token);
    set({ token: res.access_token });
  },

  updateLocale: async (locale) => {
    const currentUser = get().user;
    const currentToken = getAuthToken();
    const updateVersion = ++localeUpdateVersion;
    const isCurrentUser = () => Boolean(currentUser
      && get().user?.id === currentUser.id
      && get().user?.entity_id === currentUser.entity_id
      && isSameAuthIdentity(currentToken, getAuthToken())
      && isSameAuthIdentity(currentToken, get().token));
    const user = await api.auth.updateProfile({ locale });
    if (!isCurrentUser()) {
      throw new Error("The signed-in account changed");
    }
    if (updateVersion !== localeUpdateVersion) return get().user!;
    const canCommit = () => updateVersion === localeUpdateVersion && isCurrentUser();
    if (!await rememberUser(user, canCommit, true)) {
      if (isCurrentUser()) return get().user!;
      throw new Error("The signed-in account changed");
    }
    set({ user });
    return user;
  },

  logout: async () => {
    clearVolatileUserState();
    localStorage.setItem("manor_session_signed_out", "1");
    const logoutRequest = api.auth.logout();
    // Revoke local authority before waiting for best-effort server cleanup.
    // This also makes in-flight profile or locale work fail its session fence.
    set({ token: null, user: null, pendingVerificationEmail: null });
    clearAuthBrowserState();
    forgetUser();
    try {
      await logoutRequest;
    } catch {
      // Local cleanup must still complete if the token already expired or the
      // network is unavailable. The local signed-out marker prevents cookie
      // recovery until the next explicit sign-in succeeds.
    }
  },

  checkAuth: async () => {
    let token = getAuthToken();
    if (!token || accessTokenExpired(token)) {
      const before = token;
      const generation = localStorage.getItem("manor_auth_generation");
      try {
        const restored = await restorePersistentSession();
        if (!restored && (getAuthToken() !== before || generation !== localStorage.getItem("manor_auth_generation"))) return;
        token = restored ? getAuthToken() : null;
      } catch {
        // A network outage does not delete an existing recoverable session.
        set({ isLoading: false });
        return;
      }
    }
    if (!token) {
      clearVolatileUserState();
      clearAuthBrowserState();
      forgetUser();
      set({ token: null, user: null, isLoading: false, pendingVerificationEmail: null, pending2fa: null });
      return;
    }
    if (get().user && isSameAuthIdentity(get().token, token)) {
      set({ token, isLoading: false });
      return;
    }
    if (!isSameAuthIdentity(get().token, token)) {
      clearVolatileUserState();
      forgetUser();
      set({ token, user: null, isLoading: true, pendingVerificationEmail: null, pending2fa: null });
    }
    const state = get();
    try {
      const user = await api.auth.me();
      const currentToken = getAuthToken();
      if (!isSameAuthIdentity(token, currentToken) || get().user !== state.user) return;
      const isCurrentSession = () => isSameAuthIdentity(token, getAuthToken()) && get().user === state.user;
      if (!await rememberUser(user, isCurrentSession) || !isCurrentSession()) return;
      if (!isSameAuthIdentity(token, getAuthToken()) || get().user !== state.user) return;
      set({ user, token: currentToken, isLoading: false });
    } catch (error) {
      if (!isSameAuthIdentity(token, getAuthToken()) || get().user !== state.user) return;
      if (error instanceof ApiError && (error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500)) {
        set({ isLoading: false });
        return;
      }
      clearVolatileUserState();
      clearAuthBrowserState();
      setPreferredTimeZone(undefined);
      set({ token: null, user: null, isLoading: false });
    }
  },
}));
