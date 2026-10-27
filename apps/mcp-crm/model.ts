import type { CallToolResult, ResourceLink } from "@modelcontextprotocol/sdk/types.js";

export type RecordData = Record<string, unknown>;
export type Project = RecordData & { id: string; name: string };
export type Item = RecordData & { id: string; projectId: string; title: string };
export type Annotation = RecordData & { id: string };
export type Label = RecordData & { id: string; name: string };
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

export function record(value: unknown): RecordData | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as RecordData : undefined;
}

export function records(value: unknown): RecordData[] {
  return Array.isArray(value) ? value.map(record).filter((entry): entry is RecordData => !!entry) : [];
}

export function text(value: unknown, fallback = "—"): string {
  return typeof value === "string" && value.length ? value : typeof value === "number" ? String(value) : fallback;
}

export function payload(result: CallToolResult): RecordData {
  if (result.isError) {
    const message = result.content.find((part) => part.type === "text");
    throw new Error(message?.type === "text" ? message.text : "The request could not finish.");
  }
  if (record(result.structuredContent)) return result.structuredContent!;
  const content = result.content.find((part) => part.type === "text");
  if (content?.type !== "text") throw new Error("The server returned no workspace data.");
  const value = record(JSON.parse(content.text));
  if (!value) throw new Error("The server returned an invalid workspace response.");
  return value;
}

export function project(value: unknown): Project | undefined {
  const data = record(value);
  return data && typeof data.id === "string" && idPattern.test(data.id) && typeof data.name === "string" ? data as Project : undefined;
}

export function item(value: unknown): Item | undefined {
  const data = record(value);
  return data && typeof data.id === "string" && idPattern.test(data.id) && typeof data.projectId === "string" && idPattern.test(data.projectId) && typeof data.title === "string" ? data as Item : undefined;
}

export function objectMetadata(annotation: RecordData, labels: Label[]): RecordData {
  const properties = record(annotation.properties);
  const addr = record(properties?.addr);
  const overrides = record(addr?.overrides);
  const label = labels.find((entry) => entry.id === annotation.labelId);
  const review = record(addr?.fieldworkReview);
  return {
    label: overrides?.label ?? label?.name ?? addr?.label ?? annotation.labelId,
    kind: overrides?.kind ?? addr?.kind,
    structureKind: overrides?.structure_kind ?? review?.structureKind ?? addr?.structure_kind,
    status: overrides?.status ?? properties?.status,
    geometryReviewed: overrides?.geometry_reviewed,
    notes: overrides?.notes ?? annotation.notes,
  };
}

export function itemLink(value: Item): ResourceLink {
  return { type: "resource_link", uri: `fieldwork://item/${value.id}`, name: value.id, title: value.title,
    description: `Addr CRM item in project ${value.projectId} · revision ${text(value.revision)}`, mimeType: "application/json" };
}

export function errorText(error: unknown): string {
  return error instanceof Error && error.message.length <= 800 ? error.message : "The request could not finish. Try again.";
}
