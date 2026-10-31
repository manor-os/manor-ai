// First: whether this is the embedded customer-trial frame. Its memory storage
// was installed before any module by public/embed-isolate.js (index.html).
import { EMBED_REFUSED } from "./embed/embedContext";
import { renderEmbedNotice } from "./embed/embedNotice";
import { initializeLocale } from "./lib/i18n";
import { renderBootstrapFailure } from "./lib/bootstrapFailure";
import "./index.css";

if (EMBED_REFUSED) renderEmbedNotice("root");
else void initializeLocale()
  .then(() => import("./bootstrap"))
  .catch((error) => renderBootstrapFailure("root", "Manor", error));
