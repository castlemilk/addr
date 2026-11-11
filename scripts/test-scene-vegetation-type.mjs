// The vegetation-type field (decision card 2026-10-08 part 3, "yes to defaults"): SceneObject.vegetation_type is grass, hedge, shrub or
// tree, optional (absent = not set), kept only on kind tree or vegetation. Checked here: the type, the save validation (refused on a
// building, refused for an unknown value), the inspector quick buttons (a press goes through the page's change(): undoable, status,
// ring and label untouched), the list filter, same-image recovery, and record-chat-feedback.mjs (a vegetation_type decision, dry run
// and apply against a stub app on a fixture scene). Pure: nothing here reads or writes data/scenes; temp folders are deleted.
// The filter is client-side and not in the URL. Live variant (not part of task test:reviews): VEG_APP_URL=http://localhost:3008 also dry-runs record-chat-feedback against the
// running app on a qa_veg_<scene> scratch copy of VEG_SCENE (default win-69468b2b783cd964), deleted at the end.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import http from 'node:http';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const repository = process.cwd();
// A module loader with its own cache: scene-store reads process.cwd() when loaded, so the save check loads a fresh copy inside its temp folder.
const loader = (cache = new Map()) => function load(name) {
  if (cache.has(name)) return cache.get(name);
  const exports = {};
  cache.set(name, exports);
  const source = ts.transpileModule(fs.readFileSync(path.join(repository, `src/lib/${name}.ts`), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  new Function('exports', 'require', source)(exports, id => id.startsWith('.')
    ? load(path.posix.normalize(path.posix.join(path.posix.dirname(name), id))) : id.startsWith('@/lib/') ? load(id.slice('@/lib/'.length)) : require(id));
  return exports;
};
const load = loader();
const vegetation = load('scene-vegetation');
const { VEGETATION_TYPES, VEGETATION_TYPE_LABELS, VEGETATION_TYPE_KINDS, carriesVegetationType, isVegetationType, vegetationTypeIssue, withVegetationType,
  withReviewVegetationType, VEGETATION_FILTERS, vegetationFilterValue, vegetationFilterAllows, vegetationTypeCounts } = vegetation;
const checks = [];
const ok = name => { checks.push(name); };

// --- 1. Types -------------------------------------------------------------------------------------------------------------------
assert.deepEqual([...VEGETATION_TYPES], ['grass', 'hedge', 'shrub', 'tree']);
assert.deepEqual([...VEGETATION_TYPE_KINDS], ['tree', 'vegetation']);
assert.deepEqual(VEGETATION_TYPE_LABELS, { grass: 'Grass', hedge: 'Hedge', shrub: 'Shrub', tree: 'Tree' });
assert.deepEqual([...VEGETATION_FILTERS], ['all', 'unset', 'grass', 'hedge', 'shrub', 'tree']);
{
  // The SceneObject field is the four-value union: a fifth value is a type error (the @ts-expect-error must be used), absent is fine.
  const directory = await mkdtemp(path.join(tmpdir(), 'addr-veg-types-'));
  try {
    const file = path.join(directory, 'probe.ts'), lib = path.join(repository, 'src/lib');
    await writeFile(file, [
      `import type { SceneObject } from ${JSON.stringify(path.join(lib, 'scene-types'))};`,
      `import { VEGETATION_TYPES, type VegetationType } from ${JSON.stringify(path.join(lib, 'scene-vegetation'))};`,
      'export const set: SceneObject["vegetation_type"] = "hedge";',
      'export const absent: SceneObject["vegetation_type"] = undefined;',
      'export const all: readonly VegetationType[] = VEGETATION_TYPES;',
      '// @ts-expect-error a type outside grass / hedge / shrub / tree',
      'export const bush: SceneObject["vegetation_type"] = "bush";',
      '// @ts-expect-error the field is optional, not nullable: clearing removes the key',
      'export const cleared: SceneObject["vegetation_type"] = null;',
      '',
    ].join('\n'));
    const config = ts.getParsedCommandLineOfConfigFile(path.join(repository, 'tsconfig.json'), {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} });
    const program = ts.createProgram([file], { ...config.options, noEmit: true, incremental: false, skipLibCheck: true });
    const diagnostics = [...program.getSyntacticDiagnostics(program.getSourceFile(file)), ...program.getSemanticDiagnostics(program.getSourceFile(file))];
    assert.deepEqual(diagnostics.map(d => ts.flattenDiagnosticMessageText(d.messageText, ' ')), [], 'the probe type-checks: the union refuses bush and null');
  } finally { await rm(directory, { recursive: true, force: true }); }
}
ok('types');

// --- 2. Save validation ---------------------------------------------------------------------------------------------------------
const ring = [[10, 10], [30, 10], [30, 30], [10, 30]], frame = [[1, 0], [0, 1], [0, 0]];
const object = (id, kind, extra = {}) => ({ id, kind, label: kind === 'building' ? 'Building' : kind === 'tree' ? 'Tree' : 'Vegetation', ring, holes: [],
  status: 'candidate', geometry_reviewed: false, source: 'fixture', model: { height_m: 3, base_m: 0, rotation_deg: 0, height_source: 'class-prior', height_verified: false, color: '#447744' }, ...extra });
const model = { id: 'fixture', model_signature: 'a'.repeat(64), image_sha256: 'b'.repeat(64), image_width: 100, image_height: 100, pixel_to_meters: frame, meters_to_pixel: frame,
  objects: [object('tree1', 'tree'), object('veg1', 'vegetation'), object('roof1', 'building')], groups: [] };
const review = (objects, revision = 0, reviewed_classes = []) => ({ revision, model_signature: model.model_signature, objects, groups: [], reviewed_classes });
const { validateSceneReview } = load('scene-store');
{
  const stored = validateSceneReview(review([object('tree1', 'tree', { vegetation_type: 'grass' }), object('veg1', 'vegetation', { vegetation_type: 'hedge' }), object('roof1', 'building')]), model);
  assert.equal(stored.objects[0].vegetation_type, 'grass'); assert.equal(stored.objects[1].vegetation_type, 'hedge'); assert.equal('vegetation_type' in stored.objects[2], false);
  for (const type of VEGETATION_TYPES) for (const kind of VEGETATION_TYPE_KINDS) validateSceneReview(review([object('x', kind, { vegetation_type: type })]), model);
  const refused = (objects, pattern, why) => assert.throws(() => validateSceneReview(review(objects), model), e => e.status === 400 && pattern.test(e.message), why);
  refused([object('roof1', 'building', { vegetation_type: 'tree' })], /Building: a vegetation type is kept only on a tree or vegetation outline/, 'refused on a building');
  for (const kind of ['road', 'pool', 'driveway', 'other']) refused([object('x', kind, { label: kind, vegetation_type: 'grass' })], /only on a tree or vegetation outline/, `refused on ${kind}`);
  refused([object('tree1', 'tree', { vegetation_type: 'bush' })], /choose a vegetation type \(grass, hedge, shrub, tree\)/, 'an unknown value is refused');
  refused([object('tree1', 'tree', { vegetation_type: 'Grass' })], /choose a vegetation type/, 'values are lower case');
  refused([object('tree1', 'tree', { vegetation_type: null })], /choose a vegetation type/, 'null is not a value: clearing removes the key');
  assert.equal(vegetationTypeIssue({ kind: 'tree' }), null); assert.equal(vegetationTypeIssue({ kind: 'building' }), null, 'absent is fine on any kind');
  assert.equal(carriesVegetationType('tree') && carriesVegetationType('vegetation') && !carriesVegetationType('building'), true);
  assert.equal(isVegetationType('shrub') && !isVegetationType('garden-bed'), true, 'garden bed is not offered');
}
{
  // Through the store transaction: a type-only change is an edit of a tree (crm.tree.write), saved with status and ring unchanged.
  const directory = await mkdtemp(path.join(tmpdir(), 'addr-veg-save-'));
  try {
    process.chdir(directory);
    const { saveScene } = loader()('scene-store');
    assert.equal(fs.realpathSync(process.cwd()), fs.realpathSync(directory), 'the store writes only inside the temp folder');
    await mkdir('data/scenes/fixture', { recursive: true });
    await writeFile('data/scenes/fixture/latest.json', JSON.stringify(model));
    await writeFile('data/scenes/fixture/review.json', JSON.stringify(review(model.objects, 1)));
    // A first save with every scope stores the normalized review (computed footprint fields), so the next saves differ by the type alone.
    const everyScope = ['crm.read', 'crm.roof.write', 'crm.tree.write', 'crm.object.write'];
    const base = await saveScene('fixture', review(model.objects, 1), { principal: 'fixture', scopes: everyScope });
    const before = review(base.objects, base.revision);
    const typed = withReviewVegetationType(before, 'tree1', 'shrub');
    await assert.rejects(saveScene('fixture', typed, { principal: 'roofer', scopes: ['crm.read', 'crm.roof.write'] }), e => e.status === 403,
      'a vegetation type is a tree edit: roof access alone cannot store it');
    assert.equal(withReviewVegetationType(before, 'roof1', 'shrub'), before, 'the editor helper never puts a type on a building');
    const onRoof = { ...before, objects: before.objects.map(o => o.id === 'roof1' ? { ...o, vegetation_type: 'shrub' } : o) };
    await assert.rejects(saveScene('fixture', onRoof, { principal: 'fixture', scopes: everyScope }),
      e => e.status === 400 && /only on a tree or vegetation outline/.test(e.message), 'the store refuses a type on a building whoever saves it');
    const saved = await saveScene('fixture', typed, { principal: 'fixture', scopes: ['crm.read', 'crm.tree.write'] });
    assert.equal(saved.revision, base.revision + 1); assert.equal(saved.objects[0].vegetation_type, 'shrub');
    assert.deepEqual(saved.objects.slice(1), base.objects.slice(1), 'the other objects are stored as they were');
    assert.equal(saved.objects[0].status, 'candidate'); assert.deepEqual(saved.objects[0].ring, ring); assert.equal(saved.objects[0].label, 'Tree');
    assert.equal(JSON.parse(await readFile('data/scenes/fixture/review.json', 'utf8')).objects[0].vegetation_type, 'shrub');
  } finally {
    process.chdir(repository);
    await rm(directory, { recursive: true, force: true });
  }
}
ok('validation');

// --- 3. Inspector quick buttons: a press goes through change(), Undo restores it, status / ring / label stay -----------------
{
  const exports = {};
  const code = ts.transpileModule(fs.readFileSync(path.join(repository, 'src/components/scenes/SceneVegetationType.tsx'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => id.startsWith('@/lib/') ? load(id.slice('@/lib/'.length)) : require(id));
  const Buttons = exports.default;
  const { createElement } = require('react'), { renderToStaticMarkup } = require('react-dom/server');
  const buttons = element => {
    const found = [];
    const walk = node => {
      if (Array.isArray(node)) return node.forEach(walk);
      if (!node || typeof node !== 'object') return;
      if (node.type === 'button') found.push(node.props);
      walk(node.props?.children);
    };
    walk(element);
    return found;
  };
  // The page's wiring, as src/app/scenes/page.tsx has it (pinned below): change() pushes the draft on the undo stack.
  let draft = review(model.objects.map(o => ({ ...o, status: o.id === 'tree1' ? 'confirmed' : o.status, geometry_reviewed: o.id === 'tree1' })), 3, ['tree']);
  const undo = [];
  const change = next => { undo.push(draft); draft = next; };
  const changeVegetationType = (objectId, type) => { const next = withReviewVegetationType(draft, objectId, type); if (next === draft) return; change(next); };
  const original = structuredClone(draft), tree = () => draft.objects.find(o => o.id === 'tree1');
  const press = (object, label) => {
    const props = buttons(Buttons({ object, onChange: type => changeVegetationType(object.id, type) })).find(p => [].concat(p.children).join('') === label);
    assert(props, `a ${label} button`); assert.equal(props.disabled, false, `${label} is enabled`);
    props.onClick();
  };
  const markup = renderToStaticMarkup(createElement(Buttons, { object: tree(), onChange: () => {} }));
  for (const label of ['Grass', 'Hedge', 'Shrub', 'Tree', 'Not set']) assert.match(markup, new RegExp(`>${label}</button>`));
  assert.match(markup, /aria-label="Vegetation type"/); assert.match(markup, /data-vegetation-type=""[^>]*aria-pressed="true"|aria-pressed="true"[^>]*data-vegetation-type=""/, 'Not set is current');
  press(tree(), 'Hedge');
  assert.equal(tree().vegetation_type, 'hedge'); assert.equal(undo.length, 1);
  for (const key of ['status', 'geometry_reviewed', 'ring', 'holes', 'label', 'model', 'kind']) assert.deepEqual(tree()[key], original.objects[0][key], `${key} unchanged`);
  assert.deepEqual(draft.objects.slice(1), original.objects.slice(1), 'other objects unchanged'); assert.deepEqual(draft.reviewed_classes, ['tree'], 'reviewed classes kept');
  assert.equal(draft.revision, 3);
  assert.match(renderToStaticMarkup(createElement(Buttons, { object: tree(), onChange: () => {} })), /class="scene-quick-kind is-current" aria-pressed="true"[^>]*>Hedge</);
  press(tree(), 'Hedge'); assert.equal(undo.length, 1, 'pressing the current type changes nothing and adds no undo step');
  press(tree(), 'Grass'); assert.equal(tree().vegetation_type, 'grass'); assert.equal(undo.length, 2);
  press(tree(), 'Not set'); assert.equal('vegetation_type' in tree(), false, 'Not set removes the key'); assert.equal(undo.length, 3);
  // Undo edit three times: the draft before the first press, exactly.
  while (undo.length) draft = undo.pop();
  assert.deepEqual(draft, original, 'Undo restores the draft before the presses');
  assert.equal(Buttons({ object: object('roof1', 'building'), onChange: () => {} }), null, 'no buttons on a building');
  assert(buttons(Buttons({ object: object('veg1', 'vegetation', { status: 'rejected' }), onChange: () => {} })).every(p => p.disabled), 'disabled on a rejected outline');
  assert(buttons(Buttons({ object: object('veg1', 'vegetation'), disabled: true, onChange: () => {} })).every(p => p.disabled), 'disabled while busy');
  const roof = object('roof1', 'building'), superseded = object('tree9', 'tree', { status: 'superseded' });
  assert.equal(withVegetationType(roof, 'grass'), roof, 'a building keeps no type'); assert.equal(withVegetationType(superseded, 'grass'), superseded, 'a replaced outline is history');
  const plain = object('t', 'tree'); assert.equal(withVegetationType(plain, 'bush'), plain, 'an unknown value changes nothing');
  const before = review([roof]); assert.equal(withReviewVegetationType(before, 'roof1', 'grass'), before, 'the same review when nothing changes');
}
ok('quick buttons undoable, status and ring kept');

// --- 4. Filter ------------------------------------------------------------------------------------------------------------------
{
  for (const value of VEGETATION_FILTERS) assert.equal(vegetationFilterValue(value), value);
  for (const value of [null, undefined, '', 'bush', 'ALL', 'garden-bed']) assert.equal(vegetationFilterValue(value), 'all');
  const objects = [object('g', 'vegetation', { vegetation_type: 'grass' }), object('h', 'vegetation', { vegetation_type: 'hedge' }), object('t', 'tree', { vegetation_type: 'tree' }),
    object('s', 'tree', { vegetation_type: 'shrub' }), object('u', 'tree'), object('v', 'vegetation'), object('b', 'building'), object('r', 'road', { label: 'Road' }),
    object('old', 'tree', { status: 'superseded', vegetation_type: 'grass' })];
  const kept = filter => objects.filter(o => vegetationFilterAllows(o, filter)).map(o => o.id);
  assert.deepEqual(kept('all'), objects.map(o => o.id), 'all filters nothing');
  assert.deepEqual(kept('unset'), ['u', 'v'], 'not set: tree and vegetation outlines without a type only');
  assert.deepEqual(kept('grass'), ['g', 'old']); assert.deepEqual(kept('hedge'), ['h']); assert.deepEqual(kept('shrub'), ['s']); assert.deepEqual(kept('tree'), ['t']);
  assert.deepEqual(vegetationTypeCounts(objects), { unset: 2, grass: 1, hedge: 1, shrub: 1, tree: 1 }, 'counts skip other kinds and replaced outlines');
  const { sceneQueueFilter } = load('scene-queue');
  assert.deepEqual(Object.keys(sceneQueueFilter({})).sort(), ['issue', 'kind', 'query', 'status'], 'the server-side filter shape is unchanged');
}
ok('filter');

// --- 5. Static pins on the scene editor (its imports are too heavy to render here) ----------------------------------------------
{
  const page = fs.readFileSync('src/app/scenes/page.tsx', 'utf8');
  const pin = (text, why) => assert(page.includes(text), why);
  pin('const next = withReviewVegetationType(draft, objectId, type);\n    if (next === draft) return;\n    setSelected(objectId);\n    change(next);', 'a press goes through change(), so Undo edit restores it');
  pin('{carriesVegetationType(inspected.kind) && <SceneVegetationType object={inspected} disabled={busy}\n                          onChange={type => changeVegetationType(inspected.id, type)}/>}', 'the inspector shows the buttons on tree and vegetation outlines');
  pin('aria-label="Vegetation type filter"', 'a vegetation type filter select');
  pin('useState<VegetationFilter>("all")', 'the default filter is all');
  assert(!page.includes('params.get("veg")') && !page.includes('params.set("veg"'), 'client-side only: not read from or written to the URL (the URL effect keeps its pinned dependencies)');
  pin('matchesSceneQueue(o,sceneQueueFilter({kind,status,query,issue})) &&\n          vegetationFilterAllows(o, vegetationFilter) &&', 'the queue applies it with the status / kind filter');
  pin('matchesSceneQueue(o, filter) && vegetationFilterAllows(o, vegetationFilter) && (status', 'the advance order after a decision applies it too');
  pin('vegetation_type: carriesVegetationType(e.target.value) ? inspected.vegetation_type : undefined,', 'a class change drops a type the new kind cannot carry');
  assert.equal((page.match(/setVegetationFilter\("all"\)/g) || []).length, 5, 'related, grouped and replacement opens, the canvas click and Undo widen it');
  assert(!/vegetation_type[^\n]*label:|label:[^\n]*VEGETATION_TYPE_LABELS/.test(page), 'the label is never rewritten from the type');
}
ok('editor wiring pinned');

// --- 6. Same-image recovery keeps a type-only edit; a fuller tree inherits it, a building does not -------------------------------
{
  const { recoverSceneReview } = load('scene-feedback');
  const saved = review([object('tree1', 'tree', { vegetation_type: 'hedge' }), model.objects[1], model.objects[2]], 4);
  const recovered = recoverSceneReview(model, { ...model, model_signature: 'c'.repeat(64) }, saved);
  const carried = recovered.review.objects.find(o => o.id === 'tree1');
  assert.equal(carried.vegetation_type, 'hedge', 'a type-only edit is a reviewer edit and is carried'); assert.equal(recovered.carried_objects, 1);
  const fuller = { ...object('whole', 'tree', { replaces: ['tree1'] }) }, roofer = { ...object('roofy', 'building', { replaces: ['tree1'] }) };
  assert.equal(recoverSceneReview(model, { ...model, model_signature: 'c'.repeat(64), objects: [fuller] }, saved).review.objects.find(o => o.id === 'whole').vegetation_type, 'hedge');
  assert.equal('vegetation_type' in recoverSceneReview(model, { ...model, model_signature: 'c'.repeat(64), objects: [roofer] }, saved).review.objects.find(o => o.id === 'roofy'), false);
}
ok('recovery');

// --- 7. record-chat-feedback.mjs: a vegetation_type decision (dry run, then apply) against a stub app -----------------------------
// Asynchronous on purpose: the stub app below runs in this process, so a synchronous spawn would block the requests it must answer.
const runChat = async (base, decisions, apply = false) => {
  const directory = fs.mkdtempSync(path.join(tmpdir(), 'addr-veg-chat-')), file = path.join(directory, 'decisions.json');
  try {
    fs.writeFileSync(file, JSON.stringify({ decisions }));
    const child = spawn(process.execPath, [path.join(repository, 'scripts/record-chat-feedback.mjs'), file, ...(apply ? ['--apply'] : [])],
      { env: { ...process.env, ADDR_WEB: base, NODE_NO_WARNINGS: '1' }, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; }); child.stderr.on('data', chunk => { stderr += chunk; });
    const timer = setTimeout(() => child.kill('SIGKILL'), 60000);
    const status = await new Promise(resolve => child.on('close', resolve));
    clearTimeout(timer);
    assert.equal(status, 0, stderr);
    return JSON.parse(stdout);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
};
{
  const scene = 'qa_veg_fixture', posted = [];
  const stubReview = review([object('tree1', 'tree', { status: 'confirmed', geometry_reviewed: true }), object('veg1', 'vegetation', { vegetation_type: 'grass' }), object('roof1', 'building')], 7);
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, 'http://stub');
    if (request.method === 'GET' && url.pathname === '/api/scenes' && url.searchParams.get('id') === scene)
      return response.end(JSON.stringify({ model: { ...model, id: scene }, review: stubReview, stale: false }));
    if (request.method === 'POST' && url.pathname === '/api/scenes') {
      let body = ''; for await (const chunk of request) body += chunk;
      const { id, review: next } = JSON.parse(body); posted.push({ id, review: next });
      try { const valid = validateSceneReview(next, model); return response.end(JSON.stringify({ review: { ...valid, revision: next.revision + 1 } })); }
      catch (e) { response.statusCode = e.status || 500; return response.end(JSON.stringify({ error: e.message })); }
    }
    response.statusCode = 404; response.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const decisions = [
      { scene, object: 'tree1', vegetation_type: 'hedge', sentence: 'That one is a hedge.' },
      { scene, object: 'veg1', vegetation_type: null },
      { scene, object: 'roof1', vegetation_type: 'grass' },
      { scene, object: 'tree1', vegetation_type: 'bush' },
    ];
    const dry = await runChat(base, decisions);
    assert.equal(dry.apply, false); assert.equal(posted.length, 0, 'a dry run posts nothing');
    assert.deepEqual(dry.failed.map(f => [f.object, f.reason]), [
      ['roof1', 'vegetation_type: a vegetation type is kept only on a tree or vegetation outline.'],
      ['tree1', 'vegetation_type: choose a vegetation type (grass, hedge, shrub, tree) or leave it not set.']]);
    const [hedge, cleared] = dry.scenes[0].changes;
    assert.deepEqual(hedge, { object: 'tree1', before: { status: 'confirmed', geometry_reviewed: true }, after: { status: 'confirmed', vegetation_type: 'hedge', geometry_reviewed: true } });
    assert.deepEqual(cleared, { object: 'veg1', before: { status: 'candidate', vegetation_type: 'grass', geometry_reviewed: false }, after: { status: 'candidate', geometry_reviewed: false } });
    assert.equal(dry.scenes[0].saved, null);
    // Apply the same batch: the two refused decisions are skipped, the other two are saved through POST /api/scenes.
    const applied = await runChat(base, decisions, true);
    assert.equal(applied.failed.length, 2, JSON.stringify(applied.failed)); assert.equal(applied.scenes[0].saved, true); assert.equal(applied.scenes[0].revision, 8);
    assert.equal(posted.length, 1);
    const [tree, veg, roof] = posted[0].review.objects;
    assert.equal(tree.vegetation_type, 'hedge'); assert.equal(tree.status, 'confirmed'); assert.equal(tree.geometry_reviewed, true); assert.deepEqual(tree.ring, ring); assert.equal(tree.label, 'Tree');
    assert.deepEqual(tree.provenance.human_decision.fields, ['vegetation_type']); assert.equal(tree.provenance.human_decision.sentence, 'That one is a hedge.');
    assert.equal(tree.provenance.human_decision.source, 'human:chat');
    assert.equal('vegetation_type' in veg, false); assert.deepEqual(veg.provenance.human_decision.fields, ['vegetation_type']); assert.equal(veg.status, 'candidate');
    assert.equal(roof.provenance, undefined, 'the refused decision wrote nothing on the building'); assert.equal('vegetation_type' in roof, false);
  } finally { server.close(); }
}
ok('record-chat-feedback vegetation_type decision (stub app)');

// --- 8. Live variant: dry run against the running app on a qa_ scratch copy (VEG_APP_URL only) -------------------------------
if (process.env.VEG_APP_URL) {
  const base = process.env.VEG_APP_URL, source = process.env.VEG_SCENE || 'win-69468b2b783cd964';
  const real = path.join(repository, 'data/scenes', source), scratch = `qa_veg_${source}`, folder = path.join(repository, 'data/scenes', scratch);
  const sha = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const realSha = sha(path.join(real, 'review.json'));
  assert(!fs.existsSync(folder), `${scratch} already exists; remove the stale scratch copy first`);
  try {
    fs.cpSync(real, folder, { recursive: true, filter: p => !/review\.lock$|\.tmp$/.test(p) });
    const response = await fetch(`${base}/api/scenes?id=${scratch}`);
    assert.equal(response.status, 200, `GET ${scratch}`);
    const data = await response.json();
    assert(data.review && !data.stale, 'the scratch copy has a current saved review');
    const tree = data.review.objects.find(o => o.kind === 'tree' && !['rejected', 'superseded'].includes(o.status));
    const building = data.review.objects.find(o => o.kind === 'building' && o.status !== 'superseded');
    assert(tree && building, 'a tree and a building on the scratch copy');
    assert(!data.review.objects.some(o => 'vegetation_type' in o), 'no type is stored on any object yet');
    const bytes = fs.readFileSync(path.join(folder, 'review.json'));
    const out = await runChat(base, [{ scene: scratch, object: tree.id, vegetation_type: 'tree', sentence: 'Dry run: a tree.' }, { scene: scratch, object: building.id, vegetation_type: 'grass' }]);
    assert.equal(out.apply, false); assert.equal(out.scenes[0].saved, null);
    assert.deepEqual(out.scenes[0].changes[0].after, JSON.parse(JSON.stringify({ status: tree.status, structure_kind: tree.structure_kind, vegetation_type: 'tree', geometry_reviewed: tree.geometry_reviewed })));
    assert.equal(out.failed.length, 1); assert.match(out.failed[0].reason, /only on a tree or vegetation outline/);
    assert.deepEqual(fs.readFileSync(path.join(folder, 'review.json')), bytes, 'the dry run wrote nothing on the scratch copy');
    console.error(JSON.stringify({ live: { base, scratch, revision: data.review.revision, tree: tree.id, tree_status: tree.status, building: building.id, change: out.scenes[0].changes[0], refused: out.failed[0] } }));
  } finally { fs.rmSync(folder, { recursive: true, force: true }); }
  assert(!fs.existsSync(folder), 'scratch copy removed');
  assert.equal(sha(path.join(real, 'review.json')), realSha, 'real scene review untouched');
  ok('record-chat-feedback dry run on a qa_ scratch copy (live app)');
}

// --- 9. A Fieldwork pull that changes a typed tree into another kind drops the type the new kind cannot carry ---------------------
// clearIncompatibleProfiles (src/lib/annotations/feedback.ts) runs on every imported class correction; before 2026-10-08 it kept
// vegetation_type, so a tree -> building correction from Fieldwork was refused at save. Temp folder only, like section 2.
{
  const directory = await mkdtemp(path.join(tmpdir(), 'addr-veg-fieldwork-'));
  try {
    process.chdir(directory);
    const fresh = loader();
    const store = fresh('annotations/feedback-store'), core = fresh('annotations/feedback'), scenes = fresh('scene-store');
    const { objectGeometry, updateSceneObject, sceneObjects } = fresh('annotations/addr-scene');
    assert.equal(fs.realpathSync(process.cwd()), fs.realpathSync(directory), 'the import writes only inside the temp folder');
    const principal = { principal: 'fixture', scopes: ['crm.read', 'crm.roof.write', 'crm.tree.write', 'crm.object.write'] };
    const sha = value => createHash('sha256').update(value).digest('hex');
    const wrap = value => { const json = JSON.stringify(value); return { json, sha256: sha(json) }; };
    const ref = document => ({ url: `/api/v1/assets/${document.sha256}`, sha256: document.sha256, contentType: 'application/json' });
    const id = 'veg-fieldwork', image = Buffer.from(`Synthetic image bytes for ${id}`), imageSha256 = sha(image);
    const tree = (oid, x) => object(oid, 'tree', { ring: [[x, 10], [x + 15, 10], [x + 15, 25], [x, 25]], status: 'confirmed', geometry_reviewed: true,
      model: { ...object(oid, 'tree').model, tree: { crown_profile: 'rounded', crown_base_ratio: 0.2 } } });
    const fixtureModel = { schema_version: 1, id, title: id, address_id: null, cohort: null, centroid: { lon: 145.3, lat: -37.8 }, image_sha256: imageSha256, image_width: 100, image_height: 100,
      pixel_to_meters: frame, meters_to_pixel: frame, model_signature: sha(`model:${id}`), objects: [tree('hedge1', 5), tree('shrub1', 30), tree('grass1', 55), tree('plain1', 80)],
      groups: [], scans: {}, height_note: 'Estimates' };
    // The reviewer typed three of the four trees locally; the machine model carries no type.
    const types = { hedge1: 'hedge', shrub1: 'shrub', grass1: 'grass' };
    const fixtureReview = { revision: 7, model_signature: fixtureModel.model_signature, objects: fixtureModel.objects.map(o => types[o.id] ? { ...structuredClone(o), vegetation_type: types[o.id] } : structuredClone(o)),
      groups: [], reviewed_classes: ['tree'], basemap_mode: 'raw', updated_by: 'original-actor', updated_at: '2026-10-08T00:00:00Z', history: [] };
    const modelDoc = wrap(fixtureModel), reviewDoc = wrap(fixtureReview);
    const base = { id: `addr-scene-${id}`, projectId: 'test-project', title: id, adapter: 'addr-scene', revision: 0,
      source: { sceneId: id, model: ref(modelDoc), review: ref(reviewDoc), reviewModel: ref(modelDoc), image: { url: `/api/v1/assets/${imageSha256}`, sha256: imageSha256 }, modelSignature: fixtureModel.model_signature, imageSha256, width: 100, height: 100 },
      metadata: { addr: { sourceReviewRevision: 7, snapshotId: 'snapshot-test' } }, status: 'in_review', tags: [], notes: '',
      annotations: fixtureReview.objects.map(o => ({ id: o.id, labelId: 'test-project-kind-tree', geometry: objectGeometry(o, fixtureModel), properties: { addr: { objectId: o.id, layer: 'review', source: o.source } } })) };
    await mkdir(`data/scenes/${id}`, { recursive: true });
    await writeFile(`data/scenes/${id}/latest.json`, modelDoc.json); await writeFile(`data/scenes/${id}/review.json`, reviewDoc.json);
    await writeFile(`data/scenes/${id}/${imageSha256}.png`, image);
    // Fieldwork's class selector changes the kind and nothing else (vegetation_type is not a mapped feedback field).
    let cloud = base;
    for (const [oid, kind] of [['hedge1', 'building'], ['shrub1', 'vegetation'], ['grass1', 'road']]) {
      const current = sceneObjects(cloud, fixtureModel, fixtureReview).find(o => o.id === oid);
      assert.equal(current.vegetation_type, types[oid], `the Fieldwork projection shows ${oid}'s local type`);
      cloud = updateSceneObject(cloud, fixtureReview.objects.find(o => o.id === oid), { ...structuredClone(current), kind }, fixtureModel);
    }
    cloud = { ...cloud, revision: 2 };
    const entry = { itemId: base.id, sceneId: id, baseItem: wrap(base), item: wrap(cloud), model: modelDoc, review: reviewDoc, reviewModel: modelDoc, actor: 'workspace-token', timestamp: '2026-10-08T01:00:00Z' };
    const unsigned = { schemaVersion: 1, origin: 'https://fieldwork.example', projectId: 'test-project', snapshotId: 'snapshot-test', manifestSha256: sha('manifest'), cursor: '40', entries: [entry], quarantined: [] };
    const bundle = { ...unsigned, digest: core.feedbackBundleDigest(unsigned) };
    const plan = await store.previewFeedback(bundle, principal);
    assert.equal(plan.canApply, true, JSON.stringify(plan));
    const result = await store.applyFeedback(bundle, plan.planId, principal);
    assert.equal(result.complete, true, 'the class corrections save: no type is left on a kind that cannot carry one');
    const stored = (await scenes.loadScene(id)).review, by = oid => stored.objects.find(o => o.id === oid);
    assert.equal(stored.revision, 8);
    assert.equal(by('hedge1').kind, 'building'); assert.equal('vegetation_type' in by('hedge1'), false, 'tree -> building drops the type');
    assert.equal(by('grass1').kind, 'road'); assert.equal('vegetation_type' in by('grass1'), false, 'tree -> road drops the type');
    assert.equal(by('shrub1').kind, 'vegetation'); assert.equal(by('shrub1').vegetation_type, 'shrub', 'tree -> vegetation keeps the type: vegetation carries one');
    for (const oid of ['hedge1', 'shrub1', 'grass1']) {
      assert.equal(by(oid).model.tree, undefined, `${oid}: the tree profile is cleared as before`);
      assert.equal(by(oid).status, 'needs_adjustment'); assert.equal(by(oid).fieldwork_import.pending, true, `${oid}: a pending local draft`);
      assert.equal(by(oid).label, 'Tree', `${oid}: the label is not rewritten`);
    }
    assert.equal(by('plain1').kind, 'tree'); assert.equal(by('plain1').status, 'confirmed'); assert.equal('vegetation_type' in by('plain1'), false, 'an untouched tree is stored as it was');
    assert.equal(JSON.parse(await readFile(`data/scenes/${id}/review.json`, 'utf8')).objects.find(o => o.id === 'hedge1').kind, 'building');
  } finally {
    process.chdir(repository);
    await rm(directory, { recursive: true, force: true });
  }
}
ok('Fieldwork class correction drops a type the new kind cannot carry');

// --- 10. The editor's own class changes: the inspector select drops the type, the quick relabel never changes the kind ------------
{
  const page = fs.readFileSync('src/app/scenes/page.tsx', 'utf8');
  // The inspector class select (pinned in section 5) builds the next object with this expression; evaluated here for every kind.
  const { SCENE_CLASSES } = load('scene-types');
  const typed = object('tree1', 'tree', { vegetation_type: 'hedge' });
  for (const kind of SCENE_CLASSES) {
    const next = { ...typed, kind, vegetation_type: carriesVegetationType(kind) ? typed.vegetation_type : undefined };
    assert.equal(vegetationTypeIssue(next), null, `select ${kind}: the next object saves`);
    assert.equal(JSON.parse(JSON.stringify(next)).vegetation_type, carriesVegetationType(kind) ? 'hedge' : undefined, `select ${kind}: the stored type`);
  }
  // The one-key quick relabel only sets a building's structure sub-kind: the kind never changes, a tree is left alone.
  assert(page.includes('|| busy || layer === "model" || instanceMode || data?.stale || effective.kind !== "building"'), 'relabel acts on buildings only');
  const { withStructureKind } = load('scene-structure');
  assert.equal(withStructureKind(typed, 'shed', 'Shed'), typed, 'a typed tree is unchanged by a structure relabel');
  const relabelled = withStructureKind(object('roof1', 'building'), 'shed', 'Shed');
  assert.equal(relabelled.kind, 'building'); assert.equal('vegetation_type' in relabelled, false);
  assert.equal((page.match(/kind: e\.target\.value as SceneKind/g) || []).length, 1, 'the inspector select is the only editor path that sets a new kind');
}
ok('editor class change keeps no type on a kind that cannot carry one');
console.log(`Vegetation type: ${checks.join('; ')} passed.`);
