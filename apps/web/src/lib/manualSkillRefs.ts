export type ManualSkillReference = {
  kind: "id" | "slug";
  value: string;
  source?: "builtin" | "entity";
};

export type ManualSkillReferenceSource = {
  id: string;
  reference?: ManualSkillReference;
};

export function manualSkillReference(
  skill: ManualSkillReferenceSource,
): ManualSkillReference {
  return skill.reference || { kind: "id", value: skill.id };
}

export function manualSkillReferences(
  skills: ManualSkillReferenceSource[],
): ManualSkillReference[] {
  return skills.map(manualSkillReference);
}

type ManualSkillLookupItem = {
  id: string;
  slug?: string | null;
  entity_id?: string | null;
};

/** Resolve portable UI references before a chat request crosses the wire. */
export function resolveManualSkillReferenceIds(
  references: ManualSkillReference[],
  availableSkills: ManualSkillLookupItem[],
): ManualSkillReference[] {
  return references.map((reference) => {
    if (reference.kind === "id") return reference;

    const candidates = availableSkills.filter(
      (skill) => skill.slug === reference.value,
    );
    const resolved =
      reference.source === "builtin"
        ? candidates.find((skill) => skill.entity_id == null)
        : reference.source === "entity"
          ? candidates.find((skill) => skill.entity_id != null)
          : candidates.find((skill) => skill.entity_id != null) || candidates[0];
    if (!resolved?.id) {
      throw new Error(`Skill not found or not available: ${reference.value}`);
    }
    return { kind: "id", value: resolved.id };
  });
}

/** IDs are dual-written for API versions that predate typed references. */
export function legacyManualSkillIds(
  references: ManualSkillReference[] | undefined,
): string[] {
  return Array.from(
    new Set(
      (references || [])
        .filter((reference) => reference.kind === "id")
        .map((reference) => reference.value),
    ),
  );
}
