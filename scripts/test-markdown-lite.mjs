// markdown-lite: the checkpoint REVIEW.md renderer. Headings, paragraphs, bullet lists, pipe tables
// and fenced code become React elements; only http(s) URLs and /scenes links become anchors,
// repo paths become <code>, and everything else is escaped text (never dangerouslySetInnerHTML).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const source = readFileSync('src/lib/markdown-lite.ts', 'utf8');
assert(!source.includes('dangerouslySetInnerHTML') && !source.includes('innerHTML'), 'the renderer never injects HTML');
const exports = {};
new Function('exports', 'require', ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText)(exports, id => require(id));
const { parse, render } = exports;
const html = (markdown, options) => renderToStaticMarkup(React.createElement(React.Fragment, null, ...render(parse(markdown), options)));

// Blocks.
let blocks = parse('# Title\n\nCheckpoint `CP2` | v1\n\n## Section\n\nOne line\nsame paragraph.\n\n- first\n- second `x`\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\n```\nraw <code>\n```\n');
assert.deepEqual(blocks.map(b => b.type), ['h1', 'p', 'h2', 'p', 'ul', 'table', 'code']);
assert.equal(blocks[0].text, 'Title');
assert.equal(blocks[3].text, 'One line same paragraph.');
assert.deepEqual(blocks[4].items, ['first', 'second `x`']);
assert.deepEqual(blocks[5].header, ['a', 'b']); assert.deepEqual(blocks[5].rows, [['1', '2'], ['3', '4']]);
assert.equal(blocks[6].text, 'raw <code>');
assert.deepEqual(parse('### deep\n#### deeper'), [{ type: 'h2', text: 'deep' }, { type: 'h2', text: 'deeper' }], 'deeper headings flatten to h2');
assert.deepEqual(parse(''), []);
assert.deepEqual(parse('\n\n  \n'), []);
assert.deepEqual(parse('| only | header |\nno separator'), [{ type: 'p', text: '| only | header | no separator' }], 'a pipe line without a separator row is a paragraph');
assert.deepEqual(parse('```\nunterminated'), [{ type: 'code', text: 'unterminated' }]);
assert.deepEqual(parse('* star\n* list'), [{ type: 'ul', items: ['star', 'list'] }]);
assert.deepEqual(parse('- [ ] `z17:1:2` [s/o](/scenes?scene=s&object=o) 63 m2'), [{ type: 'ul', items: ['[ ] `z17:1:2` [s/o](/scenes?scene=s&object=o) 63 m2'] }]);

// Headings demote by one level by default so the page keeps its own h1.
let out = html('# Title\n\n## Section');
assert.equal(out, '<h2>Title</h2><h3>Section</h3>');
assert.equal(html('# Title', { headingOffset: 0 }), '<h1>Title</h1>');
assert.equal(html('## S', { headingOffset: 5 }), '<h6>S</h6>', 'heading levels never exceed h6');

// Tables.
out = html('| band | group | review_url |\n| --- | --- | --- |\n| low | z17:1:2 | /scenes?scene=s&object=o&kind=building |\n| high | z17:3:4 | none |');
assert.match(out, /^<table><thead><tr><th>band<\/th><th>group<\/th><th>review_url<\/th><\/tr><\/thead><tbody><tr><td>low<\/td><td>z17:1:2<\/td><td><a href="\/scenes\?scene=s&amp;object=o&amp;kind=building">\/scenes\?scene=s&amp;object=o&amp;kind=building<\/a><\/td><\/tr><tr><td>high<\/td>/);
assert.match(out, /<td>none<\/td><\/tr><\/tbody><\/table>$/);
out = html('| a | b | c |\n| --- | --- | --- |\n| 1 | 2 |');
assert.equal((out.match(/<td>/g) || []).length, 3, 'short rows are padded to the header width');
assert.match(out, /<td><\/td>/);

