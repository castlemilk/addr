import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";
const dir = await mkdtemp(path.join(tmpdir(), "addr-roof-geometry-"));
try {
  const source = await readFile("src/lib/roof-geometry.ts", "utf8");
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText.replace('from "three"', `from ${JSON.stringify(import.meta.resolve("three"))}`);
  const modulePath = path.join(dir, "geometry.mjs");
  await writeFile(modulePath, code);
  const { buildRooflineMesh, buildWallStrip, triangulateRing } = await import(pathToFileURL(modulePath));
  const ring = [[0,0],[4,0],[4,0],[10,0],[10,4],[4,4],[4,10],[0,10],[0,0]];
  for (const points of [ring, [...ring].reverse()]) {
    const mesh = buildRooflineMesh([{ ring: points, z: points.map(([x]) => 3 + x * .3) }]);
    let area = 0;
    for (let i = 0; i < mesh.length; i += 9) {
      const [ax,ay,az,bx,by,bz,cx,cy,cz] = mesh.slice(i,i+9);
      area += Math.abs((bx-ax)*(cy-ay)-(cx-ax)*(by-ay))/2;
      assert(Math.abs(az - (3+ax*.3)) < 1e-8);
      assert(Math.abs(bz - (3+bx*.3)) < 1e-8);
      assert(Math.abs(cz - (3+cx*.3)) < 1e-8);
      assert(!((ax+bx+cx)/3 > 4 && (ay+by+cy)/3 > 4), "Triangle crosses courtyard");
    }
    assert(Math.abs(area - 64) < 1e-8, `Missing roof area: ${area}/64 m²`);
  }
  assert.deepEqual(triangulateRing([{x:NaN,y:0},{x:1,y:0},{x:1,y:1}]), []);
  const gableRing = [[0,0],[12,0],[12,8],[0,8]];
  const gableParts = [
    {ring:[[0,0],[12,0],[12,4],[0,4]],z:[3,3,6,6]},
    {ring:[[12,8],[0,8],[0,4],[12,4]],z:[3,3,6,6]},
  ];
  for (const boundary of [gableRing, [...gableRing].reverse(), [...gableRing, gableRing[0]]]) {
    const walls = buildWallStrip(boundary, 0, 3, gableParts);
    let wallArea = 0;
    for (let i = 0; i < walls.length; i += 9) {
      const a = walls.slice(i,i+3), b = walls.slice(i+3,i+6), c = walls.slice(i+6,i+9);
      const u = b.map((x,j)=>x-a[j]), v = c.map((x,j)=>x-a[j]);
      wallArea += Math.hypot(u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])/2;
    }
    assert(Math.abs(wallArea - 144) < 1e-8, "Walls must include both 12 m² triangular gable ends");
    assert(walls.some((v,i)=>i%3===2 && v===6), "Gable walls must reach the ridge");
  }
  console.log("Roof geometry: complete concave coverage, repeated/closed vertices, both windings and preserved heights passed.");
} finally { await rm(dir, { recursive: true, force: true }); }
