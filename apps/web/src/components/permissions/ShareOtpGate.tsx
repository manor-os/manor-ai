import { FormEvent, useState } from "react";
import Button from "../ui/Button";
import Input from "../ui/Input";
import { t } from "../../lib/i18n";

interface ShareOtpGateProps {
  endpoint: string;
  onVerified: () => void;
}

export default function ShareOtpGate({ endpoint, onVerified }: ShareOtpGateProps) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"email" | "code">("email");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const suffix = step === "email" ? "request-otp" : "verify-otp";
      const response = await fetch(`${endpoint}/${suffix}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(step === "email" ? { email } : { email, code }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = body?.detail;
        throw new Error(
          typeof detail === "string"
            ? detail
            : detail?.message || t("page.shared.verification_error"),
        );
      }
      if (step === "email") {
        setStep("code");
      } else if (typeof body?.access_token === "string") {
        onVerified();
      } else {
        throw new Error(t("page.shared.verification_error"));
      }
    } catch (err: any) {
      setError(err?.message || t("page.shared.verification_error"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      style={{
        maxWidth: 420,
        margin: "28px auto 12px",
        padding: 20,
        borderRadius: "var(--radius-card)",
        background: "var(--surface-muted)",
        boxShadow: "var(--shadow-sm)",
      }}
    >
      <h2 style={{ margin: "0 0 6px", color: "var(--text-strong)", fontSize: 18 }}>
        {t("page.shared.verify_title")}
      </h2>
      <p style={{ margin: "0 0 18px", color: "var(--text-muted)", fontSize: 13, lineHeight: 1.5 }}>
        {step === "email"
          ? t("page.shared.verify_email_hint")
          : t("page.shared.verify_code_hint", { email })}
      </p>
      <Input
        label={step === "email" ? t("page.shared.email") : t("page.shared.code")}
        value={step === "email" ? email : code}
        onChange={(event) => step === "email" ? setEmail(event.target.value) : setCode(event.target.value)}
        type={step === "email" ? "email" : "text"}
        autoComplete={step === "email" ? "email" : "one-time-code"}
        inputMode={step === "email" ? "email" : "numeric"}
        maxLength={step === "email" ? 320 : 6}
        error={error}
        required
        autoFocus
      />
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16, flexWrap: "wrap" }}>
        {step === "code" && (
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => { setStep("email"); setCode(""); setError(undefined); }}
          >
            {t("page.shared.change_email")}
          </Button>
        )}
        <Button type="submit" loading={busy} disabled={step === "email" ? !email : code.length !== 6}>
          {step === "email" ? t("page.shared.send_code") : t("page.shared.verify")}
        </Button>
      </div>
    </form>
  );
}
