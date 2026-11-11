// A deliberately small Markdown reader for the checkpoint REVIEW.md reports (docs/roof-eyes.md).
// parse() yields a flat block list; render() turns it into React elements with no HTML injection:
// text is always a React text node, so "<script>" renders as the six characters. Links are limited to
// http(s) URLs (rel="noreferrer") and internal /scenes paths; repo-relative paths become <code>.
// Headings are demoted by one level by default so the page keeps its own h1 (the object of work).
import { createElement, type ReactElement, type ReactNode } from "react";

export type Block =
  | { type: "h1" | "h2"; text: string }
  | { type: "p"; text: string }
  | { type: "ul"; items: string[] }
  | { type: "table"; header: string[]; rows: string[][] }
  | { type: "code"; text: string };

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*]\s+(.*)$/;
const FENCE = /^\s*```/;
const SEPARATOR_CELL = /^:?-{3,}:?$/;
const isPipeRow = (line: string) => line.trim().startsWith("|") && line.trim().endsWith("|") && line.includes("|");
const cells = (line: string) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
const isSeparator = (line: string | undefined) => line !== undefined && isPipeRow(line) && cells(line).every(c => SEPARATOR_CELL.test(c));

export function parse(markdown: string): Block[] {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (FENCE.test(line)) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !FENCE.test(lines[i])) code.push(lines[i++]);
      i++;
      blocks.push({ type: "code", text: code.join("\n") });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ type: heading[1].length === 1 ? "h1" : "h2", text: heading[2].trim() });
      i++;
      continue;
    }
    if (BULLET.test(line)) {
      const items: string[] = [];
      while (i < lines.length && BULLET.test(lines[i])) items.push(BULLET.exec(lines[i++])![1].trim());
      blocks.push({ type: "ul", items });
      continue;
    }
    if (isPipeRow(line) && isSeparator(lines[i + 1])) {
      const header = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && isPipeRow(lines[i])) {
        const row = cells(lines[i++]);
        rows.push(Array.from({ length: header.length }, (_, k) => row[k] ?? ""));
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }
    if (line.trim() === "") { i++; continue; }
    const paragraph: string[] = [];
    while (i < lines.length && lines[i].trim() !== "" && !HEADING.test(lines[i]) && !BULLET.test(lines[i]) && !FENCE.test(lines[i])
      && !(isPipeRow(lines[i]) && isSeparator(lines[i + 1]))) paragraph.push(lines[i++].trim());
    blocks.push({ type: "p", text: paragraph.join(" ") });
  }
  return blocks;
}

// --- Inline rendering ------------------------------------------------------------------------------------------

const INLINE = new RegExp([
  "`([^`]+)`",                                        // 1 code span
  "\\*\\*([^*]+)\\*\\*",                              // 2 bold
  "\\[([^\\]]+)\\]\\(([^)\\s]+)\\)",                  // 3 text, 4 target
  "(https?:\\/\\/[^\\s<>()\\[\\]]+)",                 // 5 bare URL
  "(\\/scenes(?:[/?][^\\s<>()\\[\\]]*)?)(?![\\w/?])", // 6 bare internal link
  "(?<![\\w/.])((?:data|docs|scripts|src)\\/[\\w./-]+)", // 7 bare repo path
].join("|"), "g");
const TRAILING = /[.,;:!?]+$/;
const isHttp = (target: string) => /^https?:\/\//i.test(target);
const isInternal = (target: string) => /^\/scenes(?:$|[/?])/.test(target) && !/\/\.\.?(?:$|[/?])/.test(target);
const untrail = (value: string): [string, string] => { const tail = TRAILING.exec(value)?.[0] ?? ""; return [value.slice(0, value.length - tail.length), tail]; };
const link = (text: string, target: string, key: string): ReactNode =>
  isHttp(target) ? createElement("a", { key, href: target, rel: "noreferrer" }, text)
    : isInternal(target) ? createElement("a", { key, href: target }, text)
      : createElement("code", { key }, text);

export function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0, n = 0;
  for (const match of text.matchAll(INLINE)) {
    const index = match.index ?? 0;
    if (index > last) out.push(text.slice(last, index));
    const key = `i${n++}`;
    if (match[1] !== undefined) out.push(createElement("code", { key }, match[1]));
    else if (match[2] !== undefined) out.push(createElement("strong", { key }, match[2]));
    else if (match[3] !== undefined) out.push(link(match[3], match[4], key));
    else if (match[5] !== undefined || match[6] !== undefined) {
      const [target, tail] = untrail(match[5] ?? match[6]);
      out.push(link(target, target, key));
      if (tail) out.push(tail);
    } else if (match[7] !== undefined) {
      const [path, tail] = untrail(match[7]);
      out.push(createElement("code", { key }, path));
      if (tail) out.push(tail);
    }
    last = index + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// --- Block rendering -------------------------------------------------------------------------------------------

export interface RenderOptions {
  /** Added to every heading level; 1 keeps the page's h1 unique (h1 -> h2, h2 -> h3). */
  headingOffset?: number;
}
export function render(blocks: Block[], options: RenderOptions = {}): ReactElement[] {
  const offset = options.headingOffset ?? 1;
  const heading = (level: number) => `h${Math.min(6, Math.max(1, level + offset))}`;
  return blocks.map((block, index) => {
    const key = `b${index}`;
    switch (block.type) {
      case "h1": return createElement(heading(1), { key }, ...inline(block.text));
      case "h2": return createElement(heading(2), { key }, ...inline(block.text));
      case "p": return createElement("p", { key }, ...inline(block.text));
      case "ul": return createElement("ul", { key }, ...block.items.map((item, k) => createElement("li", { key: `l${k}` }, ...inline(item))));
      case "code": return createElement("pre", { key }, createElement("code", null, block.text));
      case "table": return createElement("table", { key },
        createElement("thead", null, createElement("tr", null, ...block.header.map((cell, k) => createElement("th", { key: `h${k}` }, ...inline(cell))))),
        createElement("tbody", null, ...block.rows.map((row, r) => createElement("tr", { key: `r${r}` }, ...row.map((cell, k) => createElement("td", { key: `c${k}` }, ...inline(cell)))))));
    }
  });
}
