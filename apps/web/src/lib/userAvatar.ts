/** Old generated initials use a hash as the name, so they can show unrelated letters. */
export function userAvatarImage(avatarUrl?: string | null): string {
  const src = (avatarUrl || "").trim();
  try {
    const url = new URL(src);
    if (url.hostname === "api.dicebear.com" && /^\/\d+\.x\/initials\/svg\/?$/.test(url.pathname)) {
      return "";
    }
  } catch { /* Relative URLs, data images, and an empty avatar remain valid inputs. */ }
  return src;
}
