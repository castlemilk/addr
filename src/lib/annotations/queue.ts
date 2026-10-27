import type { Item } from "./model";

/** Items per ListItems call for an untagged queue (the workspace's historic page size). */
export const QUEUE_PAGE = 20;
/** Items per ListItems call while a tag filter is active. ListItems takes projectId, cursor, query (a literal title substring),
 *  status, adapter and limit, and has no tag filter (internal/annotation/service.go, the live 27 Sep build included), so a tagged
 *  queue is read page by page and filtered here; larger pages keep that to a few calls for a few hundred items. */
export const TAG_SCAN_PAGE = 100;  // the server filters by tag (ListItems tag, 2026-10-17); the local filter stays as a guard
/** One tagged load stops after this many server pages once at least one item matched (with no match yet it reads on to the end, so a
 *  queue whose items sort last never opens empty); Load more continues. */
export const TAG_SCAN_MAX_PAGES = 10;
/** A deep link looks this many pages ahead for its item before opening it on its own. */
export const LINK_SEEK_PAGES = 15;

export interface QueueSearch { text: string; tag: string; pendingTag: string }
/** Splits the queue search box into its title text and a `tag:<name>` token. A token still being typed (the last word, with no
 *  space after it) is `pendingTag` and never part of `text`; `final` (Enter) completes it. The last complete token wins. */
export function parseQueueSearch(input: string, final = false): QueueSearch {
  const words = input.split(/\s+/).filter(Boolean);
  const open = !final && !/\s$/.test(input);
  let tag = "", pendingTag = "";
  const rest: string[] = [];
  words.forEach((word, index) => {
    const match = /^tag:(.*)$/i.exec(word);
    if (!match) { rest.push(word); return; }
    if (open && index === words.length - 1) pendingTag = match[1];
    else if (match[1]) tag = match[1];
  });
  return { text: rest.join(" "), tag, pendingTag };
}

export const normaliseTag = (tag: string) => tag.trim().toLowerCase();
export function itemHasTag(item: Pick<Item, "tags">, tag: string) {
  const wanted = normaliseTag(tag);
  return !!wanted && (item.tags ?? []).some((value) => normaliseTag(value) === wanted);
}

export interface QueuePage { items: Item[]; nextCursor: string; scanned: number }
export type FetchQueuePage = (cursor: string, limit: number) => Promise<{ items?: Item[]; nextCursor?: string }>;
/** One queue page from `cursor`. Without a tag it is one server page. With a tag it reads server pages until at least `want`
 *  items carry the tag, the server cursor ends, `maxPages` were read or `isCurrent` turns false; every match on a read page is
 *  kept and `nextCursor` is the server's cursor after the last page read, so Load more never skips or repeats an item. */
export async function collectQueuePage(fetchPage: FetchQueuePage, cursor: string, tag: string, options: { want?: number; maxPages?: number; isCurrent?: () => boolean } = {}): Promise<QueuePage> {
  if (!tag) {
    const page = await fetchPage(cursor, QUEUE_PAGE);
    return { items: page.items ?? [], nextCursor: page.nextCursor ?? "", scanned: page.items?.length ?? 0 };
  }
  const want = options.want ?? QUEUE_PAGE, maxPages = options.maxPages ?? TAG_SCAN_MAX_PAGES;
  const items: Item[] = [];
  let next = cursor, scanned = 0, pages = 0;
  do {
    const page = await fetchPage(next, TAG_SCAN_PAGE);
    pages++; scanned += page.items?.length ?? 0;
    items.push(...(page.items ?? []).filter((entry) => itemHasTag(entry, tag)));
    next = page.nextCursor ?? "";
  } while (next && items.length < want && (pages < maxPages || !items.length) && (options.isCurrent?.() ?? true));
  return { items, nextCursor: next, scanned };
}
/** The tags (normalised) every one of two or more items carries, e.g. a review queue's shared set tag; a row chip for one of them
 *  tells the items apart no better than the list heading does. */
export function sharedTags(items: Pick<Item, "tags">[]): Set<string> {
  if (items.length < 2) return new Set();
  const [first, ...rest] = items.map((entry) => new Set((entry.tags ?? []).map(normaliseTag).filter(Boolean)));
  return new Set([...first].filter((tag) => rest.every((tags) => tags.has(tag))));
}
/** Appends `more` to `current`, skipping ids already listed (server order is kept). */
export function mergeQueue<T extends { id: string }>(current: T[], more: T[]): T[] {
  const seen = new Set(current.map((entry) => entry.id));
  return [...current, ...more.filter((entry) => !seen.has(entry.id) && !!seen.add(entry.id))];
}
/** The footer counter: "20 of 70" once the list is complete, "20 of 20+" while the server has more. */
export function queueCounter(index: number, loaded: number, more: boolean) {
  const total = `${loaded}${more ? "+" : ""}`;
  return index >= 0 ? `${index + 1} of ${total}` : `Not in this list · ${total} loaded`;
}

export interface QueueLink { project: string; item: string; q: string; tag: string }
export const QUEUE_LINK_KEYS = ["project", "item", "q", "tag"] as const;
/** Reads the deep-link parameters (?project=&item=&q=&tag=). Nothing else is read; a token never travels in the URL. */
export function readQueueLink(search: string): QueueLink {
  const params = new URLSearchParams(search);
  const value = (key: string) => (params.get(key) ?? "").trim().slice(0, 300);
  return { project: value("project"), item: value("item"), q: value("q"), tag: value("tag") };
}
/** `href` with the four deep-link parameters set (empty ones removed) and every other parameter and the hash kept, as a
 *  path-relative URL for history.replaceState. */
export function writeQueueLink(href: string, link: QueueLink): string {
  const url = new URL(href);
  for (const key of QUEUE_LINK_KEYS) {
    const value = link[key].trim();
    if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
