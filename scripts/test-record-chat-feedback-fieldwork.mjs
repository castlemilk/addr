// record-chat-feedback.mjs `source: human:fieldwork` (the Fieldwork review queue, scripts/ml/fieldwork_review.py pull): the source and the
// Fieldwork receipt (with the change actor) land in provenance.human_decision; a decision without a receipt or without the change
// actor and an unknown source are refused; the default
// stays human:chat. Runs against a stub app in this process; nothing reads or writes data/scenes. Usage: node scripts/test-record-chat-feedback-fieldwork.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';

const repository = process.cwd();
const ring = [[10, 10], [40, 10], [40, 40], [10, 40]];
const object = (id, kind, extra = {}) => ({ id, kind, label: kind, ring, status: 'candidate', geometry_reviewed: false, source: 'sam3-text-onnx',
  model: { height_m: 3, base_m: 0, height_source: 'class-prior', height_verified: false, width_m: 3, length_m: 3, area_m2: 9, rotation_deg: 0, color: '#fff' }, ...extra });
const scene = 'qa_fieldwork_fixture';
const model = { id: scene, model_signature: 'a'.repeat(64), image_sha256: 'b'.repeat(64), image_width: 100, image_height: 100,
  pixel_to_meters: [[0.1, 0], [0, -0.1], [-5, 5]], meters_to_pixel: [[10, 0], [0, -10], [50, 50]], objects: [], groups: [] };
const review = { revision: 4, model_signature: model.model_signature, objects: [object('veg1', 'vegetation'), object('tree1', 'tree')], groups: [], reviewed_classes: [] };
const posted = [];
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://stub');
  if (request.method === 'GET' && url.pathname === '/api/scenes' && url.searchParams.get('id') === scene)
    return response.end(JSON.stringify({ model, review, stale: false }));
  if (request.method === 'POST' && url.pathname === '/api/scenes') {
    let body = ''; for await (const chunk of request) body += chunk;
    const next = JSON.parse(body); posted.push(next);
    return response.end(JSON.stringify({ review: { ...next.review, revision: next.review.revision + 1 } }));
  }
  response.statusCode = 404; response.end('{}');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const run = async (payload, apply = false) => {
  const directory = fs.mkdtempSync(path.join(tmpdir(), 'addr-fieldwork-chat-')), file = path.join(directory, 'decisions.json');
  try {
    fs.writeFileSync(file, JSON.stringify(payload));
    const child = spawn(process.execPath, [path.join(repository, 'scripts/record-chat-feedback.mjs'), file, ...(apply ? ['--apply'] : [])],
      { env: { ...process.env, ADDR_WEB: base, NODE_NO_WARNINGS: '1' }, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; }); child.stderr.on('data', chunk => { stderr += chunk; });
    const status = await new Promise(resolve => child.on('close', resolve));
    return { status, stderr, out: status === 0 ? JSON.parse(stdout) : null };
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
};
const fieldwork = { source: 'human:fieldwork', origin: 'http://127.0.0.1:18131', projectId: 'mooroolbark-review', itemId: 'r1008-3-veg-x-v12345678', revision: 2, cursor: '9', line: 10, actor: 'local-dev', changeActors: ['local-dev'] };
try {
  const payload = { source: 'human:fieldwork', decisions: [
    { scene, object: 'veg1', vegetation_type: 'grass', sentence: 'Fieldwork mooroolbark-review r1008-3-veg-x rev 2: veg line 10 outline status set to confirmed (proposed grass).', fieldwork },
    { scene, object: 'tree1', vegetation_type: 'tree', sentence: 'no receipt' },
    { scene, object: 'tree1', vegetation_type: 'tree', sentence: 'no actor', fieldwork: { ...fieldwork, actor: undefined } },
  ] };
  const dry = await run(payload);
  assert.equal(dry.status, 0, dry.stderr);
  assert.equal(dry.out.source, 'human:fieldwork'); assert.equal(dry.out.apply, false); assert.equal(posted.length, 0, 'a dry run posts nothing');
  assert.deepEqual(dry.out.scenes[0].changes.map(c => [c.object, c.after.vegetation_type]), [['veg1', 'grass']]);
  assert.deepEqual(dry.out.failed.map(f => f.object), ['tree1', 'tree1']);
  assert.match(dry.out.failed[0].reason, /Fieldwork receipt/);
  assert.match(dry.out.failed[1].reason, /change actor/);
  const applied = await run(payload, true);
  assert.equal(applied.status, 0, applied.stderr); assert.equal(posted.length, 1);
  const veg = posted[0].review.objects.find(o => o.id === 'veg1');
  assert.equal(veg.vegetation_type, 'grass'); assert.equal(veg.status, 'candidate');
  assert.equal(veg.provenance.human_decision.source, 'human:fieldwork');
  assert.equal(veg.provenance.human_decision.transcribed_by, 'agent');
  assert.deepEqual(veg.provenance.human_decision.fieldwork, fieldwork);
  assert.deepEqual(veg.provenance.human_decision.fields, ['vegetation_type'], 'the receipt is metadata, not a decision field');
  assert.equal(posted[0].review.objects.find(o => o.id === 'tree1').provenance, undefined);
  const chat = await run({ decisions: [{ scene, object: 'tree1', vegetation_type: 'tree', sentence: 'That is a tree.' }] }, true);
  assert.equal(chat.status, 0, chat.stderr); assert.equal(chat.out.source, 'human:chat');
  assert.equal(posted[1].review.objects.find(o => o.id === 'tree1').provenance.human_decision.source, 'human:chat');
  assert.equal('fieldwork' in posted[1].review.objects.find(o => o.id === 'tree1').provenance.human_decision, false);
  const unknown = await run({ source: 'human:email', decisions: [{ scene, object: 'veg1', vegetation_type: 'grass' }] });
  assert.equal(unknown.status, 2); assert.match(unknown.stderr, /human:chat or human:fieldwork/); assert.equal(posted.length, 2);
  console.log('record-chat-feedback human:fieldwork: 5 checks passed');
} finally { server.close(); }
