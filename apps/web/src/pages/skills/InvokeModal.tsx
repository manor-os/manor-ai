import React, { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import Modal from "../../components/ui/Modal";
import Button from "../../components/ui/Button";
import Textarea from "../../components/ui/Textarea";
import LoadingSpinner from "../../components/ui/LoadingSpinner";
import { useToastStore } from "../../stores/toast";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { getSkillDescription } from "./skillTypes";

interface InvokeModalProps {
  skill: any | null;
  open: boolean;
  onClose: () => void;
}

export function InvokeModal({ skill, open, onClose }: InvokeModalProps) {
  const toast = useToastStore();
  const [invokeInput, setInvokeInput] = useState("");
  const [invokeFormValues, setInvokeFormValues] = useState<
    Record<string, string>
  >({});
  const [invokeResult, setInvokeResult] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setInvokeInput("");
      setInvokeFormValues({});
      setInvokeResult(null);
    }
  }, [open, skill?.id]);

  const invokeMutation = useMutation({
    mutationFn: ({ id, input }: { id: string; input: string | Record<string, unknown> }) =>
      api.skills.invoke(id, input),
    onSuccess: (data: any) => {
      const out =
        typeof data === "string"
          ? data
          : data?.output ?? data?.result ?? JSON.stringify(data, null, 2);
      setInvokeResult(out);
    },
    onError: (e: any) => {
      toast.error(e?.message || t("page.invoke_modal.invocation_failed"));
    },
  });

  if (!skill) return null;

  const schema = skill.input_schema as
    | {
        type?: string;
        required?: string[];
        properties?: Record<string, any>;
      }
    | undefined;
  const props =
    schema?.properties && typeof schema.properties === "object"
      ? schema.properties
      : null;
  const useForm = !!(props && Object.keys(props).length > 0);
  const requiredKeys = new Set<string>(schema?.required || []);
  const description = getSkillDescription(skill);

  const submit = () => {
    if (useForm) {
      const payload: Record<string, unknown> = {};
      for (const [key, fieldSchema] of Object.entries(
        props as Record<string, any>,
      )) {
        const raw = (invokeFormValues[key] || "").trim();
        if (!raw) continue;
        const fType = (fieldSchema as any)?.type;
        if (fType === "number" || fType === "integer") {
          const n = Number(raw);
          if (!Number.isNaN(n)) payload[key] = n;
        } else if (fType === "array") {
          payload[key] = raw
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
        } else if (fType === "boolean") {
          payload[key] = raw.toLowerCase() === "true";
        } else {
          payload[key] = raw;
        }
      }
      invokeMutation.mutate({ id: skill.id as string, input: payload });
    } else {
      invokeMutation.mutate({ id: skill.id as string, input: invokeInput });
    }
  };

  const canSubmit = useForm
    ? Array.from(requiredKeys).every((k) => (invokeFormValues[k] || "").trim())
    : !!invokeInput.trim();

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`${t("page.invoke_modal.test")}: ${skill.name || ""}`}
      maxWidth="32rem"
      footer={
        <Button variant="outline" onClick={onClose}>
          {t("page.flows.close")}
        </Button>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {!!description && (
          <p
            style={{ fontSize: 12, color: "#a8a29e", fontWeight: 500, margin: 0 }}
          >
            {description}
          </p>
        )}

        {useForm ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {Object.entries(props as Record<string, any>).map(
              ([key, fieldSchema]) => {
                const fSchema = fieldSchema as {
                  type?: string;
                  description?: string;
                };
                const isRequired = requiredKeys.has(key);
                const isLong =
                  (fSchema.description || "").length > 60 ||
                  fSchema.type === "array";
                return (
                  <div
                    key={key}
                    style={{ display: "flex", flexDirection: "column", gap: 4 }}
                  >
                    <label
                      style={{
                        fontSize: 11,
                        fontWeight: 700,
                        color: "#57534e",
                        textTransform: "uppercase" as const,
                        letterSpacing: "0.04em",
                      }}
                    >
                      {key}
                      {isRequired && (
                        <span style={{ color: "#c14a44" }}> *</span>
                      )}
                      <span
                        style={{
                          marginLeft: 6,
                          fontWeight: 500,
                          color: "#a8a29e",
                          textTransform: "none" as const,
                          letterSpacing: 0,
                        }}
                      >
                        {fSchema.type === "array"
                          ? t("page.invoke_modal.list_csv")
                          : `(${fSchema.type || t("page.invoke_modal.string")})`}
                      </span>
                    </label>
                    {isLong ? (
                      <textarea
                        value={invokeFormValues[key] || ""}
                        onChange={(e) =>
                          setInvokeFormValues((v) => ({
                            ...v,
                            [key]: e.target.value,
                          }))
                        }
                        rows={2}
                        placeholder={fSchema.description || ""}
                        style={{
                          fontSize: 12,
                          padding: 8,
                          border: "1px solid rgba(28,25,23,0.06)",
                          borderRadius: 6,
                          fontFamily: "inherit",
                          resize: "vertical",
                        }}
                      />
                    ) : (
                      <input
                        type="text"
                        value={invokeFormValues[key] || ""}
                        onChange={(e) =>
                          setInvokeFormValues((v) => ({
                            ...v,
                            [key]: e.target.value,
                          }))
                        }
                        placeholder={fSchema.description || ""}
                        style={{
                          fontSize: 13,
                          padding: "6px 10px",
                          border: "1px solid rgba(28,25,23,0.06)",
                          borderRadius: 6,
                        }}
                      />
                    )}
                    {fSchema.description && !isLong && (
                      <span
                        style={{ fontSize: 11, color: "#a8a29e", lineHeight: 1.4 }}
                      >
                        {fSchema.description}
                      </span>
                    )}
                  </div>
                );
              },
            )}
          </div>
        ) : (
          <Textarea
            label={t("page.invoke_modal.test_message")}
            value={invokeInput}
            onChange={(e) => setInvokeInput(e.target.value)}
            rows={4}
            placeholder={t("page.invoke_modal.test_message_placeholder")}
          />
        )}

        <Button
          variant="primary"
          onClick={submit}
          disabled={!canSubmit || invokeMutation.isPending}
        >
          {invokeMutation.isPending ? (
            <span
              style={{ display: "flex", alignItems: "center", gap: 8 }}
            >
              <LoadingSpinner size={14} />
              {t("page.invoke_modal.running")}
            </span>
          ) : (
            t("page.invoke_modal.run_skill")
          )}
        </Button>

        {invokeResult !== null && (
          <div>
            <label
              style={{
                display: "block",
                fontSize: 12,
                fontWeight: 700,
                color: "#57534e",
                marginBottom: 6,
                textTransform: "uppercase" as const,
                letterSpacing: "0.05em",
              }}
            >
              {t("page.invoke_modal.response")}
            </label>
            <pre
              style={{
                width: "100%",
                padding: 14,
                background: "#fafaf9",
                border: "1px solid #f5f5f4",
                borderRadius: 12,
                fontSize: 12,
                fontFamily: "monospace",
                color: "#57534e",
                overflow: "auto",
                maxHeight: 200,
                whiteSpace: "pre-wrap",
                margin: 0,
              }}
            >
              {invokeResult}
            </pre>
          </div>
        )}
      </div>
    </Modal>
  );
}
