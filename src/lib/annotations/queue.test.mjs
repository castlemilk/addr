import assert from "node:assert/strict";
import { test } from "node:test";
import { collectQueuePage, itemHasTag, mergeQueue, parseQueueSearch, queueCounter, readQueueLink, sharedTags, writeQueueLink, QUEUE_PAGE, TAG_SCAN_PAGE } from "./queue.ts";

// A server list in id order with ListItems' cursor contract: the cursor is opaque; an empty nextCursor ends the list.
function server(items) {
  const calls = [];
  const fetchPage = async (cursor, limit) => {
    calls.push({ cursor, limit });
    const start = cursor ? Number(cursor) : 0;
    const page = items.slice(start, start + limit);
    return { items: page, nextCursor: start + limit < items.length ? String(start + limit) : "" };
  };
  return { fetchPage, calls };
}
const make = (count, tagFor) => Array.from({ length: count }, (_, index) => ({ id: `item-${String(index).padStart(3, "0")}`, tags: tagFor(index) }));

test("a tag:<name> token is split from the title text; a token still being typed is pending, Enter completes it", () => {
  assert.deepEqual(parseQueueSearch("Shadows"), { text: "Shadows", tag: "", pendingTag: "" });
  assert.deepEqual(parseQueueSearch("tag:review-2026-10-08-cuts "), { text: "", tag: "review-2026-10-08-cuts", pendingTag: "" });
  assert.deepEqual(parseQueueSearch("Cuts tag:agent-hint-right"), { text: "Cuts", tag: "", pendingTag: "agent-hint-right" }, "still typing");
  assert.deepEqual(parseQueueSearch("Cuts tag:agent-hint-right", true), { text: "Cuts", tag: "agent-hint-right", pendingTag: "" });
  assert.deepEqual(parseQueueSearch("tag:a tag:b rest "), { text: "rest", tag: "b", pendingTag: "" }, "the last complete token wins");
  assert.deepEqual(parseQueueSearch("tag: "), { text: "", tag: "", pendingTag: "" }, "an empty token is dropped");
});

test("tags match whole, trimmed and case-insensitive", () => {
  assert.equal(itemHasTag({ tags: ["review-2026-10-08-shadows"] }, "review-2026-10-08-shadows"), true);
  assert.equal(itemHasTag({ tags: [" Agent-Hint-Right "] }, "agent-hint-right"), true);
  assert.equal(itemHasTag({ tags: ["review-2026-10-08-shadows"] }, "review-2026-10-08"), false, "no prefix match");
  assert.equal(itemHasTag({ tags: undefined }, "x"), false);
  assert.equal(itemHasTag({ tags: ["x"] }, ""), false);
});

test("an untagged page is one server page of the historic size", async () => {
  const { fetchPage, calls } = server(make(45, () => []));
  const page = await collectQueuePage(fetchPage, "", "");
  assert.equal(page.items.length, QUEUE_PAGE); assert.equal(page.nextCursor, "20");
  assert.deepEqual(calls, [{ cursor: "", limit: QUEUE_PAGE }]);
});

test("a tagged page reads server pages until enough items match, keeps every match it read and resumes without gaps", async () => {
  // 1340 items: every fourth carries the tag, so one TAG_SCAN_PAGE page holds TAG_SCAN_PAGE / 4 matches.
  const all = make(1340, (index) => index % 4 === 0 ? ["review-2026-10-08", "wanted"] : ["review-2026-10-08"]);
  const expected = all.filter((entry) => entry.tags.includes("wanted")).map((entry) => entry.id);
  const { fetchPage, calls } = server(all);
  const first = await collectQueuePage(fetchPage, "", "wanted");
  assert.equal(first.items.length, TAG_SCAN_PAGE / 4, "one page's matches, all kept");
  assert.equal(first.nextCursor, String(TAG_SCAN_PAGE)); assert.equal(first.scanned, TAG_SCAN_PAGE);
  assert.ok(calls.every((call) => call.limit === TAG_SCAN_PAGE));
  let list = first.items, cursor = first.nextCursor;
  while (cursor) { const page = await collectQueuePage(fetchPage, cursor, "wanted"); list = mergeQueue(list, page.items); cursor = page.nextCursor; }
  assert.deepEqual(list.map((entry) => entry.id), expected, "Load more keeps the tag filter and never skips or repeats");
});

