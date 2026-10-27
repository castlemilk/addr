import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [new URL("./model.ts", import.meta.url).pathname], bundle: true,
  platform: "node", format: "esm", write: false });
const model = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString("base64")}`);

test("reads structured and text tool responses, and surfaces failed tools", () => {
  assert.deepEqual(model.payload({ content: [], structuredContent: { projects: [] } }), { projects: [] });
  assert.deepEqual(model.payload({ content: [{ type: "text", text: '{"item":{"id":"item-1"}}' }] }), { item: { id: "item-1" } });
  assert.throws(() => model.payload({ isError: true, content: [{ type: "text", text: "Workspace token expired" }] }), /token expired/);
  assert.throws(() => model.payload({ content: [{ type: "text", text: "[]" }] }), /invalid workspace/);
});

test("effective class and saved feedback take precedence over immutable source labels", () => {
  const annotation = { id: "roof-1", labelId: "building", notes: "source notes", properties: { addr: {
    kind: "building", structure_kind: "house", overrides: { kind: "building", label: "Fabric shade sail",
      status: "needs_adjustment", geometry_reviewed: false, notes: "" }, fieldworkReview: { structureKind: "shade-sail" },
  } } };
  assert.deepEqual(model.objectMetadata(annotation, [{ id: "building", name: "Buildings" }]), {
    label: "Fabric shade sail", kind: "building", structureKind: "shade-sail", status: "needs_adjustment", geometryReviewed: false, notes: "",
  });
  assert.equal(model.objectMetadata({ labelId: "tree", properties: { addr: { kind: "tree" } } }, [{ id: "tree", name: "Trees" }]).label, "Trees");
});

test("rejects malformed document identities before creating chat references", () => {
  assert.equal(model.project({ id: "../../secret", name: "Injected" }), undefined);
  assert.equal(model.item({ id: "item-1", projectId: "project?token=x", title: "Injected" }), undefined);
  const value = model.item({ id: "item-1", projectId: "mooroolbark", title: "19 Cambridge Road", revision: 3 });
  assert.equal(model.itemLink(value).uri, "fieldwork://item/item-1");
  assert.equal(model.itemLink(value).description, "Addr CRM item in project mooroolbark · revision 3");
});
