// Scene editor paint order (road-shape 2026-10-07, UNDERSTANDING.md section 5, approach E1): ground < standing < roof fixtures,
// the larger outline first within a tier, array order on ties; display only (draft.objects is never reordered); a non-hitting echo
// of the selected outline; Alt/Option-click selects the object beneath. The census case reads win-69468b2b783cd964's review.json
// (the reviewer's window) and is skipped when data/scenes is absent.
import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
const directory=await mkdtemp(path.join(tmpdir(),'addr-scene-paint-order-'));
try {
 for(const name of ['scene-types','scene-paint-order']) {
  const code=ts.transpileModule(await readFile(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll(/from "\.\/([a-z-]+)"/g,'from "./$1.mjs"');
  await writeFile(path.join(directory,`${name}.mjs`),code);
 }
 const {SCENE_CLASSES,CLASS_PAINT_TIER}=await import(pathToFileURL(path.join(directory,'scene-types.mjs')));
 const {scenePaintOrder,scenePaintAreas,scenePaintTier,sceneObjectStack,sceneObjectBeneath}=await import(pathToFileURL(path.join(directory,'scene-paint-order.mjs')));
 const ids=list=>list.map(o=>o.id);
 const square=(x,y,s)=>[[x,y],[x+s,y],[x+s,y+s],[x,y+s]];
 const object=(id,kind,size,x=0,y=0)=>({id,kind,label:id,status:'candidate',ring:square(x,y,size)});

 // 1. Tiers: every kind has one; ground 0, standing 1, roof fixtures 2 (every kind the code treats as rooftop).
 for(const kind of SCENE_CLASSES) assert.ok([0,1,2].includes(CLASS_PAINT_TIER[kind]),`${kind} has a paint tier`);
 assert.deepEqual(Object.keys(CLASS_PAINT_TIER).sort(),[...SCENE_CLASSES].sort(),'the tier table names exactly the scene kinds');
 for(const kind of ['water','road','driveway','footpath','vegetation','pool']) assert.equal(CLASS_PAINT_TIER[kind],0,`${kind} is ground`);
 for(const kind of ['tree','car','building','other']) assert.equal(CLASS_PAINT_TIER[kind],1,`${kind} is standing`);
 for(const file of ['src/lib/scene-instances.ts','src/components/scenes/SceneRelationships.tsx']) {
  const rooftop=(await readFile(file,'utf8')).match(/const rooftop = \[([^\]]+)\]/)?.[1];
  assert.ok(rooftop,`${file} still lists its rooftop kinds`);
  for(const kind of rooftop.match(/[a-z_]+/g)) assert.equal(CLASS_PAINT_TIER[kind],2,`rooftop ${kind} (${file}) paints as a roof fixture`);
 }
 assert.equal(scenePaintTier('legacy_kind'),1,'an unknown kind paints as a standing object');

 // 2. Order: every paved kind before tree, car and building, which come before solar_panel, skylight and chimney, whatever the array order.
 const mixed=[object('panel','solar_panel',4),object('tree','tree',60),object('road','road',400),object('car','car',8),object('roof','building',30),
  object('chimney','chimney',1),object('drive','driveway',50),object('path','footpath',20),object('skylight','skylight',2)];
 const painted=ids(scenePaintOrder(mixed));
 const at=id=>painted.indexOf(id);
 for(const ground of ['road','drive','path']) for(const standing of ['tree','car','roof']) assert.ok(at(ground)<at(standing),`${ground} paints under ${standing}`);
 for(const standing of ['tree','car','roof']) for(const fixture of ['panel','chimney','skylight']) assert.ok(at(standing)<at(fixture),`${standing} paints under ${fixture}`);
 assert.deepEqual(painted,['road','drive','path','tree','roof','car','panel','skylight','chimney']);

 // 3. Within a tier the larger outline paints first (the smaller wins the hit test); equal areas keep array order.
 assert.deepEqual(ids(scenePaintOrder([object('small','road',5),object('big','road',50),object('mid','driveway',20)])),['big','mid','small']);
 assert.deepEqual(ids(scenePaintOrder([object('b','car',10,0),object('a','car',10,40),object('c','tree',10,80)])),['b','a','c'],'ties keep array order');
 assert.deepEqual(ids(scenePaintOrder([object('canopy','tree',40),object('shed','building',12)])),['canopy','shed'],'a shed under a larger canopy paints on top');

 // 4. Stability: the same input gives the same order; committed areas override the ring, so a vertex drag never reshuffles.
 assert.deepEqual(ids(scenePaintOrder(mixed)),painted,'deterministic');
 assert.deepEqual(ids(scenePaintOrder([...mixed].reverse())),painted,'distinct keys do not depend on array order');
 const committed=[object('lane','road',30),object('street','road',31)];
 const areas=scenePaintAreas(committed);
 assert.deepEqual([...areas.values()],[900,961]);
 const dragged=[{...committed[0],ring:square(0,0,40)},committed[1]];
 assert.deepEqual(ids(scenePaintOrder(dragged,areas)),['street','lane'],'a drag preview keeps the committed order');
 assert.deepEqual(ids(scenePaintOrder(dragged)),['lane','street'],'(without the committed areas the preview would reshuffle)');
 assert.equal(ids(scenePaintOrder([{...object('nan','road',3),ring:[[0,0],[Number.NaN,1],[1,1]]},object('ok','road',3)])).length,2,'a broken ring still paints');

 // 5. draft.objects is never reordered: a frozen input sorts without throwing and is unchanged; the output is a permutation of it.
 const draft=Object.freeze([...mixed]);
 const before=ids(draft);
 const output=scenePaintOrder(draft);
 assert.notEqual(output,draft);
 assert.deepEqual(ids(draft),before,'input order unchanged');
 assert.deepEqual(new Set(output),new Set(draft),'same objects (same references)');
 assert.equal(output.length,draft.length);

 // 6. Alt/Option-click: the stack under the pointer (topmost first, deduplicated, this svg only) and the object beneath the selection.
 const root={contains:element=>element.inside};
 const element=(id,inside=true)=>({inside,getAttribute:name=>name==='data-object-id'?id:null});
 assert.deepEqual(sceneObjectStack([element('car'),element(null),element('road'),element('road'),element('other-svg',false),element('drive')],root),['car','road','drive']);
 assert.deepEqual(sceneObjectStack([],root),[]);
 assert.equal(sceneObjectBeneath(['car','road','drive'],'car'),'road');
 assert.equal(sceneObjectBeneath(['car','road','drive'],'road'),'drive');
 assert.equal(sceneObjectBeneath(['car','road','drive'],'drive'),'car','wraps round to the top');
 assert.equal(sceneObjectBeneath(['car','road'],'tree'),'road','selection elsewhere: the object beneath the top one');
 assert.equal(sceneObjectBeneath(['car','road'],null),'road');
 assert.equal(sceneObjectBeneath(['car'],'car'),'car');
 assert.equal(sceneObjectBeneath(['car'],null),'car');
 assert.equal(sceneObjectBeneath([],'car'),undefined);

 // 7. Static pins: the canvas sorts after the filter from committed areas, keeps one hit path per object, draws a non-hitting echo,
 //    routes Alt in both the review pointer-down and the edit click; the page still passes draft.objects and advances in scene order.
 const canvas=await readFile('src/components/scenes/SceneCanvas.tsx','utf8');
 assert.ok(canvas.includes('scenePaintOrder(items.filter(paintable), baseline || layer === "model" ? paintAreas.model : paintAreas.edited)'),'render sorts after the filter');
 assert.ok(canvas.includes('useMemo(() => ({ edited: scenePaintAreas(objects), model: scenePaintAreas(original) }), [objects, original])'),'areas from the committed objects');
 assert.equal(canvas.match(/data-object-id=\{/g)?.length,1,'one hit path per object');
 assert.ok(!/\b(objects|original|items)\.(sort|reverse)\(/.test(canvas),'the canvas never sorts its object arrays in place');
 const echoStart=canvas.indexOf('className="scene-selected-echo"');
 assert.ok(echoStart>0,'the selected echo exists');
 const echo=canvas.slice(canvas.lastIndexOf('<path',echoStart),canvas.indexOf('/>',echoStart)+2);
 assert.ok(echo.includes('fill="none"')&&echo.includes('pointerEvents="none"'),'the echo never takes a click');
 for(const banned of ['data-object-id','role=','onClick','tabIndex']) assert.ok(!echo.includes(banned),`the echo has no ${banned}`);
 assert.ok(canvas.indexOf('render(layer === "model" ? original')<echoStart,'the echo is drawn after the objects');
 assert.ok(canvas.includes('document.elementsFromPoint(e.clientX, e.clientY)'),'Alt-click reads the browser hit stack');
 assert.ok(canvas.includes('const target = e.altKey ? objectBeneath(e, e.currentTarget) ?? hit : hit;'),'review pointer-down routes Alt');
 assert.ok(canvas.includes('onSelect(e.altKey ? objectBeneath(e, e.currentTarget.ownerSVGElement) ?? o.id : o.id)'),'edit click routes Alt');
 const page=await readFile('src/app/scenes/page.tsx','utf8');
 assert.ok(page.includes('objects={draft.objects}'),'the canvas still receives draft.objects in array order');
 assert.ok(page.includes('const decidedAt = next.objects.findIndex(o => o.id === selectedId);'),'advance follows scene order, not paint order');

 // 8. Census case: the reviewer's window (7 Oct). Grid hit test (even-odd, the canvas fillRule) over the review layer as render() filters it.
 const reviewFile='data/scenes/win-69468b2b783cd964/review.json';
 if(!existsSync(reviewFile)) console.log('scene paint order: census case skipped (no data/scenes)');
 else {
  const review=JSON.parse(await readFile(reviewFile,'utf8'));
  const live=review.objects.filter(o=>!['rejected','superseded'].includes(o.status)&&o.ring?.length>=3);
  const order=scenePaintOrder(live,scenePaintAreas(live));
  const rank=new Map(order.map((o,i)=>[o.id,i]));
  const box=o=>{const xs=o.ring.map(p=>p[0]),ys=o.ring.map(p=>p[1]);return [Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)];};
  const inRing=(x,y,r)=>{let c=false;for(let i=0,j=r.length-1;i<r.length;j=i++){const [xi,yi]=r[i],[xj,yj]=r[j];if((yi>y)!==(yj>y)&&x<(xj-xi)*(y-yi)/(yj-yi)+xi)c=!c;}return c;};
  const inside=(o,x,y)=>[o.ring,...(o.holes||[])].reduce((c,r)=>r.length>=3&&inRing(x,y,r)?!c:c,false);
  // Share of an object's grid points where it is the topmost painted path (the census "clickable" share; unclickable < 1 %).
  const topShare=(paint,id,step=4)=>{
   const i=paint.findIndex(o=>o.id===id),o=paint[i],[x0,y0,x1,y1]=box(o);
   const above=paint.slice(i+1).map(p=>({p,b:box(p)}));
   let all=0,top=0;
   for(let y=y0+step/2;y<y1;y+=step) for(let x=x0+step/2;x<x1;x+=step) {
    if(!inside(o,x,y))continue;
    all++;
    if(!above.some(({p,b})=>x>=b[0]&&x<=b[2]&&y>=b[1]&&y<=b[3]&&inside(p,x,y)))top++;
   }
   return all?top/all:0;
  };
  const pct=v=>`${Math.round(v*100)}%`;
  const road=live.find(o=>o.id==='e31f2cb143c1b8dc'),twin=live.find(o=>o.id==='d5875542ec9e47a2');
  if(road&&twin) {
   assert.ok(rank.get(road.id)>rank.get(twin.id),'road e31f paints after its driveway twin d587');
   const roadNow=topShare(live,road.id),roadNew=topShare(order,road.id),twinNew=topShare(order,twin.id);
   assert.ok(roadNew>0.01,`road e31f is the top hit somewhere (${pct(roadNew)})`);
   console.log(`scene paint order: win-69468 road e31f top-hit ${pct(roadNow)} array order -> ${pct(roadNew)} paint order; driveway twin d587 -> ${pct(twinNew)}`);
  } else console.log(`scene paint order: e31f/d587 pair no longer both live (${!!road}/${!!twin}); ordering case skipped`);
  const paved=order.filter(o=>['road','driveway','footpath'].includes(o.kind));
  const lastPaved=Math.max(...paved.map(o=>rank.get(o.id)));
  const cars=order.filter(o=>o.kind==='car'&&o.status==='confirmed');
  assert.ok(cars.length>0,'the window has confirmed cars');
  for(const id of ['5055200a6085ffc0','bd6c3d77b826d098','741055898032f131','9d5779de36783ce1']) if(cars.some(c=>c.id===id)) assert.ok(rank.get(id)>lastPaved,`confirmed car ${id} paints after every paved object`);
  for(const car of cars) assert.ok(rank.get(car.id)>lastPaved,`confirmed car ${car.id} paints after every paved object`);
  const lastBuilding=Math.max(-1,...order.filter(o=>o.kind==='building').map(o=>rank.get(o.id)));
  for(const panel of order.filter(o=>o.kind==='solar_panel')) assert.ok(rank.get(panel.id)>lastBuilding,`solar panel ${panel.id} paints after every building`);
  // Amodal stand-in (paved holes filled, as a straightened band would be): confirmed cars stay clickable in paint order.
  const filled=list=>list.map(o=>['road','driveway','footpath'].includes(o.kind)?{...o,holes:[]}:o);
  const amodalNow=filled(live),amodalNew=filled(order);
  const hiddenNow=cars.filter(c=>topShare(amodalNow,c.id)<0.01).length,hiddenNew=cars.filter(c=>topShare(amodalNew,c.id)<0.01).length;
  assert.equal(hiddenNew,0,'no confirmed car is unclickable under a hole-filled road in paint order');
  console.log(`scene paint order: win-69468 confirmed cars unclickable with hole-filled paved: ${hiddenNow}/${cars.length} array order -> ${hiddenNew}/${cars.length} paint order`);
 }
 console.log('scene paint order tests passed');
} finally {
 await rm(directory,{recursive:true,force:true});
}