test("a sparse tag scans several pages, stops at the page cap or when the caller moved on", async () => {
  const all = make(1500, (index) => index === 1400 ? ["rare"] : []);
  // 2026-10-17: the Splits queue (newest items, last in server order) loaded empty - the cap now applies only after a match
  const capped = await collectQueuePage(server(all).fetchPage, "", "rare", { maxPages: 1 });
  assert.deepEqual(capped.items.map((entry) => entry.id), ["item-1400"], "no match yet: keep reading past the cap");
  assert.equal(capped.nextCursor, "", "the item was on the last page");
  const many = make(1500, (index) => index % 2 ? ["often"] : []);
  const page = await collectQueuePage(server(many).fetchPage, "", "often", { maxPages: 1, want: 10_000 });
  assert.equal(page.scanned, TAG_SCAN_PAGE, "with matches found, the cap stops the scan");
  let current = true;
  const { fetchPage, calls } = server(all);
  await collectQueuePage(async (cursor, limit) => { const page = await fetchPage(cursor, limit); current = false; return page; }, "", "rare", { isCurrent: () => current });
  assert.equal(calls.length, 1, "a superseded search stops after the page in flight");
});

test("mergeQueue appends only unseen ids in order", () => {
  assert.deepEqual(mergeQueue([{ id: "a" }, { id: "b" }], [{ id: "b" }, { id: "c" }, { id: "c" }]).map((entry) => entry.id), ["a", "b", "c"]);
});

test("the footer counter is truthful about unloaded items and items outside the list", () => {
  assert.equal(queueCounter(19, 70, false), "20 of 70");
  assert.equal(queueCounter(19, 20, true), "20 of 20+");
  assert.equal(queueCounter(-1, 20, true), "Not in this list · 20+ loaded");
});

test("deep links read and write project, item, q and tag only, keep other parameters and the hash, and drop empty ones", () => {
  assert.deepEqual(readQueueLink("?project=mooroolbark-review&item=r1008-2-cut-036-p77dbc4b6&q=Cuts&tag=agent-hint-right&token=secret"),
    { project: "mooroolbark-review", item: "r1008-2-cut-036-p77dbc4b6", q: "Cuts", tag: "agent-hint-right" });
  assert.deepEqual(readQueueLink(""), { project: "", item: "", q: "", tag: "" });
  const href = writeQueueLink("https://fieldwork.example/annotations?keep=1&q=old#frag", { project: "p", item: "i 1", q: "", tag: "review-2026-10-08-veg" });
  assert.equal(href, "/annotations?keep=1&project=p&item=i+1&tag=review-2026-10-08-veg#frag");
  assert.deepEqual(readQueueLink(new URL(href, "https://x.example").search), { project: "p", item: "i 1", q: "", tag: "review-2026-10-08-veg" }, "round trip");
  assert.ok(!/token/i.test(writeQueueLink("https://fieldwork.example/", { project: "p", item: "", q: "", tag: "" })));
});

test("tags every listed item carries are shared (left off the row chips); one item shares nothing", () => {
  const items = [{ tags: ["review-2026-10-08", "Review-2026-10-08-shadows", "agent-hint-right"] }, { tags: [" review-2026-10-08 ", "review-2026-10-08-shadows", "agent-hint-wrong"] }];
  assert.deepEqual([...sharedTags(items)].sort(), ["review-2026-10-08", "review-2026-10-08-shadows"]);
  assert.deepEqual([...sharedTags(items.slice(0, 1))], []);
  assert.deepEqual([...sharedTags([{ tags: ["a"] }, {}])], []);
});