// Inline links and code.
out = html('See https://example.com/a?b=1. Then [the map](https://example.com/map) and [queue](/scenes?scene=x&object=y).');
assert.match(out, /<a href="https:\/\/example.com\/a\?b=1" rel="noreferrer">https:\/\/example.com\/a\?b=1<\/a>\./, 'bare URL keeps the trailing full stop outside');
assert.match(out, /<a href="https:\/\/example.com\/map" rel="noreferrer">the map<\/a>/);
assert.match(out, /<a href="\/scenes\?scene=x&amp;object=y">queue<\/a>/);
assert(!/rel="noreferrer">queue/.test(out), 'internal links are ordinary anchors');
out = html('Report data/references/eyes-checkpoints/CP1/REVIEW.md and docs/roof-eyes.md, plus [spec](docs/superpowers/specs/x.md) and `scene_confidence.query()`.');
assert.match(out, /<code>data\/references\/eyes-checkpoints\/CP1\/REVIEW.md<\/code> and <code>docs\/roof-eyes.md<\/code>, plus <code>spec<\/code> and <code>scene_confidence.query\(\)<\/code>\./);
out = html('Scan mooroolbark-50011498-17-118442-80417 hidden at /scenes?scene=mooroolbark-50011498-17-118442-80417&object=44c724fd3480c167&kind=building, IoU 0.90.');
assert.match(out, /<a href="\/scenes\?scene=mooroolbark-50011498-17-118442-80417&amp;object=44c724fd3480c167&amp;kind=building">/);
assert.match(out, /<\/a>, IoU 0.90\./);
assert.equal(html('**bold** text'), '<p><strong>bold</strong> text</p>');
for (const scheme of ['javascript:alert(1)', 'ftp://x', 'file:///etc/passwd', 'data:text/html,x']) {
  out = html(`[click](${scheme})`);
  assert(!out.includes('href'), `${scheme} never becomes a link: ${out}`);
  assert.match(out, /<code>click<\/code>/);
}
assert.equal(html('[x](//evil.example)'), '<p><code>x</code></p>', 'protocol-relative URLs are not internal links');
assert.equal(html('[x](/scenes/../etc)'), '<p><code>x</code></p>', 'no dot segments in internal links');
assert.match(html('[x](/scenes/processing)'), /<a href="\/scenes\/processing">x<\/a>/);
assert.equal(html('[x](/scenesy)'), '<p><code>x</code></p>', 'the internal prefix is a path segment');

// Escaping.
out = html('<script>alert(1)</script> and a <b>tag</b> & ampersand');
assert(!out.includes('<script>') && !out.includes('<b>'), out);
assert.match(out, /&lt;script&gt;alert\(1\)&lt;\/script&gt; and a &lt;b&gt;tag&lt;\/b&gt; &amp; ampersand/);
out = html('| <img src=x onerror=alert(1)> | b |\n| --- | --- |\n| `<i>` | https://x.test/<script> |');
assert(!out.includes('<img') && !out.includes('<i>') && !out.includes('<script>'), out);
assert.match(out, /<code>&lt;i&gt;<\/code>/);
out = html('```\n<script>alert(1)</script>\n```');
assert.equal(out, '<pre><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></pre>');
assert.equal(html('- <b>x</b>'), '<ul><li>&lt;b&gt;x&lt;/b&gt;</li></ul>');
assert.equal(html('# <h1>'), '<h2>&lt;h1&gt;</h2>');

// The real checkpoint reports parse without throwing and keep their tables and lists.
for (const id of ['CP1', 'CP2', 'CP2-CALIB']) {
  const real = parse(readFileSync(`data/references/eyes-checkpoints/${id}/REVIEW.md`, 'utf8'));
  const types = new Set(real.map(b => b.type));
  assert(types.has('h1') && types.has('h2') && types.has('p') && types.has('table'), `${id}: ${[...types]}`);
  const rendered = renderToStaticMarkup(React.createElement('div', null, ...render(real)));
  assert(!/<script|onerror=/.test(rendered));
  assert.equal((rendered.match(/<h1/g) || []).length, 0, `${id}: the report never adds a page h1`);
  assert(rendered.includes('<table>'), `${id} keeps its tables`);
}
console.log('markdown-lite: blocks, demoted headings, tables, http(s)/internal links, repo paths as code and escaping passed.');
