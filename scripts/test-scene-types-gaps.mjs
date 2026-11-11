import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
// Interior gaps (holes) against an edited outer outline. repairHoles is an explicit repair step; polygonIssues never repairs.
// The client repairs where an edited outline is saved (the decide path and Save scene) and names every dropped gap; the server
// keeps its contract and refuses illegal gaps (orchestrator contract, 2026-09-30 22:58). Run from the repo root: data/scenes
// is read, never written; everything compiled goes to a temp directory.
const directory=await mkdtemp(path.join(tmpdir(),'addr-scene-gaps-'));
const compiled=new Map();
// Transpile a src module and every module it imports into the temp directory (relative and @/ paths; bare packages resolve
// from the repo). validateSceneReview is pure (review and model in, review out), so the server path runs without any write.
async function compile(source){
  const file=path.resolve(source);
  if(compiled.has(file))return compiled.get(file);
  const out=path.join(directory,path.relative(path.resolve('src'),file)).replace(/\.tsx?$/,'.mjs');
  compiled.set(file,out);
  let code=ts.transpileModule(await readFile(file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
  const targets=new Map();
  for(const [,specifier] of code.matchAll(/\bfrom\s*"([^"]+)"/g)){
    if(targets.has(specifier)||specifier.startsWith('node:'))continue;
    if(specifier.startsWith('.')||specifier.startsWith('@/')){
      const base=specifier.startsWith('@/')?path.resolve('src',specifier.slice(2)):path.resolve(path.dirname(file),specifier);
      const found=[`${base}.ts`,`${base}.tsx`,path.join(base,'index.ts')].find(p=>existsSync(p));
      assert(found,`${source}: cannot resolve ${specifier}`);
      targets.set(specifier,pathToFileURL(await compile(found)).href);
    } else targets.set(specifier,import.meta.resolve(specifier));
  }
  code=code.replace(/\bfrom\s*"([^"]+)"/g,(match,specifier)=>targets.has(specifier)?`from ${JSON.stringify(targets.get(specifier))}`:match);
  await mkdir(path.dirname(out),{recursive:true});
  await writeFile(out,code);
  return out;
}
try{
  const {polygonIssues,ringArea,repairHoles,checkOutlineWithGapRepair,droppedGapsNotice,MIN_GAP_AREA_PX}=await import(pathToFileURL(await compile('src/lib/scene-types.ts')).href);
  const {validateSceneReview}=await import(pathToFileURL(await compile('src/lib/scene-store.ts')).href);
  const {initialRoofLineEdits,reanchorRoofLineEdits}=await import(pathToFileURL(await compile('src/lib/roof-line-edits.ts')).href);
  for(const [name,value] of Object.entries({repairHoles,checkOutlineWithGapRepair,droppedGapsNotice}))assert.equal(typeof value,'function',`scene-types exports ${name}`);
  assert.equal(MIN_GAP_AREA_PX,2);
  const W=512,H=512,GAP_OUTSIDE='Gap 1 must stay inside the outer outline without crossing or sharing an edge',DISCONNECT='Interior gaps cannot disconnect the outline';
  // Scene map-b81429472231f83e object 74282a916c36aa99 (building, 25.4 m2): a copy of its model outline taken 2026-09-30.
  // Gap 1 is a 1 px2 sliver the model left at the roof edge (it shares vertex [285,325] with the outer outline); gap 2 is
  // 10.5 px2 and well inside. The live latest.json, when present, must still carry exactly this outline (read-only).
  const SCENE='data/scenes/map-b81429472231f83e',LATEST=`${SCENE}/latest.json`,REVIEW=`${SCENE}/review.json`,OBJECT='74282a916c36aa99';
  const RING=[[230,300],[228,297],[226,297],[225,299],[222,298],[218,303],[218,308],[217,308],[217,313],[216,313],[215,336],[213,337],[212,344],[211,344],[212,353],[220,357],[246,356],[248,358],[256,358],[256,359],[265,358],[265,359],[274,360],[280,363],[285,363],[285,362],[287,363],[289,361],[289,351],[287,348],[288,340],[286,339],[284,335],[284,332],[286,329],[285,325],[289,321],[289,319],[286,318],[287,315],[289,314],[289,310],[287,309],[287,306],[283,307],[284,303],[280,304],[278,301],[275,301],[275,300],[274,301],[269,300],[267,304],[265,304],[265,299],[263,298],[261,298],[259,301],[256,300],[253,302],[251,302],[250,297],[248,297],[248,300],[232,300],[232,299]];
  const HOLES=[[[283,324],[285,324],[285,325]],[[240,319],[243,318],[243,322],[240,322]]];
  if(existsSync(LATEST)){
    const live=JSON.parse(await readFile(LATEST,'utf8')).objects.find(o=>o.id===OBJECT);
    assert(live,`${LATEST} no longer carries ${OBJECT}`);
    assert.deepEqual([live.ring,live.holes],[RING,HOLES],`${OBJECT}: the live model outline changed since 2026-09-30`);
  } else console.log(`${LATEST} is absent: checking the 2026-09-30 copy of ${OBJECT} only.`);
  const moved=(ring,from,to)=>ring.map(p=>p[0]===from[0]&&p[1]===from[1]?to:p);
  const frozen=JSON.stringify([RING,HOLES]);
  assert.equal(ringArea(HOLES[0]),1);assert.equal(ringArea(HOLES[1]),10.5);

  // 1. The reproduction: moving an outer vertex inward makes the model sliver cross the outline and blocked the save.
  for(const [from,to] of [[[285,325],[280,325]],[[289,321],[284,321]]]){
    const edited=moved(RING,from,to),label=`${from} -> ${to}`;
    assert.deepEqual(polygonIssues(edited,W,H,HOLES),[GAP_OUTSIDE],`${label}: refused before the repair`);
    assert.deepEqual(polygonIssues(edited,W,H,[]),[],`${label}: the edited outline itself is legal`);
    const repaired=repairHoles(edited,HOLES,W,H);
    assert.deepEqual(repaired.dropped,[{index:0,reason:'sliver'}],label);
    assert.equal(repaired.holes.length,1);assert.equal(repaired.holes[0],HOLES[1],`${label}: gap 2 is kept untouched (same array)`);
    assert.deepEqual(polygonIssues(edited,W,H,repaired.holes),[],`${label}: legal after the repair`);
    assert.deepEqual(checkOutlineWithGapRepair(edited,HOLES,W,H),{holes:[HOLES[1]],dropped:[{index:0,reason:'sliver'}],issues:[]},label);
  }

  // 2. The saved (unedited) geometry: repairHoles keeps gap 2 (10.5 px2) and drops gap 1 as a sliver whenever it is applied.
  // The save paths apply it only to an outline that would otherwise be refused, and this one is legal as it is: the model
  // sliver stays on an object nobody edited, and changes only when the reviewer saves an outline that needs the repair.
  const saved=repairHoles(RING,HOLES,W,H);
  assert.deepEqual(saved.dropped,[{index:0,reason:'sliver'}]);assert.deepEqual(saved.holes,[HOLES[1]]);
  assert.deepEqual(polygonIssues(RING,W,H,HOLES),[]);
  const untouched=checkOutlineWithGapRepair(RING,HOLES,W,H);
  assert.equal(untouched.holes,HOLES,'legal geometry comes back as the very same array');
  assert.deepEqual(untouched.dropped,[]);assert.deepEqual(untouched.issues,[]);
  assert.deepEqual(checkOutlineWithGapRepair(RING,undefined,W,H),{holes:[],dropped:[],issues:[]},'no gaps: nothing to repair');

  // 3. Each drop reason on a square outline; kept gaps are returned untouched, in order, with their original indices named.
  const square=[[100,100],[200,100],[200,200],[100,200]];
  const inside10=[[140,140],[145,140],[145,142],[140,142]],inside4=[[160,160],[162,160],[162,162],[160,162]];
  const outside=[[300,300],[305,300],[305,302],[300,302]],sliver=[[150,150],[152,150],[152,151]];
  const crossing=[[190,150],[210,150],[210,160],[190,160]],sharesEdge=[[100,120],[110,120],[110,130],[100,130]];
  const covering=[[50,50],[250,50],[250,250],[50,250]],touching=[[100,150],[110,145],[110,155]];
  const twoPoints=[[100,150],[150,140],[200,150],[150,160]];
  const duplicate=[[140,170],[150,170],[150,170],[150,180],[140,180]],nonFinite=[[120,120],[130,120],[130,130],[Number.NaN,130]];
  assert.equal(ringArea(inside10),10);
  const expect=(holes,dropped,kept,message)=>{
    const before=JSON.stringify([square,holes]),result=repairHoles(square,holes,W,H);
    assert.deepEqual(result.dropped,dropped,message);
    assert.equal(result.holes.length,kept.length,message);kept.forEach((hole,i)=>assert.equal(result.holes[i],hole,`${message}: kept gap ${i} is the input array`));
    assert.equal(JSON.stringify([square,holes]),before,`${message}: no input is mutated`);
    assert.deepEqual(Object.keys(result).sort(),['dropped','holes'],`${message}: the outer ring is never returned or rewritten`);
    assert.deepEqual(polygonIssues(square,W,H,result.holes),[],`${message}: legal after the repair`);
    return result;
  };
  assert.equal(expect([inside10],[],[inside10],'a 10 px2 gap strictly inside is kept').holes.length,1);
  expect([outside],[{index:0,reason:'outside_outline'}],[],'a gap outside the outline');
  expect([sliver],[{index:0,reason:'sliver'}],[],'a 1 px2 sliver inside');
  expect([crossing],[{index:0,reason:'crosses_outline'}],[],'a 200 px2 gap crossing the outline');
  expect([sharesEdge],[{index:0,reason:'crosses_outline'}],[],'a gap sharing an edge with the outline');
  expect([covering],[{index:0,reason:'crosses_outline'}],[],'a gap covering the whole outline is not "outside"');
  expect([touching],[],[touching],'a gap touching the outline at one point stays (polygonIssues allows it)');
  // Touching the outer outline at two points cuts the object in two: polygonIssues refuses it against the outer ring alone.
  assert.deepEqual(polygonIssues(square,W,H,[twoPoints]),[DISCONNECT]);
  expect([twoPoints],[{index:0,reason:'disconnects_outline'}],[],'a gap touching the outline at two points');
  assert.deepEqual(checkOutlineWithGapRepair(square,[twoPoints],W,H),{holes:[],dropped:[{index:0,reason:'disconnects_outline'}],issues:[]});
  const corner=[[230,300],[232,300],[232,302],[230,302]];
  assert.deepEqual(polygonIssues(RING,W,H,[corner]),[DISCONNECT],'on the real outline: a gap edge joining two outer vertices');
  assert.deepEqual(repairHoles(RING,[corner],W,H).dropped,[{index:0,reason:'disconnects_outline'}]);
  // A cycle through two gaps (each touches the outline once and they touch each other) is a rule between gaps: still refused.
  const leftGap=[[100,150],[150,145],[150,155]],rightGap=[[150,155],[200,150],[160,170]];
  assert.deepEqual(polygonIssues(square,W,H,[leftGap]),[]);assert.deepEqual(polygonIssues(square,W,H,[rightGap]),[]);
  assert.deepEqual(checkOutlineWithGapRepair(square,[leftGap,rightGap],W,H),{holes:[leftGap,rightGap],dropped:[],issues:[DISCONNECT]});
  expect([duplicate],[{index:0,reason:'invalid'}],[],'a gap with a repeated corner is not a valid polygon');
  expect([nonFinite],[{index:0,reason:'invalid'}],[],'a gap with a non-finite corner (three finite corners)');
  expect([[[120,120],[130,120]],[[120,120],[Number.NaN,1],[130,130]],null],[{index:0,reason:'sliver'},{index:1,reason:'sliver'},{index:2,reason:'sliver'}],[],'fewer than three finite points');
  expect([inside10,sliver,outside,crossing,inside4,twoPoints],[{index:1,reason:'sliver'},{index:2,reason:'outside_outline'},{index:3,reason:'crosses_outline'},{index:5,reason:'disconnects_outline'}],[inside10,inside4],'mixed gaps keep order and indices');
  // Nothing to repair against: an unusable outer ring or gap list comes back as it is, for validation to report.
  for(const ring of [[[0,0],[1,1]],[[0,0],[10,0],[Number.NaN,10]],null]){const holes=[sliver];assert.deepEqual(repairHoles(ring,holes,W,H),{holes,dropped:[]});}
  assert.deepEqual(repairHoles(square,null,W,H),{holes:null,dropped:[]});
  assert.deepEqual(checkOutlineWithGapRepair(square,null,W,H).issues,['Use at most 32 interior gaps and 2,000 total outline points'],'a malformed gap list is still refused (no validation rule is loosened)');
  // A refused outline the repair cannot make legal keeps its gaps and reports what is left to fix.
  const selfCrossing=[[100,100],[200,100],[200,200],[120,50],[100,200]];assert.equal(ringArea(selfCrossing),2500);
  assert.deepEqual(checkOutlineWithGapRepair(selfCrossing,[crossing],W,H),{holes:[crossing],dropped:[],issues:['Outline edges cross; move or remove a point']});
  // Other rules stay: gaps that overlap each other are still refused after the repair (only gaps against the outer ring are repaired).
  assert.deepEqual(checkOutlineWithGapRepair(square,[crossing,inside10,[[141,141],[146,141],[146,143],[141,143]]],W,H),
    {holes:[inside10,[[141,141],[146,141],[146,143],[141,143]]],dropped:[{index:0,reason:'crosses_outline'}],issues:['Interior gaps cannot overlap or contain one another']});
  // The save paths drop only gaps that are valid polygons on their own. A gap that is malformed or invalid by itself (too few
  // finite corners, a repeated corner, crossing edges, outside the image) is refused as before, named by its own number, even
  // when another gap could be repaired: the duplicate-point, crossing-edge and image-bound rules for gaps are not loosened.
  const editedOutline=moved(RING,[285,325],[280,325]);
  for(const [extra,issue] of [[[[240,330],[245,330],[245,330],[245,335],[240,335]],'Outline points overlap; remove the duplicate point'],
    [[[240,340],[250,350],[250,340],[240,350]],'The outline needs a visible area'],[[[600,10],[610,10],[610,20],[600,20]],'Keep the outline inside the image'],
    [[[240,330],[245,330]],'Use at least three finite outline points'],[[[240,330],[245,330],[245,335],[Number.NaN,335]],'Use at least three finite outline points']]){
    const holes=[...HOLES,extra];
    assert.deepEqual(checkOutlineWithGapRepair(editedOutline,holes,W,H),{holes,dropped:[],issues:[`Gap 3: ${issue}`]},issue);
  }
  assert.deepEqual(checkOutlineWithGapRepair(square,[crossing,[[150,150],[152,150],[152,150]]],W,H).issues,['Gap 2: The outline needs a visible area'],'a degenerate sliver is not dropped as noise');
  assert.deepEqual(checkOutlineWithGapRepair(square,Array.from({length:33},(_,i)=>i?[[110+2*i,110],[111+2*i,110],[111+2*i,111]]:crossing),W,H).issues,
    ['Use at most 32 interior gaps and 2,000 total outline points'],'the gap-count limit is not repaired away');

  // 4. repairHoles drops exactly the gaps polygonIssues refuses against the outer ring (same predicates), over thousands of
  // gaps placed on the pixel grid around the edited real outline, where contacts with vertices and edges are common; every
  // gap it keeps is accepted by polygonIssues on its own.
  const edited=moved(RING,[285,325],[280,325]),counts={kept:0,sliver:0,invalid:0,outside_outline:0,crosses_outline:0,disconnects_outline:0};
  const shapes=(x,y)=>[[[x,y],[x+2,y],[x+2,y+2],[x,y+2]],[[x,y],[x+3,y],[x,y+2]],[[x,y],[x+2,y],[x+2,y+1]],[[x,y],[x+8,y],[x+8,y+1],[x,y+1]]];
  for(let x=204;x<=300;x+=2)for(let y=290;y<=370;y+=2)for(const hole of shapes(x,y)){
    const {dropped}=repairHoles(edited,[hole],W,H),alone=polygonIssues(edited,W,H,[hole]);
    const reason=dropped[0]?.reason??'kept';counts[reason]++;
    if(reason==='outside_outline'||reason==='crosses_outline')assert.deepEqual(alone,[GAP_OUTSIDE],`${JSON.stringify(hole)}: ${reason}`);
    else if(reason==='disconnects_outline')assert.deepEqual(alone,[DISCONNECT],`${JSON.stringify(hole)}: ${reason}`);
    else if(reason==='invalid')assert.match(alone[0],/^Gap 1: /);
    else if(reason==='sliver')assert(ringArea(hole)<MIN_GAP_AREA_PX);
    else assert.deepEqual(alone,[],`${JSON.stringify(hole)} kept but refused`);
  }
  for(const reason of ['kept','sliver','outside_outline','crosses_outline'])assert(counts[reason]>50,`the grid exercises ${reason} (${counts[reason]})`);
  assert(counts.disconnects_outline>0,`the grid exercises disconnects_outline (${counts.disconnects_outline})`);

  // 5. Every legal single-vertex move of the real outline (8 directions, 3 and 5 px) saves after the repair, with or without an
  // extra 9 px2 gap near the edge: no gap error is left for the reviewer.
  const nearEdge=[[280,326],[283,326],[283,329],[280,329]];let edits=0,repairedEdits=0;
  for(let i=0;i<RING.length;i++)for(const [dx,dy] of [[1,0],[-1,0],[0,1],[0,-1],[1,1],[1,-1],[-1,1],[-1,-1]])for(const step of [3,5]){
    const ring=RING.map((p,j)=>j===i?[p[0]+dx*step,p[1]+dy*step]:p);
    if(polygonIssues(ring,W,H,[]).length)continue;
    edits++;
    for(const holes of [HOLES,[...HOLES,nearEdge]]){
      const result=checkOutlineWithGapRepair(ring,holes,W,H);
      assert.deepEqual(result.issues,[],`vertex ${i} moved by ${[dx*step,dy*step]}: ${result.issues}`);
      if(result.dropped.length)repairedEdits++;
    }
  }
  assert(edits>500&&repairedEdits>0,`${edits} legal edits, ${repairedEdits} needed a repair`);
  assert.equal(JSON.stringify([RING,HOLES]),frozen,'no call mutated the fixture');

  // 6. The notice: plain sentences naming each dropped gap as the gap list numbers them (1-based) and why. Kind-neutral: the
  // area joins the object whatever its kind (trees, roads and pools carry gaps too), so the sentence never says roof.
  const NOTICE='gap 1 was dropped because it was a sliver under 2 square pixels, so any area it covered now counts as part of the object; redraw the gap if it is real.';
  const PENDING='Gap 1 will be dropped on save: it is a sliver under 2 square pixels, so any area it covers will count as part of the object; redraw the gap if it is real.';
  assert.equal(droppedGapsNotice([]),'');assert.equal(droppedGapsNotice([],{pending:true,label:'Building'}),'');
  assert.equal(droppedGapsNotice([{index:0,reason:'sliver'}]),NOTICE.charAt(0).toUpperCase()+NOTICE.slice(1));
  assert.equal(droppedGapsNotice([{index:0,reason:'sliver'}],{label:'Building'}),`Building: ${NOTICE}`);
  assert.equal(droppedGapsNotice([{index:0,reason:'sliver'}],{pending:true}),PENDING);
  assert.equal(droppedGapsNotice([{index:0,reason:'crosses_outline'},{index:2,reason:'outside_outline'}]),'Gaps 1 and 3 were dropped because gap 1 crossed or shared an edge with the outer outline and gap 3 lay outside the outer outline, so any area they covered now counts as part of the object; redraw any that are real.');
  assert.equal(droppedGapsNotice([{index:1,reason:'invalid'},{index:3,reason:'sliver'},{index:4,reason:'disconnects_outline'}],{label:'Tree'}),'Tree: gaps 2, 4 and 5 were dropped because gap 2 was not a valid outline on its own, gap 4 was a sliver under 2 square pixels and gap 5 touched the outer outline at more than one point, so any area they covered now counts as part of the object; redraw any that are real.');
  assert.equal(droppedGapsNotice([{index:0,reason:'crosses_outline'},{index:1,reason:'disconnects_outline'}],{pending:true}),'Gaps 1 and 2 will be dropped on save: gap 1 crosses or shares an edge with the outer outline and gap 2 touches the outer outline at more than one point, so any area they cover will count as part of the object; redraw any that are real.');
  for(const reason of ['sliver','outside_outline','crosses_outline','disconnects_outline','invalid'])for(const pending of [false,true])
    assert.doesNotMatch(droppedGapsNotice([{index:0,reason}],{pending,label:'Pool'}),/roof|accura|\n/i,`${reason}: kind-neutral, one line`);

  // 7. The server keeps its contract: validateSceneReview refuses illegal gaps with the existing 400 message and never repairs
  // (scripts/test-scenes-api.mjs L77-83 stays green as written); legal geometry is stored exactly as sent.
  const model={model_signature:'gaps',image_width:W,image_height:H,pixel_to_meters:[[0.236,0],[0,-0.236],[0,0]],meters_to_pixel:[[1/0.236,0],[0,-1/0.236],[0,0]],objects:[],groups:[]};
  const object={id:OBJECT,kind:'building',label:'Building',ring:RING,holes:HOLES,status:'needs_adjustment',geometry_reviewed:false,source:'sam3',
    provenance:{source:'reviewer-test'},model:{height_m:4,base_m:0,rotation_deg:0,height_verified:false,height_source:'class-prior',color:'#67d7e7',width_m:0,length_m:0,area_m2:0}};
  const review=objects=>({revision:3,model_signature:'gaps',objects,groups:[],reviewed_classes:[]});
  const refused=(body,m,message,label)=>assert.throws(()=>validateSceneReview(body,m),e=>e.status===400&&e.message===message,label);
  const request=review([{...object,ring:edited},{...object,id:'untouched'}]),sent=JSON.stringify(request);
  refused(request,model,`Building: ${GAP_OUTSIDE}`,'the edited outline with the unrepaired model sliver is refused, never repaired');
  assert.equal(JSON.stringify(request),sent,'the request is not mutated');
  const legal=validateSceneReview(review([{...object,id:'untouched'},{...object,id:'no-gaps',holes:undefined}]),model).objects;
  assert.equal(legal[0].holes,HOLES,'legal geometry is stored as sent: the model sliver stays on an untouched object');
  assert(!('holes' in legal[1])||legal[1].holes===undefined,'an object without gaps gains none');
  refused(review([{...object,ring:selfCrossing}]),model,'Building: Outline edges cross; move or remove a point','a refused outer outline');
  refused(review([{...object,holes:[...HOLES,[[285,353],[295,353],[295,358],[285,358]]]}]),model,'Building: Gap 3 must stay inside the outer outline without crossing or sharing an edge','a crossing gap');
  const apiRing=[[10,10],[60,10],[60,60],[10,60]],TOO_MANY='Use at most 32 interior gaps and 2,000 total outline points';
  for(const [holes,message] of [[null,TOO_MANY],[[null],TOO_MANY],[[[]],'Gap 1: Use at least three finite outline points'],
    [[[[20,20],[80,20],[80,80],[20,80]]],GAP_OUTSIDE],[[[[10,20],[30,20],[30,30],[10,30]]],GAP_OUTSIDE],
    [[[[20,20],[40,20],[40,40],[20,40]],[[25,25],[30,25],[30,30],[25,30]]],'Interior gaps cannot overlap or contain one another']])
    refused(review([{...object,ring:apiRing,holes}]),model,`Building: ${message}`,`test-scenes-api payload ${JSON.stringify(holes)}`);

  // 8. The client save paths repair before posting (mirrors of page.tsx and SceneCanvas.tsx; section 9 pins the wiring). The
  // decide path (Enter, Looks right, Save outline, Still needs work) and Save scene post legal gaps, the strict server stores
  // them, and the saved message names every dropped gap; status, review flag and provenance stay the reviewer's own.
  const decide=(draft,effective,right,m,outlinePending=true)=>{
    const outlineCheck=checkOutlineWithGapRepair(effective.ring,effective.holes,m.image_width,m.image_height),problems=outlineCheck.issues;
    if(right&&problems.length)return {error:problems[0]};
    const repairedGaps=problems.length?[]:outlineCheck.dropped,frame=m.pixel_to_meters;
    const next={...draft,objects:draft.objects.map(o=>o.id===effective.id?{...(repairedGaps.length&&frame?reanchorRoofLineEdits({...o,holes:outlineCheck.holes},frame).object:o),
      status:right?'confirmed':'needs_adjustment',geometry_reviewed:right}:o)};
    const savedMessage=outlinePending?(right?'Outline saved as the reference outline.':'Outline saved; still open for another look.'):undefined;
    return {next,message:repairedGaps.length?`${savedMessage??'Decision saved.'} ${droppedGapsNotice(repairedGaps,{label:effective.label})}`:savedMessage};
  };
  const saveScene=(draft,m)=>{
    const notices=[];
    const objects=draft.objects.map(o=>{
      const check=checkOutlineWithGapRepair(o.ring,o.holes,m.image_width,m.image_height);
      if(!check.dropped.length||check.issues.length)return o;
      notices.push(droppedGapsNotice(check.dropped,{label:o.label}));
      return reanchorRoofLineEdits({...o,holes:check.holes},m.pixel_to_meters).object;
    });
    return notices.length?{review:{...draft,objects},message:`Scene saved. ${notices.join(' ')}`}:{review:draft,message:undefined};
  };
  const editedObject={...object,ring:edited},draft=review([editedObject,{...object,id:'untouched'}]);
  for(const right of [true,false]){
    const {next,message}=decide(draft,editedObject,right,model);
    assert.equal(message,`${right?'Outline saved as the reference outline.':'Outline saved; still open for another look.'} Building: ${NOTICE}`);
    const [stored,other]=validateSceneReview(next,model).objects;
    assert.deepEqual([stored.ring,stored.holes],[edited,[HOLES[1]]],'the reproduction saves: the sliver is dropped, gap 2 and the edited ring stay');
    assert.equal(stored.status,right?'confirmed':'needs_adjustment');assert.equal(stored.geometry_reviewed,right);assert.deepEqual(stored.provenance,{source:'reviewer-test'});
    assert.equal(stored.model.footprint_area_px,Math.round((ringArea(edited)-10.5)*100)/100,'metrics follow the stored gaps');
    assert.equal(other.holes,HOLES,'an untouched object keeps its model gaps');
  }
  assert.equal(decide(draft,editedObject,true,model,false).message,`Decision saved. Building: ${NOTICE}`);
  assert.deepEqual(decide(review([{...object,ring:selfCrossing}]),{...object,ring:selfCrossing},true,model),{error:'Outline edges cross; move or remove a point'});
  const scene=saveScene(draft,model);
  assert.equal(scene.message,`Scene saved. Building: ${NOTICE}`);assert.equal(scene.review.objects[1],draft.objects[1],'Save scene leaves untouched objects as they are');
  const sceneStored=validateSceneReview(scene.review,model).objects[0];
  assert.deepEqual(sceneStored.holes,[HOLES[1]]);assert.equal(sceneStored.status,'needs_adjustment');assert.equal(sceneStored.geometry_reviewed,false);
  const legalDraft=review([object,{...object,id:'untouched'}]);
  assert.deepEqual(saveScene(legalDraft,model),{review:legalDraft,message:undefined},'a legal draft is posted as it is, with the usual message');
  // Pending roof line edits: after the outer edit changeObject re-anchored them with the gaps unrepaired. The server refuses that
  // capture under the gap rule (the reviewer sees the gap message, not a roof line one); both client paths re-anchor after the repair.
  const ridge=[[230,330],[270,330]].map(([x,y])=>[x*0.236,-y*0.236]);
  const lined={...object,roof_line_edits:{...initialRoofLineEdits(object),vertices:ridge,edges:[{a:0,b:1,kind:'ridge'}]}};
  const drafted=reanchorRoofLineEdits({...lined,ring:edited},model.pixel_to_meters);
  assert.equal(drafted.dropped,0);assert.deepEqual(drafted.object.roof_line_edits.outline,{ring:edited,holes:HOLES});
  refused(review([drafted.object]),model,`Building: ${GAP_OUTSIDE}`,'the unrepaired capture is refused by the gap rule');
  for(const [name,posted] of [['decide',decide(review([drafted.object]),drafted.object,false,model).next],['Save scene',saveScene(review([drafted.object]),model).review]]){
    const accepted=validateSceneReview(posted,model).objects[0];
    assert.deepEqual(accepted.holes,[HOLES[1]],name);assert.deepEqual(accepted.roof_line_edits.outline,{ring:edited,holes:[HOLES[1]]},name);
    assert.equal(accepted.roof_line_edits.edges.length,1,`${name}: dropping a gap only grows the roof, so no roof line leaves it`);
  }
  // The live saved review of the reproduction scene, read-only: an unedited Save scene changes nothing; the reproduction edit
  // is refused unrepaired and saves after the client repair, with only the edited object changed.
  let liveChecked='the live review was absent';
  if(existsSync(REVIEW)&&existsSync(LATEST)){
    const liveModel=JSON.parse(await readFile(LATEST,'utf8')),liveReview=JSON.parse(await readFile(REVIEW,'utf8'));
    const target=liveReview.objects.find(o=>o.id===OBJECT);
    if(target&&JSON.stringify([target.ring,target.holes])===frozen){
      validateSceneReview(liveReview,liveModel);
      assert.deepEqual(saveScene(liveReview,liveModel),{review:liveReview,message:undefined},'an unedited save changes no gap in the live review');
      const editedLive={...target,ring:moved(target.ring,[285,325],[280,325]),status:'needs_adjustment',geometry_reviewed:false};
      const liveDraft={...liveReview,objects:liveReview.objects.map(o=>o.id===OBJECT?editedLive:o)};
      refused(liveDraft,liveModel,`${target.label}: ${GAP_OUTSIDE}`,'the live reproduction is refused unrepaired');
      const posted=saveScene(liveDraft,liveModel);
      assert.equal(posted.message,`Scene saved. ${target.label}: ${NOTICE}`);
      posted.review.objects.forEach((o,i)=>{if(o.id!==OBJECT)assert.equal(o,liveReview.objects[i],`${o.id} is posted as it is`);});
      assert.deepEqual(validateSceneReview(posted.review,liveModel).objects.find(o=>o.id===OBJECT).holes,[HOLES[1]]);
      const decided=decide(liveDraft,editedLive,true,liveModel);
      const confirmed=validateSceneReview(decided.next,liveModel).objects.find(o=>o.id===OBJECT);
      assert.deepEqual([confirmed.status,confirmed.geometry_reviewed,confirmed.holes],['confirmed',true,[HOLES[1]]]);
      liveChecked=`live review revision ${liveReview.revision} (${liveReview.objects.length} objects)`;
    } else liveChecked=`${OBJECT}'s saved outline changed: live review part skipped`;
  }
  // The editor: while a gap waits to be dropped the canvas names it in a notice (never a blocking alert, never silence), and a
  // new interior gap is checked against the gaps as they will be saved, numbered as the Boundary list numbers it.
  const live=(ring,holes)=>{const check=checkOutlineWithGapRepair(ring,holes,W,H);
    return {alert:check.issues[0],notice:check.dropped.length&&!check.issues.length?droppedGapsNotice(check.dropped,{pending:true}):undefined};};
  assert.deepEqual(live(edited,HOLES),{alert:undefined,notice:PENDING});
  assert.deepEqual(live(RING,HOLES),{alert:undefined,notice:undefined},'saved geometry: nothing to say');
  const dragged=[[240,319],[243,318],[243,322],[200,322]];
  assert.deepEqual(live(RING,[HOLES[0],dragged]),{alert:undefined,notice:'Gaps 1 and 2 will be dropped on save: gap 1 is a sliver under 2 square pixels and gap 2 crosses or shares an edge with the outer outline, so any area they cover will count as part of the object; redraw any that are real.'},
    'a gap corner dragged across the outline: the gap under edit is named before the save');
  assert.deepEqual(live(square,[leftGap,rightGap]),{alert:DISCONNECT,notice:undefined},'what the repair cannot fix stays an alert');
  const finish=(current,draw)=>{
    const holes=[...(current.holes||[]),draw],check=checkOutlineWithGapRepair(current.ring,current.holes,W,H),
      number=[...holes.keys()].filter(i=>!check.dropped.some(d=>d.index===i));
    return polygonIssues(current.ring,W,H,[...check.holes,draw])[0]?.replace(/^Gap (\d+)/,(_,n)=>`Gap ${number[Number(n)-1]+1}`);
  };
  const newInside=[[250,330],[255,330],[255,334],[250,334]],newCrossing=[[205,330],[225,330],[225,334],[205,334]],newDuplicate=[[250,330],[255,330],[255,330],[255,334],[250,334]];
  assert.equal(finish({ring:edited,holes:HOLES},newInside),undefined,'a legal new gap is accepted while gap 1 waits to be dropped');
  assert.deepEqual(live(edited,[...HOLES,newInside]),{alert:undefined,notice:PENDING},'the new gap is kept; gap 1 is still named');
  assert.equal(finish({ring:edited,holes:HOLES},newCrossing),'Gap 3 must stay inside the outer outline without crossing or sharing an edge','the new gap keeps the number the Boundary list gives it');
  assert.equal(finish({ring:edited,holes:HOLES},newDuplicate),'Gap 3: Outline points overlap; remove the duplicate point');
  assert.equal(finish({ring:RING,holes:HOLES},newCrossing),'Gap 3 must stay inside the outer outline without crossing or sharing an edge','legal geometry: numbered as before');
  assert.equal(finish({ring:edited},newCrossing),GAP_OUTSIDE,'the first gap is gap 1');

  // 9. The wiring the mirrors stand for (source text, read-only): the server validates as sent and never repairs; decide,
  // Save scene and the canvas call the helpers exactly as mirrored above.
  const page=await readFile('src/app/scenes/page.tsx','utf8'),canvas=await readFile('src/components/scenes/SceneCanvas.tsx','utf8'),store=await readFile('src/lib/scene-store.ts','utf8');
  assert.match(store,/const issues = polygonIssues\(o\.ring, model\.image_width, model\.image_height, o\.holes\);\s+if \(issues\.length\) throw new ReviewError\(`\$\{o\.label\}: \$\{issues\[0\]\}`\);/,'scene-store validates gaps as sent');
  assert.doesNotMatch(store,/repairHoles|checkOutlineWithGapRepair|repairedHoles/,'scene-store never repairs');
  const decideSource=page.slice(page.indexOf('const decide = useCallback('),page.indexOf('const rejectedObject ='));
  for(const piece of ['checkOutlineWithGapRepair(effective.ring, effective.holes, data?.model.image_width, data?.model.image_height)','if (right && problems.length) {',
    'const repairedGaps = problems.length ? [] : outlineCheck.dropped;','reanchorRoofLineEdits({ ...o, holes: outlineCheck.holes }, frame).object',
    'droppedGapsNotice(repairedGaps, { label: effective.label })','setDraft((d) => (d === next'])
    assert(decideSource.includes(piece),`page.tsx decide: ${piece}`);
  const saveButton=page.match(/<button\s+className="review-button review-primary"\s+disabled=\{busy \|\| !dirty\}\s+onClick=\{\(\) => \{[\s\S]*?\n\s*Save scene\n/)?.[0]??'';
  for(const piece of ['checkOutlineWithGapRepair(o.ring, o.holes, data.model.image_width, data.model.image_height)','if (!check.dropped.length || check.issues.length) return o;',
    'droppedGapsNotice(check.dropped, { label: o.label })','reanchorRoofLineEdits({ ...o, holes: check.holes }, data.model.pixel_to_meters).object',
    '`Scene saved. ${notices.join(" ")}`','setDraft((d) => (d === next ? draft : d))'])
    assert(saveButton.includes(piece),`page.tsx Save scene: ${piece}`);
  for(const piece of ['const check=checkOutlineWithGapRepair(current.ring,current.holes,scene.image_width,scene.image_height)',
    '<p className="review-notice" role="alert">{check.issues[0]}</p>','<p className="review-notice" role="status">{droppedGapsNotice(check.dropped,{pending:true})}</p>',
    'number=[...holes.keys()].filter(i=>!check.dropped.some(d=>d.index===i))','polygonIssues(current.ring,scene.image_width,scene.image_height,[...check.holes,draw])[0]?.replace(/^Gap (\\d+)/,(_,n)=>`Gap ${number[Number(n)-1]+1}`)'])
    assert(canvas.includes(piece),`SceneCanvas: ${piece}`);
  console.log(`Scene gaps: reproduction repaired (2 edits), saved geometry kept, drop reasons, polygonIssues agreement on ${Object.values(counts).reduce((a,b)=>a+b,0)} grid gaps ${JSON.stringify(counts)}, ${edits} legal vertex moves (${repairedEdits} repaired), kind-neutral notices, strict server (6 API payloads refused), client decide and Save scene paths (${liveChecked}), editor notice and gap numbering, wiring passed.`);
}finally{await rm(directory,{recursive:true,force:true});}
