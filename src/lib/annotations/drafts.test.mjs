import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { readDraft, reconcileDraft, writeDraft } from "./drafts.ts";

const records = new Map();
globalThis.window = { location: { origin: "https://fieldwork.example" } };
globalThis.localStorage = {
  getItem: (key) => records.get(key) ?? null,
  setItem: (key, value) => records.set(key, value),
  removeItem: (key) => records.delete(key),
};
globalThis.sessionStorage = globalThis.localStorage;
const saved = { id: "item-1", revision: 4, notes: "", annotations: [] };
const edited = { ...saved, notes: "Review the path" };
beforeEach(() => records.clear());

test("undoing back to the saved item removes the previous unsaved recovery copy", () => {
  reconcileDraft(edited, true, false);
  assert.equal(readDraft(saved.id).item.notes, edited.notes);
  reconcileDraft(saved, false, false);
  assert.equal(readDraft(saved.id), null);
});

test("loading a clean server copy preserves the recovery draft until the user decides", () => {
  writeDraft(edited, saved.revision);
  reconcileDraft(saved, false, true);
  assert.equal(readDraft(saved.id).item.notes, edited.notes);
  assert.equal(readDraft(saved.id).baseRevision, 4);
});

test("the same item id at another service cannot recover or delete a different service's draft", () => {
  reconcileDraft(edited, true, false);
  localStorage.setItem("fieldwork.api.url", "https://another.example");
  assert.equal(readDraft(saved.id), null);
  reconcileDraft(saved, false, false);
  localStorage.removeItem("fieldwork.api.url");
  assert.equal(readDraft(saved.id).item.notes, edited.notes);
});

 test("shared user drafts with identical IDs never recover or delete another workspace",()=>{
  sessionStorage.setItem("fieldwork.draft.workspace","ws_"+"a".repeat(32));writeDraft(edited,saved.revision);
  sessionStorage.setItem("fieldwork.draft.workspace","ws_"+"b".repeat(32));assert.equal(readDraft(saved.id),null);reconcileDraft(saved,false,false);
  sessionStorage.setItem("fieldwork.draft.workspace","legacy");assert.equal(readDraft(saved.id),null);
  sessionStorage.setItem("fieldwork.draft.workspace","ws_"+"a".repeat(32));assert.equal(readDraft(saved.id).item.notes,edited.notes);
 });
