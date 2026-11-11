// A delivered file set that comes grouped (a release package: start here, materials, press, rights, audio).
export type DeliveryFile = {
  key: string;
  name: string;
  group?: string | null;
  role?: string | null;
};

export type DeliveryGroup<T extends DeliveryFile> = { group: string; files: T[] };

// Roles that read first when a delivery names one: its overview, not the first file in the list.
const START_ROLES = ["release_overview", "overview", "readme"];

// Files grouped in the order their group first appears, the opening file's group first;
// files without a group are left out.
export function groupDeliveryFiles<T extends DeliveryFile>(files: T[]): DeliveryGroup<T>[] {
  const groups = new Map<string, T[]>();
  for (const file of files) {
    const group = file.group?.trim();
    if (!group) continue;
    groups.set(group, [...(groups.get(group) || []), file]);
  }
  const lead = openingDeliveryFile(files)?.group?.trim();
  const sections = [...groups].map(([group, grouped]) => ({ group, files: grouped }));
  return lead ? sections.sort((a, b) => Number(b.group === lead) - Number(a.group === lead)) : sections;
}

// The file to open first: the overview when the delivery names one, else the first grouped file.
export function openingDeliveryFile<T extends DeliveryFile>(files: T[]): T | null {
  const grouped = files.filter((file) => file.group?.trim());
  return grouped.find((file) => file.role && START_ROLES.includes(file.role)) || grouped[0] || null;
}
