import type { ImgHTMLAttributes } from "react";
import { useProtectedMediaUrl } from "../../lib/useProtectedMediaUrl";

/** A picture named in configuration (an App cover, a choice's picture), wherever it is stored. */
export default function ConfiguredImage({ src, ...props }: Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> & { src?: string | null }) {
  const media = useProtectedMediaUrl(src);
  if (!media.src) return null;
  return <img {...props} src={media.src} />;
}
