// src/lib/scene-queue.ts lotScopeValue / resolveLotAnchor / resolveAddressLot / lotScopeAllows / lotScopeSummary / lotAnchorTitle: the
// scene editor's client-side lot scope. Objects whose dominant lot (lot-tags-v1, served by /api/scenes/lots) lies on other land leave
// the queue and the advance order under "land" (the default: any lot of an anchor plan, common property included), only the anchor
// lots stay under "lot", and "all" filters nothing. The anchor (LotAnchor) is the route address with a plan, else the planned window
// lot, else on a window scene the dominant lots of the asked roofs (buildings only: whole-roof takeovers with `replaces`, carried
// slices with `carried_from`; DESIGN 2026-10-03; a linear-merge feature also carries `replaces` and is never asked). The scope is inert (hides nothing) without a resolved anchor and never hides an object
// without a row or without a dominant lot. A blank lot number ("" on the common-property parcel CP153157) is a lot of its own for the
// comparison and reads as "Plan CP153157" in the words. The existing exports and the server-side filter signature are unchanged: matchesSceneQueue
// never sees a lot. Then filterExclusion / holdInView: why an opened object is not in view (frame first, then other land) and when
// the editor keeps it anyway (unsaved edits, geometry editing). Pure: nothing here reads a scene or writes a file outside a temp
// dir. Then static pins on the scene editor.
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
const directory=await mkdtemp(path.join(tmpdir(),'addr-scene-lot-scope-'));
try {
 for(const name of ['scene-types','scene-queue','scene-review-issues','scene-addresses','outline-hints']) {
  const code=ts.transpileModule(await readFile(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll(/from "\.\/([a-z-]+)"/g,'from "./$1.mjs"');
  await writeFile(path.join(directory,`${name}.mjs`),code);
 }
 const queueModule=await import(pathToFileURL(path.join(directory,'scene-queue.mjs')));
 const {lotScopeValue,resolveAddressLot,resolveLotAnchor,isAskedRoof,hasLotNumber,lotTitle,lotAnchorTitle,lotScopeAllows,lotScopeSummary,matchesSceneQueue,sceneQueueFilter,frameFilterAllows,filterExclusion,holdInView}=queueModule;
 for(const name of ['REVIEW_STATUS_LABELS','sceneQueueFilter','matchesSceneQueue','sceneProgress','sceneSummary','sceneTitle','nextReviewImages','frameFilterValue','frameFilterAllows'])assert.equal(typeof queueModule[name],name==='REVIEW_STATUS_LABELS'?'object':'function',`${name} still exported`);
 for(const name of ['lotScopeValue','resolveAddressLot','resolveLotAnchor','isAskedRoof','hasLotNumber','lotTitle','lotAnchorTitle','lotScopeAllows','lotScopeSummary','filterExclusion','holdInView'])assert.equal(typeof queueModule[name],'function',`${name} exported`);
 assert.deepEqual(Object.keys(sceneQueueFilter({})).sort(),['issue','kind','query','status'],'the server-side filter shape is unchanged (no lot scope)');
 // URL / select values: anything but lot or all is the default, land.
 assert.equal(lotScopeValue('land'),'land');assert.equal(lotScopeValue('lot'),'lot');assert.equal(lotScopeValue('all'),'all');
 for(const bad of [null,undefined,'','bogus','__proto__','ALL','Lot','only'])assert.equal(lotScopeValue(bad),'land',`${bad} -> land`);
 // resolveAddressLot (kept for callers that want one lot): the route's "_scene" address when it names a plan, else the planned window
 // lot (window_frame.lots[0]), else null; it never reads the asked roofs.
 const routeAddress={lot:'3',plan:'PS631347',address_id:30300};
 const planned={window_frame:{lots:[{address_id:30300,class:'numbered',lot:'3',pfi:'214342494',plan:'PS631347',share:0.9284}]}};
 assert.deepEqual(resolveAddressLot(routeAddress,null),{lot:'3',plan:'PS631347'});
 assert.deepEqual(resolveAddressLot(routeAddress,{window_frame:{lots:[{lot:'9',plan:'OTHER',address_id:1,class:'numbered',pfi:'1',share:1}]}}),{lot:'3',plan:'PS631347'},'the route address wins over the planned lot');
 assert.deepEqual(resolveAddressLot({lot:null,plan:null,address_id:30371},planned),{lot:'3',plan:'PS631347'},'no plan on the scene row (a subject window): the planned lot');
 assert.deepEqual(resolveAddressLot(null,planned),{lot:'3',plan:'PS631347'});
 assert.deepEqual(resolveAddressLot(undefined,planned),{lot:'3',plan:'PS631347'});
 assert.deepEqual(resolveAddressLot({lot:'2',plan:'PS1',address_id:null},{window_frame:{lots:[{lot:'9',plan:'OTHER'}]}}),{lot:'2',plan:'PS1'});
 for(const [address,model] of [[null,null],[null,undefined],[null,{}],[null,{window_frame:{}}],[null,{window_frame:{lots:[]}}],[null,{window_frame:{lots:[{lot:'1',plan:null}]}}],[{lot:'3',plan:null},{}],[{lot:'3',plan:''},{window_frame:{lots:[]}}]])
  assert.equal(resolveAddressLot(address,model),null,`no plan anywhere -> null (${JSON.stringify(address)}, ${JSON.stringify(model)})`);
 // resolveLotAnchor: the anchor and its source. (1) the route address with a plan; (2) else the planned window lot; (3) else, on a
 // window scene only, the dominant lots of the asked roofs (whole-roof takeovers carrying `replaces`, carried slices carrying
 // `carried_from`; superseded objects and objects without a dominant lot are skipped), deduplicated by plan and lot in scene order;
 // (4) else null. Shapes as win-67c56bcab05af7a4 records them: two D's on two plans, two superseded carried slices.
 const row=(lot,plan,kind='lot',flags=[])=>({dominant:{lot,plan,pfi:'1',share:1,kind},flags,lots:[{lot,plan,share:1,kind}]});
 const carriedFrom={scene_id:'house-1',model_signature:'a'.repeat(64),object_id:'025cbc995dfed1f0',status:'needs_adjustment',geometry_reviewed:true,iou_with_new:0.91,matched_id:null,method:'georeferenced-affine'};
 const windowModel={window_frame:{version:'frame-cut-window-v1',planned_window_id:'win-67c56bcab05af7a4',identity:'win',lots:[]}};
 const obj=(id,status,extra={})=>({id,kind:'building',label:id,status,geometry_reviewed:false,ring:[[10,10],[100,10],[100,100],[10,100]],source:'sam3',...extra});
 const takeover=[obj('tree1','candidate'),obj('155d2040f874b3f7','candidate',{replaces:['025cbc995dfed1f0'],derived_from:['025cbc995dfed1f0']}),obj('025cbc995dfed1f0','superseded',{carried_from:carriedFrom,replacement_ids:['155d2040f874b3f7']}),
  obj('c0c0668524e5ddc5','candidate',{replaces:['ae3b556491e54311']}),obj('ae3b556491e54311','superseded',{carried_from:{...carriedFrom,object_id:'ae3b556491e54311'},replacement_ids:['c0c0668524e5ddc5']}),obj('other','candidate')];
 const takeoverRows={tree1:row(null,null),'155d2040f874b3f7':row('2','LP27328'),'025cbc995dfed1f0':row('9','LP27328'),'c0c0668524e5ddc5':row('1','PS530708'),'ae3b556491e54311':row('8','PS530708'),other:row('5','RP16517')};
 assert.deepEqual(resolveLotAnchor(routeAddress,planned,takeover,takeoverRows),{source:'address',lots:[{lot:'3',plan:'PS631347'}]},'the route address wins over the planned lot and the asked roofs');
 assert.deepEqual(resolveLotAnchor({lot:null,plan:null,address_id:null},planned,takeover,takeoverRows),{source:'planned',lots:[{lot:'3',plan:'PS631347'}]},'no plan on the scene row: the planned lot wins over the asked roofs');
 assert.deepEqual(resolveLotAnchor(null,planned,[],{}),{source:'planned',lots:[{lot:'3',plan:'PS631347'}]});
 assert.deepEqual(resolveLotAnchor({lot:null,plan:null,address_id:null},windowModel,takeover,takeoverRows),{source:'asked',lots:[{lot:'2',plan:'LP27328'},{lot:'1',plan:'PS530708'}]},'a window with two takeovers on two plans: both lots, in scene order, the superseded slices ignored');
 assert.deepEqual(resolveLotAnchor(null,windowModel,takeover,takeoverRows),{source:'asked',lots:[{lot:'2',plan:'LP27328'},{lot:'1',plan:'PS530708'}]},'no scene row at all: the same');
 assert.deepEqual(resolveLotAnchor(null,windowModel,[takeover[3],takeover[1]],takeoverRows).lots,[{lot:'1',plan:'PS530708'},{lot:'2',plan:'LP27328'}],'scene order, not plan order');
 assert.deepEqual(resolveLotAnchor(null,windowModel,takeover,{...takeoverRows,'155d2040f874b3f7':row('2','LP27328'),c0c0668524e5ddc5:row('2','LP27328')}),{source:'asked',lots:[{lot:'2',plan:'LP27328'}]},'two takeovers on one lot: one anchor lot');
 assert.deepEqual(resolveLotAnchor(null,windowModel,takeover,{...takeoverRows,c0c0668524e5ddc5:row('1','LP27328')}).lots,[{lot:'2',plan:'LP27328'},{lot:'1',plan:'LP27328'}],'two lots of one plan are two anchor lots');
 assert.deepEqual(resolveLotAnchor(null,windowModel,takeover,{...takeoverRows,c0c0668524e5ddc5:row(1,'PS530708')}).lots,[{lot:'2',plan:'LP27328'},{lot:1,plan:'PS530708'}],'a numeric lot is kept as the row has it');
 assert.deepEqual(resolveLotAnchor(null,windowModel,takeover,{...takeoverRows,c0c0668524e5ddc5:row(1,'PS530708'),'155d2040f874b3f7':row('1','PS530708')}).lots,[{lot:'1',plan:'PS530708'}],'and deduplicates as text');
 // A blank lot number: the index records lot "" for the common-property parcel CP153157 (win-2fbb386c3b235ff4, win-4cc07d6908cc688b,
 // win-8c5cf94a5d7b110f). It is an anchor lot of its own, distinct from a plan-only lot (null) on the same plan.
 assert.deepEqual(resolveLotAnchor(null,windowModel,takeover,{...takeoverRows,'155d2040f874b3f7':row('','CP153157'),c0c0668524e5ddc5:row('','CP153157')}),{source:'asked',lots:[{lot:'',plan:'CP153157'}]},'a blank lot number anchors, once');
 assert.deepEqual(resolveLotAnchor(null,windowModel,takeover,{...takeoverRows,'155d2040f874b3f7':row(null,'CP153157'),c0c0668524e5ddc5:row('','CP153157')}).lots,[{lot:null,plan:'CP153157'},{lot:'',plan:'CP153157'}],'null and "" on one plan are two anchor lots, not one');
 assert.deepEqual(resolveLotAnchor(null,windowModel,takeover,{...takeoverRows,'155d2040f874b3f7':row(null,'CP153157'),c0c0668524e5ddc5:row(undefined,'CP153157')}).lots,[{lot:null,plan:'CP153157'}],'an undefined lot is null');
 // A slice-only window: no takeover, the carried slices (needs_adjustment, carried_from) are the asked roof.
 const sliceOnly=[obj('n1','candidate'),obj('slice-a','needs_adjustment',{carried_from:carriedFrom}),obj('slice-b','confirmed',{carried_from:{...carriedFrom,object_id:'slice-b'}}),obj('n2','candidate')];
 assert.deepEqual(resolveLotAnchor(null,windowModel,sliceOnly,{n1:row('7','TP1'),'slice-a':row('3','PS631347'),'slice-b':row('CM1','PS631347','common_property'),n2:row('8','TP2')}),{source:'asked',lots:[{lot:'3',plan:'PS631347'},{lot:'CM1',plan:'PS631347'}]},'carried slices count; unrelated objects never do');
 assert.equal(resolveLotAnchor(null,windowModel,sliceOnly,{n1:row('7','TP1'),n2:row('8','TP2')}),null,'carried slices without a lot row (no review-layer rows yet): null, inert');
 assert.equal(resolveLotAnchor(null,windowModel,sliceOnly,{'slice-a':{dominant:null,flags:['lot_unassociated'],lots:[]},'slice-b':row(null,null)}),null,'no dominant lot / no plan on the asked rows: null');
 assert.equal(resolveLotAnchor(null,windowModel,[obj('d','candidate',{replaces:[]}),obj('e','candidate',{replaces:null}),obj('f','candidate',{carried_from:{}}),obj('g','candidate',{carried_from:null})],{d:row('1','P'),e:row('1','P'),f:row('1','P'),g:row('1','P')}),null,'empty replaces, null replaces, an empty carried_from and a null one are not asked roofs');
 assert.equal(resolveLotAnchor(null,windowModel,[obj('s','superseded',{replaces:['x'],carried_from:carriedFrom})],{s:row('1','P')}),null,'a superseded object is never the asked roof, whatever its lineage');
 assert.equal(resolveLotAnchor(null,windowModel,[],{}),null,'a window with nothing asked: null');
 assert.equal(resolveLotAnchor(null,windowModel,null,null),null);assert.equal(resolveLotAnchor(null,windowModel,undefined,undefined),null);
 assert.equal(resolveLotAnchor(null,windowModel,takeover,null),null,'no lot rows at all: null');
 assert.equal(resolveLotAnchor(null,{},takeover,takeoverRows),null,'a non-window scene (no window_frame) with replaces from the September widening: null, never asked');
 assert.equal(resolveLotAnchor(null,null,takeover,takeoverRows),null);assert.equal(resolveLotAnchor(null,undefined,takeover,takeoverRows),null);
 assert.deepEqual(resolveLotAnchor(null,{window_frame:{}},takeover,takeoverRows).source,'asked','a window_frame with no lots key is still a window');
 assert.deepEqual(resolveLotAnchor(null,{window_frame:{lots:[{lot:'1',plan:null}]}},takeover,takeoverRows).source,'asked','a planned lot without a plan does not anchor; the asked roofs do');
 assert.equal(resolveLotAnchor(null,windowModel,takeover,{...takeoverRows,'155d2040f874b3f7':row('2',''),c0c0668524e5ddc5:row('1',null)}),null,'an empty or null plan on a takeover row is not a plan');
 for(const [address,model] of [[null,{}],[{lot:'3',plan:null},{}],[null,{window_frame:{lots:[]}}]])assert.equal(resolveLotAnchor(address,model,[],{}),null);
 // isAskedRoof reads the kind and the lineage: a building with a non-empty replaces (a whole-roof takeover) or a non-empty carried_from
 // (a carried slice) is asked; nothing else is.
 assert.equal(isAskedRoof(takeover[1]),true,'a building with replaces is asked');assert.equal(isAskedRoof(takeover[2]),false,'superseded');assert.equal(isAskedRoof(sliceOnly[1]),true,'a carried building slice is asked');assert.equal(isAskedRoof(takeover[0]),false);
 // Repair after the behaviour skeptic (3 Oct, promote-one): a linear-merge feature (scene_linear_promote, DESIGN amendment 6 'linear_merge')
 // carries a non-empty `replaces` too; the merged driveway 9de493cd7792d847 on win-ab373041656385bf joined the lot anchor (Lot 75 LP62268
 // beside the whole roof's Lot 76 LP62268). The kind is read on BOTH branches: a non-building with replaces or carried_from is never asked.
 const mergedDriveway=obj('9de493cd7792d847','candidate',{kind:'driveway',replaces:['3723ba7aaaba7f95','9074baf1f533467d'],derived_from:['3723ba7aaaba7f95','9074baf1f533467d']});
 assert.equal(isAskedRoof(mergedDriveway),false,'a driveway with replaces (a linear merge) is NOT asked');
 for(const kind of ['footpath','road','tree','vegetation','pool','other'])assert.equal(isAskedRoof(obj('m','candidate',{kind,replaces:['a']})),false,`a ${kind} with replaces is not asked`);
 assert.equal(isAskedRoof(obj('ct','needs_adjustment',{kind:'tree',carried_from:carriedFrom})),false,'a carried non-building is not asked');
 assert.equal(isAskedRoof(obj('cs','confirmed',{kind:'solar_panel',carried_from:carriedFrom,replaces:['a']})),false,'nor with both lineage fields');
 assert.equal(isAskedRoof(obj('cb','needs_adjustment',{kind:'building',carried_from:carriedFrom})),true,'a carried building slice is');
 assert.equal(isAskedRoof(obj('nk','candidate',{kind:undefined,replaces:['a']})),false,'no kind at all: not asked');
 // win-ab373041656385bf after the promotion, in review order (the driveway precedes the takeover): the anchor is the whole roof's lot alone.
 const promoted=[obj('e4771b99af4f6c49','candidate'),mergedDriveway,obj('3511a06aafd36a43','candidate',{replaces:['b90ffa71ed5e953d'],derived_from:['b90ffa71ed5e953d']}),obj('b90ffa71ed5e953d','superseded',{carried_from:{...carriedFrom,object_id:'b90ffa71ed5e953d'},replacement_ids:['3511a06aafd36a43']})];
 const promotedRows={e4771b99af4f6c49:row('76','LP62268'),'9de493cd7792d847':row('75','LP62268'),'3511a06aafd36a43':row('76','LP62268')};
 assert.deepEqual(resolveLotAnchor({lot:null,plan:null,address_id:null},windowModel,promoted,promotedRows),{source:'asked',lots:[{lot:'76',plan:'LP62268'}]},'the merged driveway on Lot 75 never joins the anchor');
 assert.equal(lotAnchorTitle(resolveLotAnchor(null,windowModel,promoted,promotedRows)),'Asked roof: Lot 76 LP62268','and the note names one lot');
 assert.equal(resolveLotAnchor(null,windowModel,[obj('n','candidate'),mergedDriveway],promotedRows),null,'a window whose only lineage is a linear merge: null, inert');
 // lotTitle / lotAnchorTitle: the words of the lot note.
 assert.equal(lotTitle({lot:'3',plan:'PS631347'}),'Lot 3 PS631347');assert.equal(lotTitle({lot:null,plan:'PS631347'}),'Plan PS631347');assert.equal(lotTitle({lot:3,plan:'PS1'}),'Lot 3 PS1');
 assert.equal(lotTitle({lot:'',plan:'CP153157'}),'Plan CP153157','a blank lot number reads as the plan, never "Lot  CP153157"');
 assert.equal(lotTitle({lot:undefined,plan:'CP153157'}),'Plan CP153157');
 assert.equal(lotTitle({lot:'CM',plan:'SP25379'}),'Lot CM SP25379','a CM lot is named as the data names it (lot-29385)');
 assert.equal(lotTitle({lot:0,plan:'P'}),'Lot 0 P','a numeric zero is a lot number');
 for(const [lot,expected] of [['3',true],[3,true],[0,true],['CM',true],['',false],[null,false],[undefined,false]])assert.equal(hasLotNumber(lot),expected,`hasLotNumber(${JSON.stringify(lot)})`);
 assert.equal(lotAnchorTitle({source:'address',lots:[{lot:'3',plan:'PS631347'}]}),'Lot 3 PS631347');
 assert.equal(lotAnchorTitle({source:'planned',lots:[{lot:null,plan:'PS631347'}]}),'Plan PS631347');
 assert.equal(lotAnchorTitle({source:'asked',lots:[{lot:'2',plan:'LP27328'}]}),'Asked roof: Lot 2 LP27328','one asked lot');
 assert.equal(lotAnchorTitle({source:'asked',lots:[{lot:'2',plan:'LP27328'},{lot:'1',plan:'PS530708'}]}),'Asked roofs: Lot 2 LP27328 and Lot 1 PS530708','two: joined with and');
 assert.equal(lotAnchorTitle({source:'asked',lots:[{lot:'2',plan:'LP27328'},{lot:'1',plan:'PS530708'},{lot:null,plan:'TP1'}]}),'Asked roofs: Lot 2 LP27328, Lot 1 PS530708 and Plan TP1','three: commas and a final and');
 assert.equal(lotAnchorTitle({source:'asked',lots:[{lot:'',plan:'CP153157'}]}),'Asked roof: Plan CP153157','the three CP153157 windows: a blank lot number reads as the plan');
 // lotScopeAllows over the kinds of row the index holds for lot-30300, against an address anchor.
 const addressLot={lot:'3',plan:'PS631347'};
 const address={source:'address',lots:[addressLot]};
 const onLot=row('3','PS631347'),samePlan=row('1','PS631347'),common=row('CM1','PS631347','common_property'),other=row('1','TP334258','lot',['frame_cut','lot_unassociated']),
  noDominant={dominant:null,flags:['lot_unassociated'],lots:[]};
 const rows={onLot,samePlan,common,other,noDominant,noRow:undefined,nullRow:null};
 const allows=(scope,addr)=>Object.entries(rows).filter(([,o])=>lotScopeAllows(o,addr,scope)).map(([name])=>name); // no default: undefined is a case under test
 assert.deepEqual(allows('land',address),['onLot','samePlan','common','noDominant','noRow','nullRow'],'land keeps every lot of the plan, common property included, and every unknown; the other parcel leaves');
 assert.deepEqual(allows('lot',address),['onLot','noDominant','noRow','nullRow'],'lot keeps the address lot and the unknowns only');
 assert.deepEqual(allows('all',address),Object.keys(rows),'all filters nothing');
 for(const scope of ['land','lot','all'])assert.deepEqual(allows(scope,null),Object.keys(rows),`${scope} without an address is inert: nothing hidden silently`);
 assert.equal(lotScopeAllows(row(3,'PS631347'),address,'lot'),true,'a numeric lot compares as text');
 assert.equal(lotScopeAllows(row('3','PS631347'),{source:'planned',lots:[{lot:3,plan:'PS631347'}]},'lot'),true);
 assert.equal(lotScopeAllows(row('1','TP334258'),{source:'address',lots:[]},'land'),true,'an anchor without lots is inert, like no anchor');
 assert.equal(lotScopeAllows(row('3','ps631347'),address,'land'),false,'plans compare exactly');
 assert.equal(lotScopeAllows({dominant:{lot:null,plan:'PS631347'}},address,'lot'),false,'a plan without a lot is not the address lot');
 assert.equal(lotScopeAllows({dominant:{lot:null,plan:'PS631347'}},address,'land'),true,'but it is the address land');
 assert.equal(lotScopeAllows({dominant:{lot:'3',plan:null}},address,'land'),false,'a lot without a plan is not the address land');
 // A two-lot asked anchor (win-67c56bcab05af7a4: Lot 2 LP27328 and Lot 1 PS530708) under land and lot.
 const asked={source:'asked',lots:[{lot:'2',plan:'LP27328'},{lot:'1',plan:'PS530708'}]};
 const askedRows={d1:row('2','LP27328'),d2:row('1','PS530708'),samePlan1:row('9','LP27328'),samePlan2:row('8','PS530708'),common2:row('CM1','PS530708','common_property'),otherPlan:row('5','RP16517'),noPlan:row('5',null),noDominant,noRow:undefined};
 const askedAllows=(scope)=>Object.entries(askedRows).filter(([,o])=>lotScopeAllows(o,asked,scope)).map(([name])=>name);
 assert.deepEqual(askedAllows('land'),['d1','d2','samePlan1','samePlan2','common2','noDominant','noRow'],'land keeps every lot of either asked plan, common property of an asked plan included; other plans and a plan-less dominant lot leave');
 assert.deepEqual(askedAllows('lot'),['d1','d2','noDominant','noRow'],'lot keeps the two asked lots and the unknowns only');
 // A blank-lot anchor (CP153157): the words say "Plan", the comparison still keeps "" with "" under lot, so the asked roof itself stays.
 const blankAnchor={source:'asked',lots:[{lot:'',plan:'CP153157'}]};
 assert.equal(lotScopeAllows(row('','CP153157'),blankAnchor,'lot'),true,'lot: the asked roof on the blank-lot parcel stays');
 assert.equal(lotScopeAllows(row('','CP153157'),blankAnchor,'land'),true);
 assert.equal(lotScopeAllows({dominant:{lot:null,plan:'CP153157'}},blankAnchor,'lot'),false,'lot: a plan-only dominant lot is not the blank lot');
 assert.equal(lotScopeAllows({dominant:{lot:null,plan:'CP153157'}},blankAnchor,'land'),true,'land: but it is the plan');
 assert.equal(lotScopeAllows(row('7','CP153157'),blankAnchor,'lot'),false);assert.equal(lotScopeAllows(row('','OTHER'),blankAnchor,'land'),false,'another plan with a blank lot is other land');
 assert.deepEqual(askedAllows('all'),Object.keys(askedRows));
 assert.equal(lotScopeAllows(row(2,'LP27328'),asked,'lot'),true,'numeric lot against a text anchor lot');
 assert.deepEqual(lotScopeSummary(Object.values(askedRows),asked,'land'),{kept:5,hidden:2,unknown:2});
 assert.deepEqual(lotScopeSummary(Object.values(askedRows),asked,'lot'),{kept:2,hidden:5,unknown:2});
 // lotScopeSummary: kept + hidden + unknown = total; unknown never hides.
 const list=Object.values(rows);
 assert.deepEqual(lotScopeSummary(list,address,'land'),{kept:3,hidden:1,unknown:3});
 assert.deepEqual(lotScopeSummary(list,address,'lot'),{kept:1,hidden:3,unknown:3});
 assert.deepEqual(lotScopeSummary(list,address,'all'),{kept:4,hidden:0,unknown:3});
 assert.deepEqual(lotScopeSummary(list,null,'land'),{kept:4,hidden:0,unknown:3},'no address: nothing hidden');
 assert.deepEqual(lotScopeSummary([],address,'land'),{kept:0,hidden:0,unknown:0});
 // The advance order after a decision, as page.tsx computes it: members in scene order after the decided object, else the last one
 // before it, under the queue filter, the frame filter and the lot scope together. An object on other land never follows under land.
 const box=(id,x0,y0,x1,y1)=>({id,kind:'building',label:id,status:'candidate',geometry_reviewed:false,ring:[[x0,y0],[x1,y0],[x1,y1],[x0,y1]]});
 const scene=[box('a',10,10,100,100),box('b',120,120,200,200),box('c',10,10,100,512),box('d',200,200,300,300),box('e',300,300,400,400)];
 const lots={a:onLot,b:other,c:samePlan,d:common,e:undefined};
 const model={image_width:512,image_height:512};
 const advance=(decidedId,scope,frame='all',addr=address)=>{
  const filter=sceneQueueFilter({status:'all'});
  const members=scene.filter(o=>o.id!==decidedId&&matchesSceneQueue(o,filter)&&frameFilterAllows(o,model,frame)&&lotScopeAllows(lots[o.id],addr,scope));
  const decidedAt=scene.findIndex(o=>o.id===decidedId);
  return members.find(o=>scene.indexOf(o)>decidedAt)?.id??members.at(-1)?.id??null;
 };
 assert.equal(advance('a','land'),'c','land: b is on other land, c (same plan) is next');
 assert.equal(advance('a','lot'),'e','lot: b, c and d are off the lot; e has no row and stays');
 assert.equal(advance('a','all'),'b','all: b is next in scene order');
 assert.equal(advance('a','land','skip'),'d','land and skip frame-cut: c touches the frame, d (common property) is next');
 assert.equal(advance('a','land','all',null),'b','no address: the scope is inert and b is next');
 assert.equal(advance('e','land'),'d','nothing after e: the last member before it');
 // matchesSceneQueue itself is lot-blind: the server's scene summaries keep counting objects on other land.
 assert.ok(matchesSceneQueue(scene[1],sceneQueueFilter({status:'candidate'})));
 // filterExclusion: which client-side filter keeps an object out of the queue. The frame filter is read first (a frame-cut object on
 // other land reads as frame_cut), then the lot scope (other_land exactly when lotScopeAllows is false); null when neither excludes.
 const clear=box('clear',10,10,100,100),cut=box('cut',0,10,100,100);
 assert.equal(filterExclusion(cut,model,'skip',other,address,'land'),'frame_cut','both: frame first');
 assert.equal(filterExclusion(cut,model,'skip',onLot,address,'land'),'frame_cut','frame-cut on the address lot');
 assert.equal(filterExclusion(cut,model,'skip',undefined,address,'land'),'frame_cut','frame-cut without a lot row');
 assert.equal(filterExclusion(clear,model,'skip',other,address,'land'),'other_land','clear of the frame, on another parcel');
 assert.equal(filterExclusion(clear,model,'skip',other,address,'lot'),'other_land');
 assert.equal(filterExclusion(clear,model,'skip',samePlan,address,'lot'),'other_land','lot scope: a same-plan lot is other land');
 assert.equal(filterExclusion(clear,model,'skip',samePlan,address,'land'),null,'land scope: a same-plan lot is in scope');
 assert.equal(filterExclusion(clear,model,'skip',common,address,'land'),null,'common property is the address land');
 assert.equal(filterExclusion(clear,model,'skip',onLot,address,'land'),null,'in scope');
 assert.equal(filterExclusion(clear,model,'skip',onLot,address,'lot'),null);
 assert.equal(filterExclusion(clear,model,'skip',undefined,address,'land'),null,'unknown lot row (no row): never excluded');
 assert.equal(filterExclusion(clear,model,'skip',null,address,'lot'),null,'unknown lot row (null)');
 assert.equal(filterExclusion(clear,model,'skip',noDominant,address,'lot'),null,'a row without a dominant lot: never excluded');
 assert.equal(filterExclusion(clear,model,'skip',other,null,'land'),null,'no address: the scope is inert');
 assert.equal(filterExclusion(cut,model,'skip',other,null,'land'),'frame_cut','no address: the frame filter still reads');
 assert.equal(filterExclusion(cut,model,'all',other,address,'all'),null,'both filters at all: nothing excludes');
 assert.equal(filterExclusion(cut,model,'all',other,address,'land'),'other_land','frame at all: the lot scope is read next');
 assert.equal(filterExclusion(clear,model,'only',onLot,address,'land'),'frame_cut','frame-cut only: a clear object is the frame exclusion');
 assert.equal(filterExclusion(cut,model,'only',other,address,'land'),'other_land','frame-cut only keeps the cut object; the lot scope still excludes it');
 assert.equal(filterExclusion(cut,null,'skip',other,address,'land'),'other_land','no image size: nothing can be called frame-cut');
 assert.equal(filterExclusion(cut,undefined,'skip',onLot,address,'land'),null);
 // holdInView: an excluded object stays in view only with unsaved edits or while editing geometry; a non-excluded object always.
 for(const excluded of [true,false])for(const dirty of [true,false])for(const editingGeometry of [true,false])
  assert.equal(holdInView({excluded,dirty,editingGeometry}),dirty||editingGeometry||!excluded,`holdInView excluded=${excluded} dirty=${dirty} editingGeometry=${editingGeometry}`);
 assert.equal(holdInView({excluded:true,dirty:false,editingGeometry:false}),false,'a hidden object without edits is not held: the card shows the first queue member');
 assert.equal(holdInView({excluded:false,dirty:false,editingGeometry:false}),true);
 // --- Static pins on the scene editor (its imports are too heavy to render here) -------------------------------------------
 const page=await readFile('src/app/scenes/page.tsx','utf8');
 assert(page.includes('aria-label="Lot filter"'),'a fourth filter select');
 assert(page.includes('useState<LotScope>("land")'),'the default scope is land');
 assert(page.includes('setLotScope(lotScopeValue(params.get("lots")))'),'read from the URL as lots=');
 assert(page.includes('if (lotScope !== "land") params.set("lots", lotScope);'),'written to the URL only when not the default');
 assert.equal((page.match(/setLotScope\("all"\)/g)||[]).length,4,'the three deliberate opens (related, grouped, replacement) and Show it anyway widen the lot scope as they widen the frame filter');
 assert.equal((page.match(/setFrameFilter\("all"\)/g)||[]).length,4,'and still widen the frame filter');
 assert(page.includes('fetch(`/api/scenes/lots?scene=${encodeURIComponent(id)}&signature=${encodeURIComponent(modelSignature)}`'),'one lots request per scene and model signature');
 assert(page.includes('const queue = useMemo(() => unframed.filter((o) => frameFilterAllows(o, data?.model, frameFilter) && lotScopeAllows(sceneLotRows[o.id], lotAnchor, lotScope)),'),'the queue applies the lot scope after the frame filter, from the same unframed list that keeps an opened object in view');
 assert(page.includes('const lotAnchor = useMemo(() => (loadedLots ? resolveLotAnchor(loadedLots.address, data?.model, draft?.objects ?? data?.model.objects, sceneLotRows) : null), [loadedLots, data?.model, draft?.objects, sceneLotRows]);'),'the anchor memo reads the route address, the model, the draft objects (review layer: takeovers and carried slices) and the lot rows');
 assert(!page.includes('addressLot')&&!page.includes('resolveAddressLot'),'the single address lot is gone from the page: the anchor replaces it everywhere');
 assert(page.includes('`${lotAnchorTitle(lotAnchor)}: ${lotSummary.hidden} ${lotSummary.hidden === 1 ? "object" : "objects"} on other ${lotScope === "lot" ? "lots" : "land"} hidden`'),'the lot note names the anchor through lotAnchorTitle ("Lot 3 PS631347: 4 objects on other land hidden", "Asked roofs: Lot 2 LP27328 and Lot 1 PS530708: ...")');
 assert(page.includes(`<option value="land">{lotAnchor.source==="asked"?"The asked roof's land":"This address's land"}</option>`),'the land option reads by source');
 assert(page.includes(`<option value="lot">{lotAnchor.source==="asked"?"The asked roof's lot only":"This lot only"}</option>`),'the lot option reads by source');
 assert(page.includes('disabled={editingGeometry||!lotAnchor} value={lotAnchor?lotScope:"all"} data-lot-anchor={lotAnchor?.source??"none"}'),'the select is disabled without an anchor and names the source');
 assert(page.includes('&& frameFilterAllows(o, data.model, frameFilter) && lotScopeAllows(sceneLotRows[o.id], lotAnchor, lotScope));'),'the advance order after a decision applies the same scope');
 assert(page.includes('<option value="all">Lot unknown: all objects</option>'),'an unresolved address reads as Lot unknown on a disabled select');
 assert(page.includes('data-testid="scene-lot-note"'),'the muted note is addressable');
 assert(!page.includes('lotScope') || !/lotScopeAllows\([^)]*\)\s*\?\s*o\s*:/.test(page),'the scope filters the queue; it never rewrites an object');
 // An object the reviewer asked for (URL, canvas click) that the frame filter or the lot scope excludes is not held in view: the
 // card shows the first queue member and a notice names the hidden object with a Show it anyway button; filters never change by themselves.
 assert(page.includes('holdInView({'),'effective uses holdInView');
 assert(page.includes('filterExclusion('),'and filterExclusion names the filter');
 assert(!page.includes('mode === "edit" || unframed.some((o) => o.id === current.id))'),'the old always-hold expression is gone');
 assert(page.includes('data-testid="scene-hidden-notice"'),'the hidden-object notice is addressable');
 assert(page.includes('role="status" data-testid="scene-hidden-notice"')||page.includes('data-testid="scene-hidden-notice" role="status"'),'and is a status region');
 assert(page.includes('Show it anyway'),'with a Show it anyway button');
 assert(page.includes('it touches the image edge (frame cut)'),'frame reason text');
 assert(page.includes('it is on other land ('),'lot reason text names the lot');
 assert(page.includes('it is on other land (${hiddenLot?.plan ? lotTitle({ lot: hiddenLot.lot, plan: hiddenLot.plan }) : "lot unknown"})'),'and names it through lotTitle, so a blank lot number reads "Plan CP153157" in the notice as in the lot note');
 assert(page.includes('objectIds={canvasObjectIds}'),'the canvas draws the non-hidden set only (through its existing objectIds prop)');
 assert(page.includes('objects={draft.objects}'),'while the canvas still receives the full object list for geometry context');
 // The selection converges on the shown object: when a filter hides the selected object and nothing holds it, `selected` is moved
 // to the first queue member during the same render, so a later bare change() with no setSelected (a Layer review coverage checkbox,
 // Reset object to prediction, Edit roof lines, 3D height editing) that makes the draft dirty holds the object the reviewer is looking
 // at, never the hidden one. The notice is derived from the requested id, not from the stale selection.
 assert(page.includes('if (current && effective?.id !== current.id) setSelected(effective?.id ?? null);'),'selected converges on the shown object during render');
 assert(!page.includes('const requestedHidden = current &&'),'the notice is not derived from the stale selection');
 assert(page.includes('draft?.objects.find((o) => o.id === requested.id)'),'the notice looks the requested object up by id');
 // --- The hidden-object notice retires when the reviewer moves on, survives a reload, and a related return restores the filters -----
 // (b) `requested` is cleared on every selection move the page makes for the reviewer: a decision or rejection that advances (persist), a
 // save that had to move the selection, a queue step (Previous / Next / Skip through move), a related or grouped open, a return and a
 // replacement open. A canvas click records a new request instead. The render-time convergence never touches it: that render is the notice.
 assert.match(page,/if \(advance\) \{\s*setSelected\(nextId\);\s*setRequested\(null\);/,'a decision that advances retires the notice');
 assert.match(page,/if\(target!==selectedId\)\{setSelected\(target\);setRequested\(null\);/,'a save that moves the selection retires it');
 assert.match(page,/const move = \(delta: number\) => \{[\s\S]*?setSelected\(object\.id\);\s*setRequested\(null\);\s*setApexMode\(false\);/,'Previous / Next / Skip (move) retire it');
 assert.match(page,/const inspectRelated = [\s\S]*?setSelected\(object\.id\);setRequested\(null\);setKind\("all"\)/,'a related open retires it');
 assert.match(page,/const inspectGrouped=[\s\S]*?setSelected\(object\.id\);setRequested\(null\);setKind\("all"\)/,'a grouped open retires it');
 assert.match(page,/const returnToObject = [\s\S]*?setSelected\(object\.id\);setRequested\(null\);/,'a return retires it');
 assert.match(page,/replacement_ids\?\.map\(id=>[^\n]*setSelected\(id\);setRequested\(null\);/,'a replacement open retires it');
 // Repair after the code skeptic (2 Oct 22:50): a Skip of the LAST candidate-queue member falls through move() to the render-time
// convergence, and the edit flows (draw, replacement outlines, recovery, Undo) land the card on a new object; each is an explicit act
// of the reviewer, so each retires the notice too.
assert.match(page,/setSkipped\(\(old\) => new Set\(\[\.\.\.old, effective\.id\]\)\);\s*setRequested\(null\);\s*move\(1\);/,'Skip retires it before move(), so the last candidate-queue member is covered');
assert.match(page,/const drawObject = [\s\S]*?setSelected\(object\.id\);\s*setRequested\(null\);\s*setMode\("edit"\);/,'a drawn object retires it');
assert.match(page,/onApply=\{\(review,selected\)=>\{change\(review\);setSelected\(selected\);setRequested\(null\);/,'applied replacement outlines retire it');
assert.match(page,/const restoreSelection = [\s\S]*?setSelected\(target\);setRequested\(null\);setInstanceMode\(false\);/,'Undo (restoreSelection) retires it');
assert.match(page,/setSelected\(data\.recovery!\.review\.objects\.find\(o => o\.status !== "candidate"\)\?\.id \|\| null\);setRequested\(null\);/,'the recovery Keep earlier objects button retires it');
assert.equal((page.match(/setRequested\(null\)/g)||[]).length,12,'exactly those twelve moves clear it; the load effect and the canvas click set a request');
 assert(!/setSelected\(effective\?\.id \?\? null\);\s*setRequested/.test(page),'the render-time convergence does not clear it: that render is the notice');
 assert(page.includes('setRequested({id:objectId,filters:filterKeyOf(nextKind,"all",query,"all",frameFilter,lotScope)});'),'a canvas click records a new request');
 // (a) While the notice is up the URL names the asked-for object (object=), so a reload reproduces the notice; otherwise the shown object.
 assert(page.includes('const requestedHiddenId = requestedHidden?.id ?? null;'),'the hidden id is a stable dependency');
 assert(page.includes('const urlObject = requestedHiddenId ?? selectedId;'),'the URL names the hidden object while the notice is up, else the shown one');
 assert(page.includes('if (urlObject) params.set("object", urlObject);'),'written as object=');
 assert(!page.includes('if(selectedId)params.set("object",selectedId);'),'the old shown-object-only write is gone');
 assert(page.includes('}, [id, query, kind, status, issue, frameFilter, lotScope, selectedId, requestedHiddenId, loading, data?.model.id, addressCheck, layer, urlRestore]);'),'and the effect re-runs when the notice comes or goes');
 // The page's own replaceState cannot re-enter: applyUrl acts on a changed scene param only and never reads object= for the current scene.
 assert(page.includes('if (requested === urlScene.current || requested === id) return;'),'applyUrl ignores the page\'s own writes for the current scene');
 // (c) A related open widens the frame filter and the lot scope to all; Return to <object> brings each back only when the returned object
 // passes it (frameFilterAllows / lotScopeAllows), else leaves it at all so the return never lands behind the notice.
 assert.match(page,/useState<\{sceneId:string;objectId:string;kind:string;status:string;query:string;issue:string;layer:typeof layer;view:string;frameFilter:FrameFilter;lotScope:LotScope\}\|null>\(null\)/,'relatedReturn carries the frame filter and the lot scope');
 assert(page.includes('{sceneId:id,objectId:selectedId,kind,status,query,issue,layer,view,frameFilter,lotScope}'),'captured when a related object is opened');
 assert(page.includes('const frame=frameFilterAllows(object,data?.model,relatedReturn.frameFilter)?relatedReturn.frameFilter:"all";'),'the frame filter comes back only when it shows the returned object');
 assert(page.includes('const lots=lotScopeAllows(sceneLotRows[object.id],lotAnchor,relatedReturn.lotScope)?relatedReturn.lotScope:"all";'),'the lot scope likewise');
 assert(page.includes('setFrameFilter(frame);setLotScope(lots);'),'and both are restored on return');
 assert.match(page,/const inspectGrouped=[\s\S]*?setRelatedReturn\(null\);/,'a grouped open still clears the return point');
 console.log('scene lot scope: lotScopeValue defaults, resolveAddressLot from the route, the planned lot and null, resolveLotAnchor (address over planned over asked, two takeovers on two plans in scene order, carried slices, superseded and row-less objects skipped, non-window scenes never asked), isAskedRoof (buildings only: a merged driveway with replaces and a carried tree are not asked), lotTitle / lotAnchorTitle (Asked roof / Asked roofs joined with and, a blank lot number as Plan), lotScopeAllows under land/lot/all for a blank-lot anchor, for the address lot and a two-lot asked anchor, a same-plan lot, common property, another parcel, unknown objects and no address, lotScopeSummary counts, the advance order with the frame filter, a lot-blind matchesSceneQueue, filterExclusion (frame first, other land, in scope, unknown row, no address), the holdInView truth table, the editor pins and the hidden-object notice pins (retiring on a move, the URL keeping the hidden id, a related return restoring the frame filter and the lot scope) passed.');
} finally {
 await rm(directory,{recursive:true,force:true});
}
