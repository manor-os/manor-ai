import React, { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import Modal from "../../components/ui/Modal";
import Button from "../../components/ui/Button";
import Input from "../../components/ui/Input";
import { useToastStore } from "../../stores/toast";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { getSkillEnvVars } from "./skillTypes";

interface CredentialModalProps {
  skill: any | null;
  open: boolean;
  onClose: () => void;
}

export function CredentialModal({ skill, open, onClose }: CredentialModalProps) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [values, setValues] = useState<Record<string, string>>({});

  useEffect(() => {
    if (skill) {
      setValues((skill.config as any)?.env_var_values || {});
    } else {
      setValues({});
    }
  }, [skill]);

  const saveMutation = useMutation({
    mutationFn: ({ skillId, vals }: { skillId: string; vals: Record<string, string> }) =>
      api.skills.saveCredentials(skillId, vals),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills"] });
      toast.success(t("page.credential_modal.credentials_saved"));
      onClose();
    },
    onError: (e: any) => {
      toast.error(e?.message || t("page.credential_modal.failed_save"));
    },
  });

  const handleClose = () => {
    setValues({});
    onClose();
  };

  const envVars = getSkillEnvVars(skill);

  return (
    <Modal
      open={open && !!skill}
      onClose={handleClose}
      title={`${t("page.credential_modal.configure_credentials")} — ${skill?.name || ""}`}
      footer={
        <>
          <Button variant="outline" onClick={handleClose}>
            {t("action.cancel")}
          </Button>
          <Button
            variant="primary"
            disabled={saveMutation.isPending}
            onClick={() => {
              if (!skill) return;
              saveMutation.mutate({ skillId: skill.id as string, vals: values });
            }}
          >
            {saveMutation.isPending ? t("page.team_people.saving") : t("page.credential_modal.save_credentials")}
          </Button>
        </>
      }
    >
      {skill && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <p style={{ fontSize: 13, color: "#78716c", margin: "0 0 4px" }}>
            {t("page.credential_modal.requires_env_vars")}
          </p>
          {envVars.map((ev: any) => {
            const name = typeof ev === "string" ? ev : ev.name;
            const desc = typeof ev === "object" ? ev.description : "";
            const required =
              typeof ev === "object" ? ev.required !== false : true;
            return (
              <div
                key={name}
                style={{ display: "flex", flexDirection: "column", gap: 6 }}
              >
                <label
                  style={{
                    fontSize: 12,
                    fontWeight: 700,
                    color: "#44403c",
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  {name}
                  {required && (
                    <span style={{ color: "#d65f59", fontSize: 10 }}>*</span>
                  )}
                </label>
                {desc && (
                  <p style={{ fontSize: 11, color: "#a8a29e", margin: 0 }}>
                    {desc}
                  </p>
                )}
                <Input
                  type="password"
                  placeholder={`${t("page.credential_modal.enter_value_for")} ${name}`}
                  value={values[name] || ""}
                  onChange={(e) =>
                    setValues((prev) => ({ ...prev, [name]: e.target.value }))
                  }
                />
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}
