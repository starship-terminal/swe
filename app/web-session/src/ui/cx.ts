/** Join class names, skipping the falsy ones. */
export function cx(...names: (string | false | null | undefined)[]): string {
  return names.filter((name): name is string => typeof name === "string" && name !== "").join(" ");
}
