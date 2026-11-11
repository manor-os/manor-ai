import { useAuthStore } from "../stores/auth";

/** End this browser session: a support impersonation goes back to admin. */
export async function leaveSessionOrLogout() {
  await useAuthStore.getState().logout();
  window.location.href = "/login";
}
