import { useEffect, useMemo, useState } from "react";
import { translateApiError } from "../../lib/api";
import { getPreferredTimeZone } from "../../lib/format";
import {
  initiativeCondition,
  initiativeTrigger,
  scheduleDraftFromTrigger,
  triggerFromScheduleDraft,
  type AgentInitiativeScheduleDraft,
} from "../../lib/agentInitiativeFormat";
import type {
  AgentInitiative,
  AgentInitiativeCreateRequest,
  AgentInitiativeDecisionMode,
} from "../../lib/agentInitiativeTypes";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import DateTimePicker from "../ui/DateTimePicker";
import Input from "../ui/Input";
import Modal from "../ui/Modal";
import RadioCard from "../ui/RadioCard";
import Select from "../ui/Select";
import Textarea from "../ui/Textarea";
import Toggle from "../ui/Toggle";

export interface AgentInitiativeFormScope {
  id: string;
  label: string;
  workspaceId: string | null;
}

export interface AgentInitiativeConversationOption {
  id: string;
  label: string;
}

interface AgentInitiativeFormProps {
  open: boolean;
  initiative?: AgentInitiative | null;
  scope: AgentInitiativeFormScope;
  conversations: AgentInitiativeConversationOption[];
  conversationsLoading?: boolean;
  saving?: boolean;
  error?: unknown;
  onClose: () => void;
  onSubmit: (request: AgentInitiativeCreateRequest) => Promise<void>;
}

interface FormDraft {
  name: string;
  instruction: string;
  semanticCondition: string;
  decisionMode: AgentInitiativeDecisionMode;
  cooldownHours: string;
  conversationId: string;
  notificationEnabled: boolean;
  notificationChannel: "default" | "email";
  notificationChanged: boolean;
  quietMode: "inherit_user" | "custom" | "none";
  quietStart: string;
  quietEnd: string;
  quietTimezone: string;
  schedule: AgentInitiativeScheduleDraft;
}

const STEP_KEYS = ["when", "check", "tell", "deliver"] as const;

function initialDraft(initiative?: AgentInitiative | null): FormDraft {
  const condition = initiative ? initiativeCondition(initiative) : null;
  const notification = initiative?.delivery_policy?.notification;
  const channels = notification?.channels;
  const surface = initiative?.delivery_policy?.surface;
  const quietHours = initiative?.interruption_policy?.quiet_hours;
  return {
    name: initiative?.name || "",
    instruction: initiative?.instruction || "",
    semanticCondition: condition?.semantic_condition || "",
    decisionMode: initiative?.decision_policy?.mode === "on_match"
      ? "on_condition"
      : initiative?.decision_policy?.mode || "on_condition",
    cooldownHours: String(Math.max(0, Math.round((initiative?.decision_policy?.cooldown_seconds ?? 3600) / 3600))),
    conversationId: surface?.conversation_id || "",
    notificationEnabled: notification?.enabled ?? false,
    notificationChannel: Array.isArray(channels) && channels.includes("email") ? "email" : "default",
    notificationChanged: false,
    quietMode: quietHours?.mode === "custom" || quietHours?.mode === "none"
      ? quietHours.mode
      : "inherit_user",
    quietStart: quietHours?.mode === "custom" ? quietHours.start || quietHours.from || "22:00" : "22:00",
    quietEnd: quietHours?.mode === "custom" ? quietHours.end || quietHours.to || "08:00" : "08:00",
    quietTimezone: quietHours?.mode === "custom" ? quietHours.timezone : getPreferredTimeZone(),
    schedule: scheduleDraftFromTrigger(initiative ? initiativeTrigger(initiative) : null),
  };
}

function fieldLabel(key: string): string {
  return t(`page.agent_initiative.${key}`);
}

