import { useState, useCallback } from "react";
import PageHeader from "../components/ui/PageHeader";
import { t } from "../lib/i18n";

const SIZE_OPTIONS = [128, 256, 512] as const;

function buildQRUrl(text: string, size: number): string {
  return `https://api.qrserver.com/v1/create-qr-code/?size=${size}x${size}&data=${encodeURIComponent(text)}`;
}

export default function QRCode() {
  const [text, setText] = useState("");
  const [size, setSize] = useState<number>(256);
  const [generated, setGenerated] = useState<{ text: string; size: number } | null>(null);
  const [copied, setCopied] = useState(false);

  const handleGenerate = useCallback(() => {
    if (!text.trim()) return;
    setGenerated({ text: text.trim(), size });
  }, [text, size]);

  const handleDownload = useCallback(async () => {
    if (!generated) return;
    try {
      const res = await fetch(buildQRUrl(generated.text, generated.size));
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `qrcode-${generated.size}px.png`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      window.open(buildQRUrl(generated.text, generated.size), "_blank");
    }
  }, [generated]);

  const handleCopy = useCallback(() => {
    if (!text.trim()) return;
    navigator.clipboard.writeText(text.trim());
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [text]);

  return (
    <div className="max-w-3xl mx-auto animate-fade-in">
      <PageHeader
        title={t("page.qr.title")}
        subtitle={t("page.qr.subtitle")}
      />

      <div className="glass-panel" style={{ padding: 24 }}>
        {/* URL / Text input */}
        <div style={{ marginBottom: 20 }}>
          <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
            {t("page.qr.url_or_text")}
          </label>
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t("page.qr.url_placeholder")}
            className="manor-input"
            onKeyDown={(e) => e.key === "Enter" && handleGenerate()}
          />
        </div>

        {/* Size selector */}
        <div style={{ marginBottom: 20 }}>
          <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>
            {t("page.qr.size")}
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            {SIZE_OPTIONS.map((s) => (
              <button
                key={s}
                onClick={() => setSize(s)}
                style={{
                  padding: "6px 16px",
                  fontSize: 13,
                  fontWeight: 600,
                  borderRadius: 10,
                  border: size === s ? "1px solid #4f7d75" : "1px solid rgba(231,229,228,0.6)",
                  background: size === s ? "rgba(79,125,117,0.1)" : "rgba(255,255,255,0.6)",
                  color: size === s ? "#436b65" : "#57534e",
                  cursor: "pointer",
                  transition: "all 0.15s",
                }}
              >
                {s}{t("page.qr.px")}
              </button>
            ))}
          </div>
        </div>

        {/* Actions */}
        <div style={{ display: "flex", gap: 12 }}>
          <button
            onClick={handleGenerate}
            disabled={!text.trim()}
            className="btn-manor"
            style={{ opacity: !text.trim() ? 0.4 : 1, display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 4.875c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5A1.125 1.125 0 013.75 9.375v-4.5zM3.75 14.625c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5a1.125 1.125 0 01-1.125-1.125v-4.5zM13.5 4.875c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5a1.125 1.125 0 01-1.125-1.125v-4.5z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 14.625c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5a1.125 1.125 0 01-1.125-1.125v-4.5z" />
            </svg>
            {t("page.qr.generate")}
          </button>
          <button
            onClick={handleCopy}
            disabled={!text.trim()}
            className="btn-manor-outline"
            style={{ opacity: !text.trim() ? 0.4 : 1, display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 7.5V6.108c0-1.135.845-2.098 1.976-2.192.373-.03.748-.057 1.123-.08M15.75 18H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08M15.75 18.75v-1.875a3.375 3.375 0 00-3.375-3.375h-1.5a1.125 1.125 0 01-1.125-1.125v-1.5A3.375 3.375 0 006.375 7.5H5.25m11.9-3.664A2.251 2.251 0 0015 2.25h-1.5a2.251 2.251 0 00-2.15 1.586m5.8 0c.065.21.1.433.1.664v.75h-6V4.5c0-.231.035-.454.1-.664M6.75 7.5H4.875c-.621 0-1.125.504-1.125 1.125v12c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V16.5a9 9 0 00-9-9z" />
            </svg>
            {copied ? t("page.qr.copied") : t("page.qr.copy_link")}
          </button>
        </div>
      </div>

      {/* QR Code display */}
      {generated && (
        <div
          className="glass-panel animate-fade-in"
          style={{
            marginTop: 24,
            padding: 32,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 16,
          }}
        >
          <div style={{
            borderRadius: 16,
            background: "rgba(255,255,255,0.6)",
            border: "1px solid rgba(28,25,23,0.06)",
            padding: 16,
          }}>
            <img
              src={buildQRUrl(generated.text, generated.size)}
              alt={t("page.qr.generated_alt")}
              width={generated.size}
              height={generated.size}
              style={{ borderRadius: 8 }}
            />
          </div>
          <p style={{ fontSize: 12, color: "#a8a29e", maxWidth: 380, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", textAlign: "center" }}>
            {generated.text}
          </p>
          <div style={{ display: "flex", gap: 12 }}>
            <button onClick={handleDownload} className="btn-manor" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
              </svg>
              {t("page.qr.download_png")}
            </button>
            <button onClick={handleCopy} className="btn-manor-outline" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m9.86-2.702a4.5 4.5 0 00-1.242-7.244l-4.5-4.5a4.5 4.5 0 00-6.364 6.364L5.25 9" />
              </svg>
              {t("page.qr.copy_url")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
