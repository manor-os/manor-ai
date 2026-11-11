import { createContext } from "react";
import type { FocusComposerDetail } from "../../lib/recommendedOptions";

/** Which message's card this is, so a typed reply can say what it answers. */
export const CardReplyContext = createContext<FocusComposerDetail>({});
