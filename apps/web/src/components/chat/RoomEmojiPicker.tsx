import EmojiPicker, { EmojiStyle, Theme, type EmojiClickData } from "emoji-picker-react";
import { getLocale } from "../../lib/i18n";

export default function RoomEmojiPicker({ onSelect }: { onSelect: (emoji: string) => void }) {
  const pick = (selection: EmojiClickData) => onSelect(selection.emoji);
  return <EmojiPicker
    className="room-emoji-picker"
    width="100%"
    height={340}
    emojiStyle={EmojiStyle.NATIVE}
    theme={document.documentElement.dataset.theme === "dark" ? Theme.DARK : Theme.LIGHT}
    reactionsDefaultOpen
    allowExpandReactions
    previewConfig={{ showPreview: false }}
    searchPlaceholder={getLocale() === "zh" ? "搜索表情" : "Search emoji"}
    onReactionClick={pick}
    onEmojiClick={pick}
  />;
}
