import type { ComponentProps } from "react";
import ChatModeTemplateGallery from "./ChatModeTemplateGallery";
import { chatModeTemplateSamples } from "./EmbeddedChat";

// The sample library lives with the full chat surface. Load it only when the
// floating composer needs templates, keeping that surface out of the shell.
export default function FloatingChatModeTemplateGallery({
  mode: chatMode,
  ...props
}: Omit<ComponentProps<typeof ChatModeTemplateGallery>, "samples">) {
  return <ChatModeTemplateGallery {...props} mode={chatMode} samples={chatModeTemplateSamples(chatMode)} />;
}