export default function AgentInitiativeForm({
  open,
  initiative,
  scope,
  conversations,
  conversationsLoading = false,
  saving = false,
  error,
  onClose,
  onSubmit,
}: AgentInitiativeFormProps) {
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<FormDraft>(() => initialDraft(initiative));
  const [validationError, setValidationError] = useState("");

  useEffect(() => {
    if (!open) return;
    setStep(0);
    setDraft(initialDraft(initiative));
    setValidationError("");
  }, [open, initiative?.id, scope.id]);

  useEffect(() => {
    if (!open || conversations.length !== 1) return;
    setDraft((current) => current.conversationId
      ? current
      : { ...current, conversationId: conversations[0].id });
  }, [conversations, open, scope.workspaceId]);

  const conversationOptions = useMemo(() => {
    const options = conversations.map((conversation) => ({
      value: conversation.id,
      label: conversation.label,
    }));
    if (draft.conversationId && !options.some((option) => option.value === draft.conversationId)) {
      options.unshift({
        value: draft.conversationId,
        label: t("page.agent_initiative.current_conversation"),
      });
    }
    return options;
  }, [conversations, draft.conversationId]);

  const setSchedule = (patch: Partial<AgentInitiativeScheduleDraft>) => {
    setDraft((current) => ({
      ...current,
      schedule: { ...current.schedule, ...patch },
    }));
  };

  const validate = (index: number): string => {
    if (index === 0) {
      if (!draft.name.trim()) return t("page.agent_initiative.name_required");
      if (draft.schedule.mode === "interval" && Number(draft.schedule.everyMinutes) < 5) {
        return t("page.agent_initiative.interval_minimum");
      }
      if (draft.schedule.mode === "at") {
        const runAt = new Date(draft.schedule.runAt).getTime();
        if (!draft.schedule.runAt || !Number.isFinite(runAt) || runAt <= Date.now()) {
          return t("page.agent_initiative.future_time_required");
        }
      }
      if (
        (draft.schedule.mode === "daily" || draft.schedule.mode === "weekly")
        && (!/^\d{2}:\d{2}$/.test(draft.schedule.time) || !draft.schedule.timezone.trim())
      ) {
        return t("page.agent_initiative.schedule_required");
      }
      if (draft.schedule.mode === "custom" && !draft.schedule.original) {
        return t("page.agent_initiative.schedule_required");
      }
    }
    if (index === 1 && !draft.instruction.trim()) {
      return t("page.agent_initiative.instruction_required");
    }
    if (index === 3 && !draft.conversationId) {
      return scope.workspaceId
        ? t("page.agent_initiative.workspace_conversation_required")
        : t("page.agent_initiative.conversation_required");
    }
    if (
      index === 3
      && draft.quietMode === "custom"
      && (
        !/^\d{2}:\d{2}$/.test(draft.quietStart)
        || !/^\d{2}:\d{2}$/.test(draft.quietEnd)
        || !draft.quietTimezone.trim()
      )
    ) {
      return t("page.agent_initiative.quiet_hours_required");
    }
    return "";
  };

  const next = () => {
    const message = validate(step);
    if (message) {
      setValidationError(message);
      return;
    }
    setValidationError("");
    setStep((current) => Math.min(STEP_KEYS.length - 1, current + 1));
  };

  const submit = async () => {
    const message = validate(3);
    if (message) {
      setValidationError(message);
      return;
    }
    const cooldownHours = Math.max(0, Number(draft.cooldownHours) || 0);
    const request: AgentInitiativeCreateRequest = {
      name: draft.name.trim(),
      instruction: draft.instruction.trim(),
      trigger: triggerFromScheduleDraft(draft.schedule),
      condition: {
        version: "1",
        semantic_condition: draft.semanticCondition.trim() || null,
      },
      decision_policy: {
        version: "1",
        mode: draft.decisionMode,
        cooldown_seconds: Math.round(cooldownHours * 3600),
        max_notifications_per_day:
          initiative?.decision_policy?.max_notifications_per_day
          ?? initiative?.decision_policy?.max_notifications_per_24h
          ?? 24,
      },
      delivery_policy: {
        version: "1",
        surface: scope.workspaceId
          ? { kind: "workspace_chat", conversation_id: draft.conversationId }
          : { kind: "conversation", conversation_id: draft.conversationId },
        notification: !draft.notificationChanged
          && initiative?.delivery_policy?.notification
          && Array.isArray(initiative.delivery_policy.notification.channels)
          && initiative.delivery_policy.notification.channels.length > 0
          ? initiative.delivery_policy.notification
          : {
            enabled: draft.notificationEnabled,
            recipient: "owner",
            channels: draft.notificationChannel === "email" ? ["email"] : ["inapp"],
            content_mode: "summary_and_link",
          },
      },
      interruption_policy: {
        version: "1",
        quiet_hours: draft.quietMode === "custom"
          ? {
            mode: "custom",
            start: draft.quietStart,
            end: draft.quietEnd,
            timezone: draft.quietTimezone.trim(),
          }
          : { mode: draft.quietMode },
        during_quiet_hours: "suppress_external",
      },
    };
    await onSubmit(request);
  };

  const scheduleOptions = [
    { value: "daily", label: t("page.agent_initiative.schedule_daily") },
    { value: "weekly", label: t("page.agent_initiative.schedule_weekly") },
    { value: "interval", label: t("page.agent_initiative.schedule_interval") },
    { value: "at", label: t("page.agent_initiative.schedule_once") },
    ...(draft.schedule.mode === "custom"
      ? [{ value: "custom", label: t("page.agent_initiative.schedule_existing") }]
      : []),
  ];
  const weekdays = [
    "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
  ].map((day, index) => ({ value: String(index), label: fieldLabel(day) }));

  const footer = (
    <div className="flex w-full flex-wrap items-center justify-between gap-2">
      <Button variant="ghost" onClick={onClose} disabled={saving}>
        {t("action.cancel")}
      </Button>
      <div className="flex items-center gap-2">
        {step > 0 ? (
          <Button
            variant="outline"
            onClick={() => {
              setValidationError("");
              setStep((current) => current - 1);
            }}
            disabled={saving}
          >
            {t("page.agent_initiative.back")}
          </Button>
        ) : null}
        {step < STEP_KEYS.length - 1 ? (
          <Button variant="primary" onClick={next} disabled={saving}>
            {t("page.agent_initiative.next")}
          </Button>
        ) : (
          <Button variant="primary" onClick={submit} loading={saving}>
            {initiative ? t("page.agent_initiative.save_changes") : t("page.agent_initiative.create")}
          </Button>
        )}
      </div>
    </div>
  );

  return (
    <Modal
      open={open}
      onClose={() => { if (!saving) onClose(); }}
      title={initiative ? t("page.agent_initiative.edit_title") : t("page.agent_initiative.create_title")}
      maxWidth="680px"
      footer={footer}
    >
      <ol className="mb-5 grid grid-cols-4 gap-2" aria-label={t("page.agent_initiative.form_steps")}>
        {STEP_KEYS.map((key, index) => (
          <li
            key={key}
            aria-current={index === step ? "step" : undefined}
            className={`rounded-lg px-2 py-2 text-center text-[11px] font-semibold ${
              index === step ? "bg-stone-900 text-white" : "bg-stone-100 text-stone-500"
            }`}
          >
            <span className="font-mono">{index + 1}</span>
            <span className="ml-1 hidden sm:inline">{fieldLabel(`step_${key}`)}</span>
          </li>
        ))}
      </ol>

      {step === 0 ? (
        <div className="space-y-4">
          <div>
            <h3 className="m-0 text-base font-bold text-stone-900">{t("page.agent_initiative.step_when")}</h3>
            <p className="mt-1 text-sm leading-relaxed text-stone-500">{t("page.agent_initiative.step_when_description")}</p>
          </div>
          <Input
            label={t("page.agent_initiative.name")}
            value={draft.name}
            onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))}
            placeholder={t("page.agent_initiative.name_placeholder")}
            maxLength={255}
            autoFocus
          />
          <div>
            <label className="mb-1.5 block text-xs font-semibold text-stone-500">
              {t("page.agent_initiative.schedule")}
            </label>
            <Select
              value={draft.schedule.mode}
              onChange={(value) => setSchedule({ mode: value as AgentInitiativeScheduleDraft["mode"] })}
              options={scheduleOptions}
              ariaLabel={t("page.agent_initiative.schedule")}
            />
          </div>
          {(draft.schedule.mode === "daily" || draft.schedule.mode === "weekly") ? (
            <div className="grid gap-4 sm:grid-cols-2">
              {draft.schedule.mode === "weekly" ? (
                <div>
                  <label className="mb-1.5 block text-xs font-semibold text-stone-500">
                    {t("page.agent_initiative.weekday")}
                  </label>
                  <Select
                    value={draft.schedule.weekday}
                    onChange={(value) => setSchedule({ weekday: value })}
                    options={weekdays}
                    ariaLabel={t("page.agent_initiative.weekday")}
                  />
                </div>
              ) : <div />}
              <div>
                <label className="mb-1.5 block text-xs font-semibold text-stone-500">
                  {t("page.agent_initiative.time")}
                </label>
                <DateTimePicker
                  mode="time"
                  value={draft.schedule.time}
                  onChange={(value) => setSchedule({ time: value })}
                  placeholder={t("page.agent_initiative.pick_time")}
                />
              </div>
              <Input
                className="sm:col-span-2"
                label={t("page.agent_initiative.timezone")}
                value={draft.schedule.timezone}
                onChange={(event) => setSchedule({ timezone: event.target.value })}
                placeholder="America/Los_Angeles"
              />
            </div>
          ) : null}
          {draft.schedule.mode === "interval" ? (
            <Input
              label={t("page.agent_initiative.interval_minutes")}
              type="number"
              min={5}
              step={1}
              value={draft.schedule.everyMinutes}
              onChange={(event) => setSchedule({ everyMinutes: event.target.value })}
              hint={t("page.agent_initiative.interval_hint")}
            />
          ) : null}
          {draft.schedule.mode === "at" ? (
            <div>
              <label className="mb-1.5 block text-xs font-semibold text-stone-500">
                {t("page.agent_initiative.run_at")}
              </label>
              <DateTimePicker
                mode="datetime"
                value={draft.schedule.runAt}
                onChange={(value) => setSchedule({ runAt: value })}
                placeholder={t("page.agent_initiative.pick_date_and_time")}
              />
            </div>
          ) : null}
          {draft.schedule.mode === "custom" ? (
            <p className="rounded-xl bg-stone-100 px-3 py-2 text-xs leading-relaxed text-stone-600">
              {t("page.agent_initiative.custom_schedule_preserved")}
            </p>
          ) : null}
        </div>
      ) : null}

      {step === 1 ? (
        <div className="space-y-4">
          <div>
            <h3 className="m-0 text-base font-bold text-stone-900">{t("page.agent_initiative.step_check")}</h3>
            <p className="mt-1 text-sm leading-relaxed text-stone-500">{t("page.agent_initiative.step_check_description")}</p>
          </div>
          <Textarea
            label={t("page.agent_initiative.instruction")}
            value={draft.instruction}
            onChange={(event) => setDraft((current) => ({ ...current, instruction: event.target.value }))}
            placeholder={t("page.agent_initiative.instruction_placeholder")}
            rows={5}
          />
          <Textarea
            label={t("page.agent_initiative.condition")}
            value={draft.semanticCondition}
            onChange={(event) => setDraft((current) => ({ ...current, semanticCondition: event.target.value }))}
            placeholder={t("page.agent_initiative.condition_placeholder")}
            rows={3}
          />
        </div>
      ) : null}

      {step === 2 ? (
        <div className="space-y-4">
          <div>
            <h3 className="m-0 text-base font-bold text-stone-900">{t("page.agent_initiative.step_tell")}</h3>
            <p className="mt-1 text-sm leading-relaxed text-stone-500">{t("page.agent_initiative.step_tell_description")}</p>
          </div>
          <div role="radiogroup" aria-label={t("page.agent_initiative.decision_mode")} className="space-y-2">
            {(["always", "on_condition", "on_change"] as AgentInitiativeDecisionMode[]).map((mode) => (
              <RadioCard
                key={mode}
                selected={draft.decisionMode === mode}
                onSelect={() => setDraft((current) => ({ ...current, decisionMode: mode }))}
                ariaLabel={fieldLabel(`decision_${mode}`)}
              >
                <span>
                  <span className="block text-sm font-semibold text-stone-800">{fieldLabel(`decision_${mode}`)}</span>
                  <span className="mt-0.5 block text-xs leading-relaxed text-stone-500">{fieldLabel(`decision_${mode}_description`)}</span>
                </span>
              </RadioCard>
            ))}
          </div>
          <Input
            label={t("page.agent_initiative.cooldown_hours")}
            type="number"
            min={0}
            step={1}
            value={draft.cooldownHours}
            onChange={(event) => setDraft((current) => ({ ...current, cooldownHours: event.target.value }))}
            hint={t("page.agent_initiative.cooldown_hint")}
          />
        </div>
      ) : null}

      {step === 3 ? (
        <div className="space-y-4">
          <div>
            <h3 className="m-0 text-base font-bold text-stone-900">{t("page.agent_initiative.step_deliver")}</h3>
            <p className="mt-1 text-sm leading-relaxed text-stone-500">{t("page.agent_initiative.step_deliver_description")}</p>
          </div>
          <div className="rounded-xl bg-stone-100/80 p-3">
            <p className="m-0 text-xs font-semibold text-stone-500">
              {t("page.agent_initiative.agent_instance")}
            </p>
            <p className="mt-1 text-sm font-semibold text-stone-800">{scope.label}</p>
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-semibold text-stone-500">
              {t("page.agent_initiative.chat_destination")}
            </label>
            <Select
              value={draft.conversationId}
              onChange={(value) => setDraft((current) => ({ ...current, conversationId: value }))}
              options={conversationOptions}
              placeholder={conversationsLoading
                ? t("page.agent_initiative.loading_conversations")
                : scope.workspaceId
                  ? t("page.agent_initiative.choose_workspace_conversation")
                  : t("page.agent_initiative.choose_conversation")}
              disabled={conversationsLoading || conversationOptions.length === 0}
              ariaLabel={t("page.agent_initiative.chat_destination")}
            />
            {!conversationsLoading && conversationOptions.length === 0 ? (
              <p className="mt-2 text-xs leading-relaxed text-amber-700" role="status">
                {scope.workspaceId
                  ? t("page.agent_initiative.no_workspace_conversation_hint")
                  : t("page.agent_initiative.no_conversation_hint")}
              </p>
            ) : null}
          </div>
          <div className="flex items-start justify-between gap-4 rounded-xl bg-stone-100/80 p-3">
            <div>
              <p className="m-0 text-sm font-semibold text-stone-800">{t("page.agent_initiative.external_notification")}</p>
              <p className="mt-1 text-xs leading-relaxed text-stone-500">{t("page.agent_initiative.external_notification_description")}</p>
            </div>
            <Toggle
              checked={draft.notificationEnabled}
              onChange={() => setDraft((current) => ({
                ...current,
                notificationEnabled: !current.notificationEnabled,
                notificationChanged: true,
              }))}
              aria-label={t("page.agent_initiative.external_notification")}
            />
          </div>
          {draft.notificationEnabled ? (
            <div>
              <label className="mb-1.5 block text-xs font-semibold text-stone-500">
                {t("page.agent_initiative.notification_channel")}
              </label>
              <Select
                value={draft.notificationChannel}
                onChange={(value) => setDraft((current) => ({
                  ...current,
                  notificationChannel: value as FormDraft["notificationChannel"],
                  notificationChanged: true,
                }))}
                options={[
                  { value: "default", label: t("page.agent_initiative.default_channels") },
                  { value: "email", label: t("page.agent_initiative.email") },
                ]}
                ariaLabel={t("page.agent_initiative.notification_channel")}
              />
            </div>
          ) : null}
          <div className="space-y-3 rounded-xl bg-stone-100/80 p-3">
            <div>
              <label className="mb-1.5 block text-xs font-semibold text-stone-500">
                {t("page.agent_initiative.quiet_hours")}
              </label>
              <Select
                value={draft.quietMode}
                onChange={(value) => setDraft((current) => ({
                  ...current,
                  quietMode: value as FormDraft["quietMode"],
                }))}
                options={[
                  { value: "inherit_user", label: t("page.agent_initiative.quiet_hours_inherit") },
                  { value: "custom", label: t("page.agent_initiative.quiet_hours_custom") },
                  { value: "none", label: t("page.agent_initiative.quiet_hours_none") },
                ]}
                ariaLabel={t("page.agent_initiative.quiet_hours")}
              />
            </div>
            {draft.quietMode === "custom" ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label className="mb-1.5 block text-xs font-semibold text-stone-500">
                    {t("page.agent_initiative.quiet_hours_start")}
                  </label>
                  <DateTimePicker
                    mode="time"
                    value={draft.quietStart}
                    onChange={(value) => setDraft((current) => ({ ...current, quietStart: value }))}
                    placeholder={t("page.agent_initiative.pick_time")}
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-semibold text-stone-500">
                    {t("page.agent_initiative.quiet_hours_end")}
                  </label>
                  <DateTimePicker
                    mode="time"
                    value={draft.quietEnd}
                    onChange={(value) => setDraft((current) => ({ ...current, quietEnd: value }))}
                    placeholder={t("page.agent_initiative.pick_time")}
                  />
                </div>
                <Input
                  className="sm:col-span-2"
                  label={t("page.agent_initiative.timezone")}
                  value={draft.quietTimezone}
                  onChange={(event) => setDraft((current) => ({
                    ...current,
                    quietTimezone: event.target.value,
                  }))}
                  placeholder="America/Los_Angeles"
                />
              </div>
            ) : null}
            <p className="m-0 text-xs leading-relaxed text-stone-500">
              {draft.quietMode === "inherit_user"
                ? t("page.agent_initiative.quiet_hours_inherited")
                : draft.quietMode === "none"
                  ? t("page.agent_initiative.quiet_hours_none_description")
                  : t("page.agent_initiative.quiet_hours_description")}
            </p>
          </div>
        </div>
      ) : null}

      {validationError ? (
        <p className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
          {validationError}
        </p>
      ) : null}
      {error ? (
        <p className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
          {translateApiError(error, t("page.agent_initiative.save_failed"))}
        </p>
      ) : null}
    </Modal>
  );
}
