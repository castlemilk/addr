// src/lib/outline-hints.ts: the editor's geometry-only hints (degenerate_outline, tiny_area_for_kind, noisy_outline, frame_cut) and the
// quiet outline evidence line, over synthetic rings and, when the scenes exist, the two reviewer-flagged outlines of
// roof_training_250-29501 and the four frame-cut buildings of lot-30300 (read-only). Agent hints, never labels: the module returns
// sentences and numbers, nothing else.
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import ts from 'typescript';

const transpile = file => ts.transpileModule(readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const h = {};
new Function('exports', 'require', transpile('src/lib/outline-hints.ts'))(h, () => { throw new Error('outline-hints must stay import-free'); });

// A 0.1 m/px frame with y down (as every scene stores it): local metres = pixels * 0.1, y negated.
const scale = 0.1;
const scene = { pixel_to_meters: [[scale, 0], [0, -scale], [0, 0]] };
const px = (x, y) => [x / scale, -y / scale];
const rect = (w, l, x0 = 0, y0 = 0) => [px(x0, y0), px(x0 + w, y0), px(x0 + w, y0 + l), px(x0, y0 + l)];
const object = (ring, kind = 'building', status = 'candidate') => ({ kind, ring, status });
const codes = hints => hints.map(x => x.code);

// --- metrics on a clean 10 x 9 m rectangle ---------------------------------------------------------------------
let m = h.outlineMetrics(rect(10, 9), scene.pixel_to_meters);
assert.ok(Math.abs(m.area_m2 - 90) < 1e-6 && Math.abs(m.perimeter_m - 38) < 1e-6, `area ${m.area_m2}, perimeter ${m.perimeter_m}`);
assert.equal(m.vertices, 4); assert.equal(m.simplified_vertices, 4);
assert.ok(Math.abs(m.rect_width_m - 9) < 1e-6 && Math.abs(m.rect_length_m - 10) < 1e-6, `rect ${m.rect_width_m} x ${m.rect_length_m}`);
assert.equal(m.off_axis_edges, 0); assert.equal(m.wonky_share, 0); assert.ok(Math.abs(m.rectilinearity - 1) < 1e-9);
assert.equal(h.outlineMetrics([[0, 0], [1, 1]], scene.pixel_to_meters), null, 'two points are no outline');
assert.equal(h.outlineMetrics(rect(10, 9), undefined), null, 'no affines, no metrics');
// A rotated rectangle keeps its width/length and every edge sits on its own axes.
const rotate = (ring, deg) => { const t = deg * Math.PI / 180; return ring.map(([x, y]) => [x * Math.cos(t) - y * Math.sin(t), x * Math.sin(t) + y * Math.cos(t)]); };
m = h.outlineMetrics(rotate(rect(10, 9), 31), scene.pixel_to_meters);
assert.ok(Math.abs(m.rect_width_m - 9) < 1e-6 && Math.abs(m.rect_length_m - 10) < 1e-6, `rotated rect ${m.rect_width_m} x ${m.rect_length_m}`);
assert.equal(m.off_axis_edges, 0, 'a rotated rectangle is not wonky against its own axes');
assert.equal(h.axisDeviation(92, 0), 2); assert.equal(h.axisDeviation(44, 0), 44); assert.equal(h.axisDeviation(47, 0), 43); assert.equal(h.axisDeviation(180 + 3, 90), 3);
// Douglas-Peucker on the closed ring: staircase pixels along a straight edge drop out, corners stay.
const stairs = []; for (let i = 0; i <= 100; i++) stairs.push(px(i / 10, (i % 2) * 0.05)); // 10 m edge with a 5 cm stair
const stairRing = [...stairs, px(10, 9), px(0, 9)];
assert.equal(h.simplifyClosedRing(stairRing.map(p => [p[0] * scale, -p[1] * scale]), 0.3).length, 4, 'a 5 cm staircase simplifies to the four corners');

// --- degenerate and tiny ------------------------------------------------------------------------------------------
let hints = h.outlineHints(object(rect(0.3, 0.3)), scene);
assert.deepEqual(codes(hints), ['degenerate_outline', 'tiny_area_for_kind'], `a 0.3 m box: ${codes(hints)}`);
assert.match(hints[0].sentence, /^0\.09 m² over 4 corners, 0\.30 by 0\.30 m; too small to be the outline of anything\./);
assert.equal(hints[0].severity, 'review'); assert.equal(hints[0].source, 'editor-geometry');
assert.deepEqual(Object.keys(hints[0].evidence).sort(), ['area_m2', 'rect_length_m', 'rect_width_m', 'rule', 'vertices']);
assert.match(hints[1].sentence, /^0\.09 m² is small for a building \(under 4 m²\)\.$/);
hints = h.outlineHints(object(rect(0.4, 6)), scene);
assert.deepEqual(codes(hints), ['degenerate_outline', 'tiny_area_for_kind'], 'a 0.4 m wide sliver is degenerate by width (2.4 m² is also under the building floor)');
hints = h.outlineHints(object(rect(1.2, 1.2)), scene);
assert.deepEqual(codes(hints), ['tiny_area_for_kind'], '1.44 m² is under the 4 m² building floor but not degenerate');
assert.match(hints[0].sentence, /^1\.4 m² is small for a building/);
assert.deepEqual(codes(h.outlineHints(object(rect(1.2, 1.2), 'pool'), scene)), ['tiny_area_for_kind'], 'pools have a 2 m² floor');
assert.deepEqual(codes(h.outlineHints(object(rect(1.2, 1.2), 'tree'), scene)), [], 'a 1.44 m² tree is above its 1 m² floor');
assert.deepEqual(codes(h.outlineHints(object(rect(0.6, 0.6), 'vegetation'), scene)), ['degenerate_outline', 'tiny_area_for_kind'], 'a 0.36 m² vegetation box is under both the 1 m² degenerate floor and its kind floor');
assert.deepEqual(codes(h.outlineHints(object(rect(0.8, 1.2), 'vegetation'), scene)), ['degenerate_outline', 'tiny_area_for_kind'], 'a 0.96 m² box (0.8 m wide) is under the 1 m² degenerate floor by area alone, and under the vegetation floor');
assert.deepEqual(codes(h.outlineHints(object(rect(0.9, 1.2), 'vegetation'), scene)), [], 'a 1.08 m² vegetation box is above both floors');
assert.deepEqual(codes(h.outlineHints(object(rect(1.2, 1.2), 'car'), scene)), [], 'kinds without a floor earn nothing');
assert.deepEqual(h.outlineHints(object(rect(0.3, 0.3), 'building', 'rejected'), scene), [], 'rejected objects earn nothing');
assert.deepEqual(h.outlineHints(object(rect(0.3, 0.3), 'building', 'superseded'), scene), [], 'superseded objects earn nothing');
assert.deepEqual(codes(h.outlineHints(object(rect(5, 5)), scene)), [], 'a 25 m² box has nothing to say');

// --- noisy: 54 jittered corners on a 10 x 9 m rectangle fire; the same rectangle with 4 corners does not ---------------
const jittered = (() => {
  // Deterministic jitter (no Math.random): 13 points per side, each pushed 0.15 to 0.35 m sideways in alternating directions
  // and with a small along-edge stagger, so no edge stays on the rectangle's axes and the 0.3 m simplify keeps most corners.
  const ring = [];
  const side = (ax, ay, bx, by, n) => { for (let i = 0; i < n; i++) { const t = i / n, s = ((i * 7) % 5) / 5; const k = (i % 2 ? 1 : -1) * (0.15 + 0.2 * s); const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy); ring.push([ax + dx * t + (-dy / len) * k, ay + dy * t + (dx / len) * k]); } };
  side(0, 0, 10, 0, 14); side(10, 0, 10, 9, 13); side(10, 9, 0, 9, 14); side(0, 9, 0, 0, 13);
  return ring.map(([x, y]) => px(x, y));
})();
assert.equal(jittered.length, 54);
m = h.outlineMetrics(jittered, scene.pixel_to_meters);
assert.ok(m.area_m2 > 80 && m.area_m2 < 100, `area ${m.area_m2}`);
assert.ok(m.vertices_per_metre >= 0.45, `corners per metre ${m.vertices_per_metre}`);
assert.ok(m.wonky_share >= 0.85, `wonky share ${m.wonky_share} (${m.off_axis_edges}/${m.edges_measured})`);
hints = h.outlineHints(object(jittered), scene);
assert.deepEqual(codes(hints), ['noisy_outline'], `jittered: ${codes(hints)}`);
assert.match(hints[0].sentence, /^54 corners along \d+ m of edge, \d+ of them off the roof's axes; roof edges are usually straight\. Check the shadow side\.$/);
assert.equal(hints[0].severity, 'review');
for (const key of ['vertices', 'perimeter_m', 'simplified_vertices', 'vertices_per_metre', 'edges_measured', 'off_axis_edges', 'wonky_share', 'axis_deg', 'rectilinearity', 'rule']) assert.ok(key in hints[0].evidence, `evidence carries ${key}`);
assert.deepEqual(codes(h.outlineHints(object(rect(10, 9)), scene)), [], 'four clean corners are not noisy');
// Corner density alone is not enough: a 10 x 9 m rectangle with 60 corners exactly on its edges (every corner on-axis) stays quiet.
const dense = []; for (let i = 0; i < 15; i++) dense.push(px(i * 10 / 15, 0)); for (let i = 0; i < 15; i++) dense.push(px(10, i * 9 / 15)); for (let i = 0; i < 15; i++) dense.push(px(10 - i * 10 / 15, 9)); for (let i = 0; i < 15; i++) dense.push(px(0, 9 - i * 9 / 15));
assert.deepEqual(codes(h.outlineHints(object(dense), scene)), [], 'collinear corners simplify away and sit on the axes: not noisy');
// The same jitter on a 4 x 4 m building (16 m²) is below the 20 m² floor: quiet.
const small = jittered.map(([x, y]) => [x * 0.4, y * 0.4]);
assert.deepEqual(codes(h.outlineHints(object(small), scene)), [], 'under 20 m² the noisy rule does not apply');
assert.deepEqual(codes(h.outlineHints(object(jittered, 'tree'), scene)), [], 'noisy_outline is a building rule');
// Quiet evidence line for buildings of 20 m² or more, nothing for smaller ones or other kinds.
assert.match(h.outlineEvidenceLine(object(rect(10, 9)), scene), /^Outline: 4 corners along 38 m of edge, 0 off the roof's axes \(0\.11 corners per metre after a 0\.3 m simplify\)\.$/);
assert.equal(h.outlineEvidenceLine(object(rect(4, 4)), scene), null); assert.equal(h.outlineEvidenceLine(object(rect(10, 9), 'tree'), scene), null);

// --- merge: served rows win, editor hints fill only missing codes ------------------------------------------------------
const served = [{ code: 'noisy_outline', severity: 'review', sentence: 'served', evidence: {} }, { code: 'frame_cut', severity: 'review', sentence: 'edge', evidence: {} }];
const merged = h.mergeObjectHints(served, h.outlineHints(object(jittered), scene));
assert.deepEqual(merged.map(x => `${x.code}:${x.sentence}`), ['noisy_outline:served', 'frame_cut:edge'], 'served rows first and a served code is not repeated by the editor');
assert.deepEqual(h.mergeObjectHints(null, h.outlineHints(object(rect(0.3, 0.3)), scene)).map(x => x.code), ['degenerate_outline', 'tiny_area_for_kind']);
assert.deepEqual(h.mergeObjectHints([], []), []);
assert.equal(h.OUTLINE_HINTS_VERSION, 'object-hints-v2'); assert.equal(h.OUTLINE_DESIGN.noisy_outline.min_vertices_per_metre, 0.45); assert.equal(h.OUTLINE_DESIGN.noisy_outline.min_wonky_share, 0.85);

// --- frame_cut: a vertex within edge_px of an image edge, or beyond it (scene_lot_tags: polygon.distance(frame.boundary) <= edge_px) ---
assert.equal(h.OUTLINE_DESIGN.frame_cut.edge_px, 1, 'edge_px mirrors scene_lot_tags DESIGN');
const t = h.OUTLINE_DESIGN.frame_cut.edge_px;
const box = (x0, y0, x1, y1) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]; // raw pixels, y down
assert.equal(h.touchesFrame(box(10, 10, 100, 100), 512, 512, t), false, 'inside');
assert.equal(h.touchesFrame(box(0, 10, 100, 100), 512, 512, t), true, 'on the left edge');
assert.equal(h.touchesFrame(box(10, 0, 100, 100), 512, 512, t), true, 'on the top edge');
assert.equal(h.touchesFrame(box(10, 10, 512, 100), 512, 512, t), true, 'on the right edge (x = width)');
assert.equal(h.touchesFrame(box(10, 10, 100, 512), 512, 512, t), true, 'on the bottom edge (y = height)');
assert.equal(h.touchesFrame(box(0.5, 10, 100, 100), 512, 512, t), true, '0.5 px inside the left edge');
assert.equal(h.touchesFrame(box(10, 10, 100, 511.5), 512, 512, t), true, '0.5 px inside the bottom edge');
assert.equal(h.touchesFrame(box(1, 10, 100, 100), 512, 512, t), true, 'exactly edge_px away counts (<=)');
assert.equal(h.touchesFrame(box(1.5, 10, 100, 100), 512, 512, t), false, '1.5 px inside is clear');
assert.equal(h.touchesFrame(box(-3, 10, 100, 100), 512, 512, t), true, 'a vertex beyond the left edge counts');
assert.equal(h.touchesFrame(box(10, 10, 600, 100), 512, 512, t), true, 'a vertex beyond the right edge counts');
assert.equal(h.touchesFrame(box(10, 10, 600, 100), 1024, 1024, t), false, 'the same ring is clear of a 1024 frame');
assert.equal(h.touchesFrame(box(10, 10, 1000, 1000), 1024, 1024, t), false, 'a 1024 frame: 24 px clear');
assert.equal(h.touchesFrame(box(10, 10, 1023, 1000), 1024, 1024, t), true, 'a 1024 frame: 1 px from the right edge');
assert.equal(h.touchesFrame(box(10, 10, 100, 1023.2), 1024, 1024, t), true, 'a 1024 frame: 0.8 px from the bottom edge');
assert.equal(h.touchesFrame(box(0, 0, 100, 100), undefined, undefined, t), false, 'no frame size, no answer');
// The hint: exact sentence per kind, review severity, editor source, the evidence shape; nothing without an image size or on rejected/superseded.
const framed = { ...scene, image_width: 512, image_height: 512 };
hints = h.outlineHints(object(box(0, 10, 100, 100)), framed); // 10 x 9 m, 4 clean corners: only the frame speaks
assert.deepEqual(codes(hints), ['frame_cut'], `a building on the left edge: ${codes(hints)}`);
assert.equal(hints[0].sentence, 'Touches the image edge; the frame cut it, not the building.');
assert.equal(hints[0].severity, 'review'); assert.equal(hints[0].source, 'editor-geometry');
assert.deepEqual(Object.keys(hints[0].evidence).sort(), ['edge_px', 'height', 'rule', 'width']);
assert.deepEqual([hints[0].evidence.edge_px, hints[0].evidence.width, hints[0].evidence.height], [0, 512, 512]);
assert.equal(h.outlineHints(object(box(0.5, 10, 100, 100)), framed)[0].evidence.edge_px, 0.5, 'edge_px is the nearest vertex distance');
assert.equal(h.outlineHints(object(box(-3, 10, 100, 100)), framed)[0].evidence.edge_px, 0, 'a ring crossing the frame is at distance 0, as polygon.distance says');
assert.equal(h.outlineHints(object(box(10, 10, 512, 100), 'tree'), framed)[0].sentence, 'Touches the image edge; the frame cut it, not the tree.');
assert.equal(h.outlineHints(object(box(10, 10, 100, 512), 'car'), framed)[0].sentence, 'Touches the image edge; the frame cut it, not the car.');
assert.equal(h.outlineHints(object(box(10, 0, 100, 100), 'solar_panel'), framed).find(x => x.code === 'frame_cut').sentence, 'Touches the image edge; the frame cut it, not the panel.');
assert.equal(h.outlineHints(object(box(10, 0, 100, 100), 'footpath'), framed)[0].sentence, 'Touches the image edge; the frame cut it, not the path.');
assert.equal(h.outlineHints(object(box(10, 0, 100, 100), 'skylight'), framed)[0].sentence, 'Touches the image edge; the frame cut it, not the object.', 'kinds outside KIND_NOUNS fall back to object');
assert.deepEqual(codes(h.outlineHints(object(box(2, 10, 100, 100)), framed)), [], '2 px inside is not frame-cut');
assert.deepEqual(codes(h.outlineHints(object(box(0, 10, 100, 100)), scene)), [], 'no image size on the scene, no frame_cut');
assert.deepEqual(codes(h.outlineHints(object(box(0, 10, 100, 100)), { ...scene, image_width: 512 })), [], 'width alone is not a frame');
assert.deepEqual(h.outlineHints(object(box(0, 10, 100, 100), 'building', 'rejected'), framed), [], 'rejected objects earn nothing, frame or not');
assert.deepEqual(h.outlineHints(object(box(0, 10, 100, 100), 'building', 'superseded'), framed), [], 'superseded objects earn nothing, frame or not');
// frame_cut joins the other rules on one object and still yields to a served row of the same code.
hints = h.outlineHints(object(box(0, 0, 3, 3)), framed);
assert.deepEqual(codes(hints), ['degenerate_outline', 'tiny_area_for_kind', 'frame_cut'], `a 0.3 m box in the corner: ${codes(hints)}`);
assert.deepEqual(h.mergeObjectHints(served, h.outlineHints(object(box(0, 10, 100, 100)), framed)).map(x => `${x.code}:${x.sentence}`), ['noisy_outline:served', 'frame_cut:edge'], 'a served frame_cut row is not repeated by the editor');

// --- The reviewer's two outlines on roof_training_250-29501, when the scene is here (read-only) -----------------------
const file = 'data/scenes/roof_training_250-29501/latest.json';
if (existsSync(file)) {
  const latest = JSON.parse(readFileSync(file, 'utf8'));
  const find = id => latest.objects.find(o => o.id === id);
  const noisy = find('4fb089fc5f936f20'), speck = find('587e522a2986ccea');
  if (noisy) {
    const metrics = h.outlineMetrics(noisy.ring, latest.pixel_to_meters);
    console.log(`4fb089fc5f936f20: ${metrics.vertices} corners, ${metrics.perimeter_m.toFixed(1)} m, ${metrics.vertices_per_metre.toFixed(3)} corners/m, wonky ${metrics.off_axis_edges}/${metrics.edges_measured} = ${metrics.wonky_share.toFixed(3)}, rectilinearity ${metrics.rectilinearity.toFixed(3)}`);
    assert.ok(codes(h.outlineHints(noisy, latest)).includes('noisy_outline'), 'the reviewer\'s noisy outline fires noisy_outline');
  }
  if (speck) {
    const fired = codes(h.outlineHints(speck, latest));
    console.log(`587e522a2986ccea: ${fired.join(', ')}`);
    assert.deepEqual(fired, ['degenerate_outline', 'tiny_area_for_kind'], 'the 0.04 m² speck is degenerate and tiny');
  }
} else console.log('roof_training_250-29501 not present; synthetic checks only.');
// --- The four frame-cut buildings the reviewer met on lot-30300, when the scene is here (read-only) ------------------------
const lot = 'data/scenes/lot-30300';
if (existsSync(`${lot}/latest.json`)) {
  const model = JSON.parse(readFileSync(`${lot}/latest.json`, 'utf8'));
  const objects = existsSync(`${lot}/review.json`) ? JSON.parse(readFileSync(`${lot}/review.json`, 'utf8')).objects : model.objects;
  const cut = objects.filter(o => h.touchesFrame(o.ring, model.image_width, model.image_height, t));
  console.log(`lot-30300: ${cut.length} of ${objects.length} objects touch the ${model.image_width} px frame by touchesFrame.`);
  for (const id of ['fc6f6f90d5daf11a', '772ae203b909d880', 'de4d4f8142dbc7b2', 'f21568769253fa28']) {
    const o = objects.find(x => x.id === id);
    if (!o || ['rejected', 'superseded'].includes(o.status)) continue;
    const hint = h.outlineHints(o, model).find(x => x.code === 'frame_cut');
    assert.ok(hint, `${id} fires frame_cut`);
    assert.equal(hint.sentence, 'Touches the image edge; the frame cut it, not the building.');
  }
} else console.log('lot-30300 not present; synthetic frame checks only.');
console.log('outline hints: metrics, closed-ring simplify, rotated rectangle axes, degenerate and tiny floors by kind, noisy_outline on 54 jittered corners (not on clean, dense-collinear, small or non-building rings), quiet evidence line, touchesFrame (inside, on, 0.5 px inside, beyond, 1024 frame) and frame_cut by kind noun (absent without an image size or on rejected/superseded), and served-first merge passed.');
