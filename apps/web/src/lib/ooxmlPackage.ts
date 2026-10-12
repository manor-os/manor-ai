import type JSZip from "jszip";

/**
 * JSZip records a folder entry ("ppt/charts/") whenever a file is added under a
 * folder that had none. OOXML packages are flat part lists and Office's own
 * files never carry folder entries; strict OPC readers reject them. Drop them
 * right before a package is written.
 */
export function removeZipFolderEntries(zip: JSZip): JSZip {
  for (const name of Object.keys(zip.files)) {
    // zip.remove() on a folder would also delete every part inside it.
    if (zip.files[name].dir) delete zip.files[name];
  }
  return zip;
}
