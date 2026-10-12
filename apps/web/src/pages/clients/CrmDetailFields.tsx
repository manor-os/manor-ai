import type { ReactNode } from "react";

export default function CrmDetailFields({ fields }: { fields: { label: string; value?: ReactNode; mono?: boolean }[] }) {
  return (
    <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      {fields.map(field => (
        <div key={field.label} className="min-w-0">
          <dt className="text-xs text-stone-500">{field.label}</dt>
          <dd className={`mt-1 break-words text-sm ${field.mono ? "font-mono tabular-nums" : ""}`}>
            {field.value === null || field.value === undefined || field.value === "" ? "—" : field.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
