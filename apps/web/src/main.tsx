import { initializeLocale } from "./lib/i18n";
import { renderBootstrapFailure } from "./lib/bootstrapFailure";
import "./index.css";

void initializeLocale()
  .then(() => import("./bootstrap"))
  .catch((error) => renderBootstrapFailure("root", "Manor", error));
