/** Puts text on the clipboard; true when it got there. A page without the Clipboard API
 *  (an insecure origin, an older browser) copies through a hidden text box instead. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "");
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.appendChild(box);
      box.select();
      const copied = document.execCommand("copy");
      document.body.removeChild(box);
      return copied;
    } catch {
      return false;
    }
  }
}
