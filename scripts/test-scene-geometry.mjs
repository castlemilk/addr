import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";
import * as THREE from "three";
const directory = await mkdtemp(path.join(tmpdir(), "addr-scene-geometry-"));
try {
  for (const name of [
    "scene-types",
    "scene-structure",
    "scene-region",
    "roof-annotations",
    "roof-line-edits",
    "roof-geometry",
    "scene-geometry",
    "scene-roof-mount",
    "scene-tree",
    "scene-car",
    "scene-direction",
    "scene-height",
    "scene-roof-feedback",
    "scene-image-frame",
    "scene-surface",
    "scene-solar",
    "scene-visibility",
    "polygon-clipping",
    "scene-instances",
    "scene-candidates",
    "scene-roof-lines",
    "scene-map-view",
  ]) {
    const code = ts
      .transpileModule(await readFile(`src/lib/${name}.ts`, "utf8"), {
        compilerOptions: {
          target: ts.ScriptTarget.ES2022,
          module: ts.ModuleKind.ES2022,
        },
      })
      .outputText.replaceAll(/from "\.\/([a-z-]+)"/g, 'from "./$1.mjs"')
      .replaceAll(
        'from "three"',
        `from ${JSON.stringify(import.meta.resolve("three"))}`,
      );
    await writeFile(path.join(directory, `${name}.mjs`), code);
  }
  const { polygonIssues, footprintMetrics, transformPoint, ringArea } = await import(
    pathToFileURL(path.join(directory, "scene-types.mjs"))
  );
  const { roofImageLines } = await import(pathToFileURL(path.join(directory,"scene-roof-lines.mjs")));
  {
    const frame=[[2,0],[0,-2],[100,200]];
    const roof={kind:"building",roof_lines:[{kind:"hip",line_meters:[[1,2],[3,4]]}],
      roof_annotation:{vertices:[[0,0,3],[4,0,5],[4,6,3]],edges:[{a:0,b:1,kind:"ridge"},{a:1,b:0,kind:"ridge"},
        {a:1,b:2,kind:"valley"},{a:0,b:2,kind:"eave"},{a:0,b:99,kind:"hip"}]}};
    assert.deepEqual(roofImageLines(roof,frame),[{a:[100,200],b:[108,200],kind:"ridge"},{a:[108,200],b:[108,188],kind:"valley"}],
      "Aerial lines use saved graph, deduplicate reversed edges and exclude eaves/invalid points");
    assert.deepEqual(roofImageLines({...roof,roof_annotation:null},frame),[{a:[102,196],b:[106,192],kind:"hip"}],"Legacy model lines keep the registered frame");
    assert.deepEqual(roofImageLines({...roof,roof_annotation:{vertices:[],edges:[]}},frame),[],"An empty saved graph must not resurrect old model edges");
    assert.deepEqual(roofImageLines({...roof,kind:"tree"},frame),[]);
  }
  const { populateScene, populateScenePrediction, setSceneIsolation } = await import(
    pathToFileURL(path.join(directory, "scene-geometry.mjs"))
  );
  const { sceneMapWindow } = await import(pathToFileURL(path.join(directory, "scene-map-view.mjs")));
  {
    const near = [145.310, -37.792, 145.314, -37.788], focus = [145.312, -37.790];
    assert.deepEqual(sceneMapWindow(near, focus), {bounds: near, limited: false});
    const wide = sceneMapWindow([145.29, -37.80, 145.35, -37.75], focus);
    assert(wide.limited, "A pitched horizon uses bounded nearby meshes instead of dropping every mesh");
    assert(wide.bounds[0] < focus[0] && wide.bounds[2] > focus[0]);
    assert(wide.bounds[1] < focus[1] && wide.bounds[3] > focus[1]);
    assert((wide.bounds[3] - wide.bounds[1]) * 111320 < 1201);
    const edge = sceneMapWindow([145.311, -37.791, 145.35, -37.75], focus);
    assert.equal(edge.bounds[0], 145.311); assert.equal(edge.bounds[1], -37.791);
  }
  const {roofMount,outlineSupport,parentRoofMesh}=await import(pathToFileURL(path.join(directory,"scene-roof-mount.mjs")));
  const {triangulateFootprint}=await import(pathToFileURL(path.join(directory,"roof-geometry.mjs")));
  const {convexIntersection}=await import(pathToFileURL(path.join(directory,"polygon-clipping.mjs")));
  const {treeSurfaces}=await import(pathToFileURL(path.join(directory,"scene-tree.mjs")));
  const {carSurfaces,carPlan}=await import(pathToFileURL(path.join(directory,"scene-car.mjs")));
  const {imageBearing,withObjectDirection}=await import(pathToFileURL(path.join(directory,"scene-direction.mjs")));
  const {withObjectHeight,heightDragPlane,heightOnPlane,heightIsThickness,objectHeightRange,withObjectRoofHeight,withObjectRoofAnnotation,sceneRoofGraph}=await import(pathToFileURL(path.join(directory,"scene-height.mjs")));
  const {pavedSurface}=await import(pathToFileURL(path.join(directory,"scene-surface.mjs")));
  const {solarPlane}=await import(pathToFileURL(path.join(directory,"scene-solar.mjs")));
  const {sceneVisibility}=await import(pathToFileURL(path.join(directory,"scene-visibility.mjs")));
  const {pointInRoof,roofHeightGroup,withRoofPointHeight,annotationIssues,annotationParts}=await import(pathToFileURL(path.join(directory,"roof-annotations.mjs")));
  const {lineageIssue,replaceInstances,selectionAfterRestore,availableParents}=await import(pathToFileURL(path.join(directory,"scene-instances.mjs")));
  const {regionBounds,regionIssue}=await import(pathToFileURL(path.join(directory,"scene-region.mjs")));
  const {candidateMatch,candidateDraft,sameCandidateFrame,candidateReviewURL,candidateAssessment}=await import(pathToFileURL(path.join(directory,"scene-candidates.mjs")));
  {
    const frame={image_width:512,image_height:512,pixel_to_meters:[[1,0],[0,1],[0,0]]};
    const make=(id,kind,ring,height,extra={})=>({id,kind,label:id,ring,status:'candidate',geometry_reviewed:false,visibility:'full',source:'fixture',
      model:{height_m:height,base_m:1,color:'#8ce2ba',height_source:'prior',height_verified:false,...extra}});
    const roof=make('compare-roof','building',[[0,0],[12,0],[12,8],[0,8]],4);
    roof.roof_annotation={schema_version:1,roof_type:'gabled',vertices:[[0,0,4],[12,0,4],[12,8,4],[0,8,4],[0,4,7],[12,4,7]],
      faces:[[0,1,5,4],[4,5,2,3]],edges:[{a:4,b:5,kind:'ridge'}]};
    const panel={...make('compare-panel','solar_panel',[[1,1],[4,1],[4,2],[1,2]],.15),parent_id:roof.id};
    const tree=make('compare-tree','tree',[[25,25],[35,25],[35,35],[25,35]],8);
    const road=make('compare-road','road',[[0,20],[20,20],[20,23],[0,23]],.15,{surface:{profile:'crowned',grade_percent:3,crossfall_percent:2,heading_deg:90}});
    const car=make('compare-car','car',[[25,0],[30,0],[30,2],[25,2]],1.7,{car:{profile:'car',heading_deg:90}});
    const original=[roof,panel,tree,road,car],snapshot=structuredClone(original),visible=new Set(original.map(o=>o.kind));
    const predicted=new THREE.Group(),regular=new THREE.Group(),edited=new THREE.Group();
    populateScenePrediction(predicted,original,frame,visible);
    populateScene(regular,original,frame,null,visible);
    const edits=original.map(o=>withObjectHeight(o,o.model.height_m+(o.kind==='solar_panel'?.1:2)));
    populateScene(edited,edits,frame,roof.id,visible);
    assert.deepEqual(original,snapshot,'Comparing predictions cannot mutate annotations or approvals');
    const meshes=root=>root.children.filter(o=>o instanceof THREE.Mesh);
    assert.equal(meshes(predicted).length,meshes(regular).length);
    for(let i=0;i<meshes(regular).length;i++) {
      const a=meshes(predicted)[i],b=meshes(regular)[i];
      assert.deepEqual(Array.from(a.geometry.attributes.position.array),Array.from(b.geometry.attributes.position.array),'Original roof, crown, surface, vehicle and mounting geometry remain exact');
      assert.deepEqual(a.position.toArray(),b.position.toArray(),'Original base elevations stay registered');
      assert(a.material.transparent&&!a.material.depthWrite);
    }
    for(const id of original.map(o=>o.id)) {
      const bounds=root=>{const box=new THREE.Box3();for(const child of root.children)if(child.userData.objectId===id)box.expandByObject(child);return box;};
      assert(bounds(edited).max.z>bounds(predicted).max.z,'Edited geometry differs from the immutable comparison');
    }
    const ray=new THREE.Raycaster(new THREE.Vector3(6,4,50),new THREE.Vector3(0,0,-1));
    predicted.updateMatrixWorld(true);assert.equal(ray.intersectObjects(predicted.children,true).length,0,'Ghosts cannot intercept picking or height handles');
    assert(predicted.children.filter(o=>o instanceof THREE.LineSegments).every(o=>!o.material.depthTest&&o.renderOrder===2),'Original interior shapes remain visible as labelled X-ray lines');
    setSceneIsolation(predicted,original,roof.id);
    assert(predicted.children.every(o=>o.visible===[roof.id,panel.id].includes(o.userData.objectId)),'Isolation includes original attached panels');
    setSceneIsolation(predicted,original,null,new Set([tree.id,car.id]));
    assert(predicted.children.every(o=>o.visible===[tree.id,car.id].includes(o.userData.objectId)),'Group isolation compares exact members across classes');
    setSceneIsolation(predicted,original,null,undefined,new Set([road.id]));
    assert(predicted.children.every(o=>o.visible===(o.userData.objectId===road.id)),'Review layer scoping also hides prediction ghosts');
    const hidden=new THREE.Group();populateScenePrediction(hidden,original,frame,new Set(['solar_panel']));
    assert(hidden.children.length&&hidden.children.every(o=>o.userData.objectId===panel.id),'Hidden original parents still provide roof support without being displayed');
    const empty=new THREE.Group();populateScenePrediction(empty,[{...tree,status:'rejected'},{...car,status:'superseded'}],frame,visible);
    assert.equal(empty.children.length,0,'Inactive source objects do not acquire prediction geometry');
    const shallow=structuredClone(roof);shallow.roof_annotation.vertices[4][2]=4.15;shallow.roof_annotation.vertices[5][2]=4.15;
    const lowRoof=new THREE.Group();populateScenePrediction(lowRoof,[shallow],frame,visible);
    let ridgeFound=false;
    for(const edge of lowRoof.children.filter(o=>o instanceof THREE.LineSegments)) {
      const positions=edge.geometry.attributes.position.array;
      for(let i=0;i<positions.length;i+=6) {
        const a=Array.from(positions.slice(i,i+3)),b=Array.from(positions.slice(i+3,i+6));
        if(Math.abs(a[1]-4)<1e-5&&Math.abs(b[1]-4)<1e-5&&Math.abs(a[2]-4.15)<1e-5&&Math.abs(b[2]-4.15)<1e-5&&Math.abs(a[0]-b[0])>11.9)ridgeFound=true;
      }
    }
    assert(ridgeFound,'A shallow original roof ridge must remain visible in the comparison');
  }
  {
    const {withRoofFeedback,currentRoofFeedback,validRoofFeedback,roofLineworkIssue}=await import(pathToFileURL(path.join(directory,"scene-roof-feedback.mjs")));
    const roof={id:'roof',kind:'building',status:'confirmed',geometry_reviewed:true,ring:[[0,0],[10,0],[10,8],[0,8]],model:{height_m:3,height_verified:false},
      roof_annotation:{schema_version:1,roof_type:'hipped',vertices:[[0,0,3],[10,0,3],[10,8,3],[0,8,3],[0,4,6],[10,4,6]],faces:[[0,1,5,4],[4,5,2,3]],edges:[{a:4,b:5,kind:'ridge'}]}};
    const before=JSON.stringify(roof),feedback=withRoofFeedback(roof,{family:'gabled',linework:true});
    assert.equal(JSON.stringify(roof),before);assert(validRoofFeedback(feedback));
    assert.deepEqual(currentRoofFeedback(feedback),{family:'gabled',linework:true});
    assert.equal(feedback.roof_annotation.roof_type,'hipped','A family observation does not rewrite the mesh');
    assert.equal(feedback.status,'confirmed');assert(feedback.geometry_reviewed);assert.equal(feedback.model.height_verified,false);
    assert.equal(roofLineworkIssue(feedback,[[1,0],[0,1],[0,0]]),null);
    assert.deepEqual(currentRoofFeedback(withObjectRoofHeight(feedback,4,8)),{family:'gabled',linework:true},'Estimated Z is separate from checked XY');
    const moved=structuredClone(feedback);moved.roof_annotation.vertices[4][1]=3;
    assert.deepEqual(currentRoofFeedback(moved),{family:'gabled',linework:false});
    assert(currentRoofFeedback(withRoofFeedback(moved,{linework:true})).linework,'An explicit recheck binds the revised XY');
    const reshaped=structuredClone(feedback);reshaped.ring[0][0]=1;
    assert.deepEqual(currentRoofFeedback(reshaped),{});assert(validRoofFeedback(reshaped),'Stale observations remain audit data');
    assert.deepEqual(currentRoofFeedback(withRoofFeedback(feedback,{family:null})),{family:undefined,linework:true});
    assert.equal(withRoofFeedback(withRoofFeedback(feedback,{family:null}),{linework:false}).roof_feedback,undefined);
    assert(!validRoofFeedback({...feedback,kind:'tree'}));
    for(const invalid of [null,[],true,{family:'unknown',outline:{ring:roof.ring,holes:[]}},
      {...feedback.roof_feedback,linework:{vertices:[null]}},{...feedback.roof_feedback,linework:{...feedback.roof_feedback.linework,edges:[null]}},
      {...feedback.roof_feedback,linework:{...feedback.roof_feedback.linework,edges:[{a:0,b:999,kind:'ridge'}]}}])
      assert(!validRoofFeedback({...feedback,roof_feedback:invalid}));
  }
  {
    const {objectImageFrame,imageFrameScale}=await import(pathToFileURL(path.join(directory,"scene-image-frame.mjs")));
    const image={width:512,height:512},laptop={width:740,height:264};
    const road=[[112,4],[500,4],[500,88],[112,88]];
    const frame=objectImageFrame([road],image,laptop);
    assert(frame.width/frame.height>2.7,'A wide road uses the landscape viewport instead of a square crop');
    assert(imageFrameScale(frame,laptop)>2.5*264/512,'Road details are materially larger at the same card height');
    for(const viewport of [laptop,{width:366,height:388},{width:512,height:512}]) {
      for(const rings of [[road],[[[2,450],[19,450],[19,500],[2,500]]],[[[250,15],[265,15],[265,490],[250,490]]]]) {
        const f=objectImageFrame(rings,image,viewport);
        const points=rings.flat(),xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
        assert(Math.abs(f.x+f.width/2-(Math.min(...xs)+Math.max(...xs))/2)<1e-8&&Math.abs(f.y+f.height/2-(Math.min(...ys)+Math.max(...ys))/2)<1e-8,'The selected object stays centred even at an image edge');
        for(const [x,y] of rings.flat())assert(x>=f.x-1e-8&&x<=f.x+f.width+1e-8&&y>=f.y-1e-8&&y<=f.y+f.height+1e-8,'Framing never crops the selected outline');
      }
    }
    const predicted=[[20,40],[80,40],[80,70],[20,70]],edited=[[260,200],[320,200],[320,260],[260,260]];
    const comparison=objectImageFrame([predicted,edited],image,laptop);
    for(const [x,y] of [...predicted,...edited])assert(x>=comparison.x&&x<=comparison.x+comparison.width&&y>=comparison.y&&y<=comparison.y+comparison.height,'Both layers share an honest comparison frame');
    assert.deepEqual(objectImageFrame([],image,laptop),{x:0,y:0,...image});
    const rectangular=objectImageFrame([[[600,250],[630,300]]],{width:640,height:320},laptop);
    assert.equal(rectangular.x+rectangular.width/2,615);assert.equal(rectangular.y+rectangular.height/2,275);
    const clipped=objectImageFrame([[[0,100],[200,100],[200,200],[0,200]]],image,laptop,true);
    assert(clipped.x < -60 && clipped.x+clipped.width>224,'A clipped roof gains viewing room beyond the capture');
    assert.equal(clipped.x+clipped.width/2,70,'Extra context favours the clipped side without changing the outline');
  }
  {
    const original={id:'tree',kind:'tree',status:'confirmed',geometry_reviewed:true,ring:[[10,10],[30,10],[30,30],[10,30]],
      model:{height_m:6,base_m:2,height_source:'class-prior',height_verified:true,tree:{crown_profile:'spreading',crown_base_ratio:.25}}};
    const snapshot=structuredClone(original),changed=withObjectHeight(original,8);
    assert.equal(changed.model.height_m,8);assert.equal(changed.model.base_m,2);
    assert.equal(changed.model.height_source,'reviewer-estimate');assert.equal(changed.model.height_verified,false);
    assert.deepEqual(changed.model.tree,original.model.tree);assert.equal(changed.status,'confirmed');assert(changed.geometry_reviewed);
    assert.deepEqual(changed.ring,original.ring);assert.deepEqual(original,snapshot);
    for(const value of [6,-1,151,NaN,Infinity])assert.equal(withObjectHeight(original,value),original);
    const house={...original,kind:'building',roof_annotation:{vertices:[[0,0,6],[10,0,6],[5,4,8]],faces:[[0,1,2]],edges:[]}};
    const shifted=withObjectHeight(house,9);
    assert.deepEqual(shifted.roof_annotation.vertices,[[0,0,9],[10,0,9],[5,4,11]]);
    assert.deepEqual(house.roof_annotation.vertices,[[0,0,6],[10,0,6],[5,4,8]]);
    assert.deepEqual(objectHeightRange(house),{min:0,max:98});assert.equal(withObjectHeight(house,99),house,'A height edit must not produce unsaveable apexes');
    const stepped={...house,roof_annotation:{...house.roof_annotation,vertices:[[0,0,4],[10,0,6],[5,4,8]]}};
    assert.deepEqual(objectHeightRange(stepped),{min:2,max:98});assert.equal(withObjectHeight(stepped,1),stepped,'Do not flatten lower faces against zero');
    const decimal={...house,model:{...house.model,height_m:4.18},roof_annotation:{...house.roof_annotation,vertices:[[0,0,4.18],[10,0,4.18],[5,4,4.4799999999999995]]}};
    const limit=withObjectHeight(decimal,objectHeightRange(decimal).max);
    assert.equal(Math.max(...limit.roof_annotation.vertices.map(p=>p[2])),100,'Round-off at the upper roof limit must remain saveable');
    assert.equal(heightIsThickness('solar_panel'),true);assert.equal(heightIsThickness('tree'),false);
    for(const position of [[20,-30,25],[-30,10,35],[10,30,15]]) {
      const camera=new THREE.PerspectiveCamera(42,1.4,.1,2000);camera.up.set(0,0,1);camera.position.set(...position);camera.lookAt(2,3,4);camera.updateMatrixWorld();
      const anchor=new THREE.Vector3(2,3,8),plane=heightDragPlane(camera,anchor),project=p=>{const s=p.clone().project(camera);return new THREE.Vector2(s.x,s.y);};
      const start=heightOnPlane(camera,project(anchor),plane),end=heightOnPlane(camera,project(anchor.clone().setZ(10.5)),plane);
      assert(Math.abs(start-8)<1e-8);assert(Math.abs(end-start-2.5)<1e-8,'A rotated perspective drag must preserve metric height');
    }
    const overhead=new THREE.PerspectiveCamera();overhead.position.set(2,3,20);assert.equal(heightDragPlane(overhead,new THREE.Vector3(2,3,8)),null);
  }
  {
    const graph={schema_version:1,roof_type:'gabled',vertices:[[0,0,4],[10,0,4],[10,8,4],[0,8,4],[5,0,6],[5,8,6]],
      faces:[[0,4,5,3],[4,1,2,5]],edges:[{a:4,b:5,kind:'ridge'}]};
    const object={id:'roof-height',kind:'building',status:'confirmed',geometry_reviewed:true,visibility:'full',review_issues:['shape_3d'],
      ring:[[0,0],[10,0],[10,8],[0,8]],holes:[],roof_annotation:graph,model:{height_m:4,base_m:3,height_verified:true,height_source:'prior'}};
    const snapshot=structuredClone(object),edited=withObjectRoofHeight(object,4,7.25);
    assert.deepEqual(roofHeightGroup(graph,4),[4,5]);
    assert.deepEqual(edited.roof_annotation.vertices.map(p=>p[2]),[4,4,4,4,7.25,7.25]);
    assert.deepEqual(annotationIssues(edited.roof_annotation,object.ring),[],'A connected gable ridge must keep both slopes planar');
    assert.deepEqual(edited.roof_annotation.faces,graph.faces);assert.deepEqual(edited.roof_annotation.edges,graph.edges);
    assert.equal(edited.model.height_m,4);assert.equal(edited.model.base_m,3);assert.equal(edited.model.height_verified,false);
    assert.equal(edited.status,'confirmed');assert(edited.geometry_reviewed);assert.deepEqual(edited.review_issues,['shape_3d']);
    assert.deepEqual(edited.ring,object.ring);assert.deepEqual(edited.holes,object.holes);assert.deepEqual(object,snapshot);
    const panel={id:'mounted-panel',kind:'solar_panel',parent_id:object.id,ring:[[1,2],[4,2],[4,6],[1,6]],model:{height_m:.15,base_m:0,placement:'roof'}};
    const mounted=roofMount(panel,[object,panel],[[1,0],[0,1],[0,0]]),raised=roofMount(panel,[edited,panel],[[1,0],[0,1],[0,0]]);
    assert(mounted.positions.length&&raised.positions.length,'The labelled bank stays supported by the edited roof');
    assert(Math.max(...raised.positions.filter((_,i)=>i%3===2))>Math.max(...mounted.positions.filter((_,i)=>i%3===2))+.2,'Rooftop objects must follow the new ridge elevation');
    for(const value of [6,-1,101,NaN,Infinity])assert.equal(withObjectRoofHeight(object,4,value),object);
    for(const index of [-1,1.5,99])assert.equal(withObjectRoofHeight(object,index,7),object);
    assert.equal(withObjectRoofHeight({...object,kind:'tree'},4,7).roof_annotation,graph,'Other classes cannot acquire roof feedback');
    assert.equal(withObjectRoofAnnotation(object,graph),object);
    const corner=withRoofPointHeight(graph,0,5);
    assert.deepEqual(corner.vertices.slice(1),graph.vertices.slice(1));
    assert(annotationIssues(corner,object.ring).includes('Roof slopes need planar adjustment'),'Individual eave changes must expose the non-planar draft');
    const stepped={...graph,vertices:[...graph.vertices,[5,12,6.4]],edges:[...graph.edges,{a:5,b:6,kind:'ridge'}]};
    assert.deepEqual(roofHeightGroup(stepped,4),[4,5],'Do not flatten a sloping or stepped ridge');
    const branched={...graph,vertices:[...graph.vertices,[5,12,6],[8,8,6]],edges:[...graph.edges,{a:5,b:6,kind:'ridge'},{a:5,b:7,kind:'ridge'}]};
    assert.deepEqual(roofHeightGroup(branched,4),[4,5,6,7]);
    const fromModel={...object,roof_annotation:null,roof_parts:annotationParts(graph),roof_lines:[{line_meters:[[5,0],[5,8]],kind:'ridge'}],roof_type:'gabled'};
    const recovered=sceneRoofGraph(fromModel),index=recovered.vertices.findIndex(p=>p[0]===5&&p[1]===0);
    assert.equal(withObjectRoofHeight(fromModel,index,8).roof_annotation.vertices.filter(p=>p[2]===8).length,2,'Unedited model roof graphs support coupled height feedback');
  }
  {
    const affine=[[0,-2],[3,0],[100,-50]],center=[100,-50];
    assert.equal(imageBearing(center,[0,4],affine),90,'A rotated image must resolve to east in metres');
    assert.equal(imageBearing(center,[-5,0],affine),0,'Pixel left is north in this rotated frame');
    assert.equal(imageBearing(center,[5,0],affine),180);
    assert.equal(imageBearing(center,[0,-4],affine),270);
    assert.equal(imageBearing(center,[0,0],affine),null,'No direction at the anchor');
    assert.equal(imageBearing(center,[NaN,0],affine),null);
    const object={id:'direction',kind:'car',status:'confirmed',geometry_reviewed:true,review_issues:['shape_3d'],ring:[[0,0],[4,0],[4,2],[0,2]],
      model:{height_m:1.8,base_m:.2,height_verified:true,car:{profile:'van',cabin_length_ratio:.73,cabin_width_ratio:.81,cabin_offset_ratio:-.1}}};
    const before=structuredClone(object),edited=withObjectDirection(object,-90);
    assert.equal(edited.model.car.heading_deg,270);
    assert.equal(edited.model.car.profile,'van');assert.equal(edited.model.car.cabin_length_ratio,.73);
    assert.equal(edited.model.height_verified,false);assert.equal(edited.model.height_m,1.8);
    assert.equal(edited.status,'confirmed');assert.equal(edited.geometry_reviewed,true);
    assert.deepEqual(edited.review_issues,['shape_3d']);assert.deepEqual(edited.ring,object.ring);assert.deepEqual(object,before);
    const road={...object,kind:'road',model:{...object.model,surface:{profile:'crowned',grade_percent:4,crossfall_percent:2}}};
    assert.deepEqual(withObjectDirection(road,450).model.surface,{profile:'crowned',grade_percent:4,crossfall_percent:2,heading_deg:90});
    const solar={...object,kind:'solar_panel',model:{...object.model,solar:{clearance_m:.12}}};
    assert.deepEqual(withObjectDirection(solar,361.234,27.5).model.solar,{clearance_m:.12,tilt_deg:27.5,azimuth_deg:1.23});
    assert.equal(withObjectDirection(solar,90),solar,'An unavailable roof plane cannot become an invented tilt');
    assert.equal(withObjectDirection(solar,90,0),solar,'A level bank has no downhill direction');
    assert.equal(withObjectDirection(object,Infinity),object);
  }
  {
    const frame=[[1,0],[0,1],[0,0]], square=[[0,0],[20,0],[20,20],[0,20]];
    assert.equal(candidateReviewURL('image','solar_panel','edited'),'/scenes?scene=image&kind=solar_panel&status=all&object=edited');
    const metrics={images:3,groups:2,references:5,matched:5,extra:3,missed:0};
    assert.equal(candidateAssessment(metrics,{...metrics,extra:1}).status,'fewer-extras');
    assert.equal(candidateAssessment(metrics,{...metrics,matched:4,missed:1,extra:0}).status,'loses-matches','Lower extra counts must not conceal a lost reference');
    assert.equal(candidateAssessment(metrics,metrics).status,'unchanged');
    assert.equal(candidateAssessment(metrics,{...metrics,extra:4}).status,'more-extras');
    assert.equal(candidateAssessment(metrics,{...metrics,references:4}).status,'unavailable');
    assert.equal(candidateAssessment({...metrics,references:0},{...metrics,references:0}).status,'unavailable');
    const proposal={id:'predicted',kind:'solar_panel',label:'Solar bank',ring:square,status:'candidate',geometry_reviewed:false,
      model:{height_m:.2,base_m:0,height_source:'class-prior',height_verified:true,color:'#333333'}};
    const reference={...proposal,id:'edited',status:'confirmed',geometry_reviewed:true};
    assert.equal(candidateMatch(proposal,[reference],frame).id,'edited','Changed prediction IDs match the existing outline geometrically');
    assert.equal(candidateMatch(proposal,[{...reference,status:'rejected'}],frame).status,'rejected','Rejected annotations remain reachable for reconsideration');
    assert.equal(candidateMatch(proposal,[{...reference,status:'superseded'}],frame),null);
    assert.equal(candidateMatch(proposal,[{...reference,kind:'building'}],frame),null);
    const gap=[[2,2],[18,2],[18,18],[2,18]];
    assert.equal(candidateMatch({...proposal,ring:gap},[{...reference,holes:[gap]}],frame),null,'A courtyard is empty, not a matching footprint');
    const model={id:'image',image_sha256:'image-a',image_width:512,image_height:512,pixel_to_meters:frame,centroid:{lat:0,lon:0},model_signature:'model-a',objects:[reference],groups:[]};
    assert(sameCandidateFrame(model,{...model,model_signature:'new-model'}));
    assert(!sameCandidateFrame(model,{...model,pixel_to_meters:[[2,0],[0,2],[0,0]]}));
    assert(!sameCandidateFrame(model,{...model,image_sha256:'new-image'}));
    const data={model,review:{revision:7,model_signature:'model-a',objects:[reference],groups:[],reviewed_classes:['tree','solar_panel']}};
    const before=JSON.stringify(data),draft=candidateDraft(data,{...proposal,parent_id:'unknown',group_id:'unknown',holes:[gap]},'abcdef1234567890');
    assert.equal(JSON.stringify(data),before);assert.equal(draft.revision,7);assert.deepEqual(draft.objects[0],reference);
    assert.deepEqual(draft.reviewed_classes,['tree']);assert.equal(draft.objects[1].status,'needs_adjustment');
    assert.equal(draft.objects[1].geometry_reviewed,false);assert.equal(draft.objects[1].model.height_verified,false);
    assert.equal(draft.objects[1].parent_id,undefined);assert.equal(draft.objects[1].group_id,undefined);assert.deepEqual(draft.objects[1].holes,[gap]);
  }
  for(const lat of [-37.79044,0,75]){
    for(const size of [10,250])assert.equal(regionIssue(regionBounds(lat,145.31252,size)),null);
    for(const size of [9.9,250.1])assert(regionIssue(regionBounds(lat,145.31252,size)));
  }
  assert(
    polygonIssues([
      [0, 0],
      [10, 10],
      [0, 10],
      [10, 0],
    ]).length,
  );
  assert(
    polygonIssues([
      [0, 0],
      [10, 0],
      [10, 10],
      [5, 0],
      [0, 10],
    ]).length,
    "Touching nonadjacent edges are invalid",
  );
  assert(
    polygonIssues([
      [0, 0],
      [10, 0],
      [10, 0],
      [10, 10],
      [0, 10],
    ]).length,
    "Repeated vertices are invalid",
  );
  assert.deepEqual(
    polygonIssues([
      [0, 0],
      [10, 0],
      [10, 10],
      [0, 10],
    ]),
    [],
  );
  const frame = [
      [1, 0],
      [0, 1],
      [0, 0],
    ],
    ring = [
      [1, 1],
      [5, 1],
      [5, 3],
      [1, 3],
    ];
  const metrics = footprintMetrics(ring, frame);
  assert.equal(metrics.area_m2, 8);
  assert.equal(metrics.length_m, 4);
  assert.equal(metrics.width_m, 2);
  const template = {
    id: "test",
    ring,
    status: "candidate",
    model: {
      height_m: 7,
      base_m: 2,
      color: "#567854",
      rotation_deg: 0,
      length_m: 4,
      width_m: 2,
    },
  };
  const concaveCanopy=[[0,0],[10,0],[10,10],[7,10],[7,3],[3,3],[3,10],[0,10]];
  const pavement=[[0,0],[20,0],[20,6],[0,6]],surfaceModel={...template.model,base_m:2,height_m:.15};
  const surfaceArea=positions=>{
    let area=0;
    for(let i=0;i<positions.length;i+=9){const [ax,ay,,bx,by,,cx,cy]=positions.slice(i,i+9);area+=Math.abs((bx-ax)*(cy-ay)-(by-ay)*(cx-ax))/2;}
    return area;
  };
  const courtyardRing=[[0,0],[10,0],[20,0],[20,20],[0,20]],gap=[[6,6],[14,6],[14,14],[6,14]],holes=[gap];
  assert.deepEqual(polygonIssues(courtyardRing,512,512,holes),[]);
  for(const invalid of [null,[null],[[]],[[[5,5],[NaN,5],[6,6]]],
    [[[0,6],[14,6],[14,14],[0,14]]],[[[6,6],[22,6],[22,14],[6,14]]],
    [gap,[[7,7],[8,7],[8,8],[7,8]]],[gap,[[13,13],[16,13],[16,16],[13,16]]]])
    assert(polygonIssues(courtyardRing,512,512,invalid).length,'Invalid or overlapping gaps cannot be saved');
  const pointContactCases=[
    [[[0,10],[5,8],[5,12]]],
    [[[4,4],[8,4],[8,8],[4,8]],[[8,8],[12,8],[12,12],[8,12]]],
    [[[4,4],[8,4],[8,8],[4,8]],[[8,8],[12,8],[12,12],[8,12]],[[8,8],[5,10],[5,12],[7,12]]],
  ];
  for(const touchingGaps of pointContactCases)for(const reverse of [false,true]) {
    const inner=touchingGaps.map(h=>reverse?[...h].reverse():h);
    assert.deepEqual(polygonIssues(courtyardRing,512,512,inner),[],
      'An isolated contact between gaps or with the shell is a valid polygon');
    const mesh=triangulateFootprint(courtyardRing,inner);
    const positions=mesh.triangles.flatMap(t=>t.flatMap(i=>[...mesh.points[i],0]));
    const expected=400-inner.reduce((sum,h)=>sum+surfaceArea(h.slice(1,-1).flatMap((p,i)=>[...h[0],0,...p,0,...h[i+2],0])),0);
    assert(Math.abs(surfaceArea(positions)-expected)<1e-6,'Point contacts retain the exact triangulated visible area');
    for(let i=0;i<positions.length;i+=9)for(const h of inner) {
      const triangle=[0,3,6].map(k=>positions.slice(i+k,i+k+2));
      const intersection=convexIntersection(triangle,h);
      const area=Math.abs(intersection.reduce((sum,p,j)=>{const q=intersection[(j+1)%intersection.length];return sum+p[0]*q[1]-q[0]*p[1];},0))/2;
      assert(area<1e-6,'Triangulation must not fill point-touching gaps');
    }
  }
  for(const invalid of [
    [[[0,10],[10,3],[20,10],[10,17]]],
    [[[0,10],[5,5],[10,10],[5,15]],[[10,10],[15,5],[20,10],[15,15]]],
    [[[8,8],[12,8],[10,4]],[[12,8],[10,12],[16,12]],[[10,12],[8,8],[4,12]]],
    [[[4,4],[8,4],[8,8],[4,8]],[[8,4],[12,4],[12,8],[8,8]]],
  ])assert(polygonIssues(courtyardRing,512,512,invalid).length,
    'Shared edges and chains or cycles disconnecting the interior must remain invalid');
  assert.equal(footprintMetrics(courtyardRing,frame,holes).area_m2,336);
  assert.equal(footprintMetrics(courtyardRing,[[0,.2],[-.2,0],[100,-150]],holes).area_m2,13.44);
  assert(!pointInRoof([10,10],courtyardRing,holes));assert(pointInRoof([6,10],courtyardRing,holes));
  const noGapCoverage=positions=>{
    for(let i=0;i<positions.length;i+=9){
      const tri=[0,3,6].map(k=>positions.slice(i+k,i+k+2)),intersection=convexIntersection(tri,gap);
      const area=Math.abs(intersection.reduce((s,p,i)=>{const q=intersection[(i+1)%intersection.length];return s+p[0]*q[1]-q[0]*p[1];},0))/2;
      assert(area<1e-6,'No emitted roof/surface triangle may fill the courtyard');
    }
  };
  for(const reverse of [false,true])for(const reverseHole of [false,true]){
    const outer=reverse?[...courtyardRing].reverse():courtyardRing,inner=reverseHole?[...gap].reverse():gap;
    const before=JSON.stringify([outer,inner]),triangulated=triangulateFootprint(outer,[inner]);
    const projected=triangulated.triangles.flatMap(t=>t.flatMap(i=>[...triangulated.points[i],0]));
    assert.equal(surfaceArea(projected),336);noGapCoverage(projected);
    assert.equal(JSON.stringify([outer,inner]),before,'Triangulation must not mutate saved boundaries');
    for(const profile of ['plane','crowned'])for(const heading_deg of [0,37.5,90,212.5]){
      const shell=pavedSurface(outer,{...surfaceModel,surface:{profile,heading_deg,grade_percent:3.75,crossfall_percent:2.5}},[inner]);
      assert(Math.abs(surfaceArea(shell.top_positions)-336)<1e-7);noGapCoverage(shell.top_positions);
      const edges=new Map();
      for(let i=0;i<shell.positions.length;i+=9){
        const tri=[0,3,6].map(k=>shell.positions.slice(i+k,i+k+3));
        for(const p of tri)assert(p.every(Number.isFinite));
        for(let e=0;e<3;e++){
          const key=[tri[e],tri[(e+1)%3]].map(p=>p.map(v=>(Math.abs(v)<1e-7?0:v).toFixed(7)).join(',')).sort().join(':');
          edges.set(key,(edges.get(key)||0)+1);
        }
      }
      assert([...edges.values()].every(n=>n===2),`Gapped ${profile} has a closed shell, including inner walls`);
    }
  }
  const gapParent={...template,id:'courtyard',kind:'building',ring:courtyardRing,holes,
    model:{...template.model,height_m:3},roof_parts:[{ring:courtyardRing,z:courtyardRing.map(p=>3+.1*p[0])}]};
  const clippedRoof=parentRoofMesh(gapParent,frame);
  assert(Math.abs(surfaceArea(clippedRoof)-336)<1e-8);noGapCoverage(clippedRoof);
  for(let i=0;i<clippedRoof.length;i+=3)assert(Math.abs(clippedRoof[i+2]-3-.1*clippedRoof[i])<1e-8);
  const gapPanel={...template,id:'courtyard-panel',kind:'solar_panel',parent_id:'courtyard',ring:[[5,5],[15,5],[15,15],[5,15]],holes,
    model:{...surfaceModel,height_m:.15}};
  assert.equal(outlineSupport({...gapPanel,holes:[]},gapParent,frame).outside_outline_m2,64);
  assert.equal(roofMount({...gapPanel,holes:[]},[gapParent],frame).support.state,'outside_outline');
  const gapMounted=roofMount(gapPanel,[gapParent],frame);
  assert.equal(gapMounted.support.state,'supported');assert.equal(gapMounted.support.area_m2,36);
  assert(Math.abs(surfaceArea(gapMounted.solar.top_positions)-36)<1e-7);noGapCoverage(gapMounted.solar.top_positions);
  for(const kind of ['building','road','tree','car','solar_panel']){
    const meshRoot=new THREE.Group(),object={...gapParent,kind,visibility:'full'};
    populateScene(meshRoot,[object],{pixel_to_meters:frame,image_width:512,image_height:512},null,new Set([kind]));
    meshRoot.updateMatrixWorld(true);
    const ray=new THREE.Raycaster(new THREE.Vector3(10,10,100),new THREE.Vector3(0,0,-1));
    assert.equal(ray.intersectObject(meshRoot,true).length,0,`${kind} must leave the courtyard open in the rendered mesh`);
    if(kind==='car')assert.equal(meshRoot.children[0].userData.geometryRole,'visible-footprint');
    if(kind==='tree') {
      assert.deepEqual(meshRoot.children.map(m=>m.userData.geometryRole),['tree-crown','tree-trunk']);
      const treeBounds=new THREE.Box3().setFromObject(meshRoot);
      assert.equal(treeBounds.min.z,object.model.base_m);
      assert.equal(treeBounds.max.z,object.model.base_m+object.model.height_m);
    }
    if(kind==='building'){
      const walls=meshRoot.children[1].geometry.attributes.position.array;
      for(let i=0;i<walls.length;i+=3)if(walls[i+2]>0)
        assert(Math.abs(walls[i+2]-3-.1*walls[i])<1e-5,'Inner walls meet clipped roof planes at the correct height');
    }
  }
  const at=(surface,x,y)=>{
    for(let i=0;i<surface.top_positions.length;i+=3)if(Math.abs(surface.top_positions[i]-x)<1e-7&&Math.abs(surface.top_positions[i+1]-y)<1e-7)return surface.top_positions[i+2];
    assert.fail(`Surface has no vertex at ${x},${y}`);
  };
  const level=pavedSurface(pavement,surfaceModel);
  assert.equal(surfaceArea(level.top_positions),120);
  assert.equal(level.min_elevation,2.15);assert.equal(level.max_elevation,2.15);
  const slope=pavedSurface(pavement,{...surfaceModel,surface:{profile:'plane',heading_deg:90,grade_percent:5,crossfall_percent:2}});
  assert(Math.abs(at(slope,20,0)-at(slope,0,0)-1)<1e-8,'5% grade rises one metre over twenty metres east');
  assert(Math.abs(at(slope,0,6)-at(slope,0,0)-.12)<1e-8,'Positive cross slope raises the left (north) edge');
  const north=pavedSurface(pavement,{...surfaceModel,surface:{profile:'plane',heading_deg:0,grade_percent:-5}});
  assert(Math.abs(at(north,0,6)-at(north,0,0)+.3)<1e-8,'Negative grade falls along the north bearing');
  const crown=pavedSurface(pavement,{...surfaceModel,surface:{profile:'crowned',heading_deg:90,crossfall_percent:2}});
  assert(Math.abs(at(crown,0,3)-at(crown,0,0)-.06)<1e-8,'Crown must include a straight raised centre, not flatten through it');
  assert(Math.abs(at(crown,0,0)-at(crown,0,6))<1e-8,'Both crown edges have the same crossfall');
  // Reduced from a captured footpath: near-collinear diagonals used to emit
  // duplicate internal skirts despite having the correct projected area.
  const digitizedPath=[[-12.34950601026997,-12.00163260152997],[-10.78407567093997,-13.04525282774997],[-8.522898514129977,-14.784619871449962],[-7.6532149922799775,-15.132493280189962],[-16.00217680203996,-7.305341583539985],[-17.219733732629955,-6.435658061689985],[-17.567607141369955,-6.435658061689985],[-17.741543845739955,-6.957468174799985]];
  for(const outline of [pavement,concaveCanopy,digitizedPath,[[0,0],[4,0],[8,0],[10,0],[10,4],[8,4],[8,7],[10,7],[10,10],[0,10]]]) {
    for(const reversed of [false,true])for(const profile of ['plane','crowned'])for(const heading_deg of [0,37.5,90,212.5,303.02]) {
      const shape=pavedSurface(reversed?[...outline].reverse():outline,{...surfaceModel,surface:{profile,heading_deg,grade_percent:3.75,crossfall_percent:2.5}});
      const expectedArea=Math.abs(outline.reduce((sum,p,i)=>{const q=outline[(i+1)%outline.length];return sum+p[0]*q[1]-q[0]*p[1];},0))/2;
      assert(Math.abs(surfaceArea(shape.top_positions)-expectedArea)<1e-7,`Surface projection must cover the complete concave outline: ${profile}, ${heading_deg}, reverse ${reversed}, area ${surfaceArea(shape.top_positions)} / ${expectedArea}`);
      const edges=new Map();
      for(let i=0;i<shape.positions.length;i+=9) {
        const tri=[0,3,6].map(k=>shape.positions.slice(i+k,i+k+3));
        for(const p of tri)assert(p.every(Number.isFinite));
        for(let e=0;e<3;e++){const key=[tri[e],tri[(e+1)%3]].map(p=>p.map(v=>(Math.abs(v)<1e-7?0:v).toFixed(7)).join(',')).sort().join(':');edges.set(key,(edges.get(key)||0)+1);}
      }
      assert([...edges.values()].every(v=>v===2),`Pavement shell must have no open seams or internal walls: ${profile}, ${heading_deg}`);
      assert(Math.abs(Math.min(...shape.positions.filter((_,i)=>i%3===2))-2)<1e-7,'Base is the lowest underside');
    }
  }
  const zeroSurface=pavedSurface(pavement,{...surfaceModel,height_m:0,surface:{profile:'crowned',heading_deg:90,crossfall_percent:2}});
  assert.deepEqual(zeroSurface.positions,zeroSurface.top_positions,'Zero thickness emits a single surface');
  const roadRoot=new THREE.Group();populateScene(roadRoot,[{...template,kind:'road',ring:pavement,model:{...surfaceModel,surface:{profile:'plane',heading_deg:90,grade_percent:5}}}],{pixel_to_meters:frame},null,new Set(['road']));
  const roadBounds=new THREE.Box3().setFromObject(roadRoot);
  assert(Math.abs(roadBounds.min.z-2)<1e-5&&Math.abs(roadBounds.max.z-3.15)<1e-5,'Rendered road must use the edited slope');
  const carOutline=[[0,0],[5,0],[5,2],[0,2]],denseCar=[[0,0],[5,0],[5,2],[0,2],[0,1.8],[0,1.5],[0,1.2],[0,1],[0,.5]];
  const carModel={...template.model,height_m:1.6,base_m:3,car:{profile:'car',heading_deg:90}};
  const carShape=carSurfaces(carOutline,carModel);
  assert.deepEqual(carPlan(denseCar,carModel).cabin,carShape.cabin,'Uneven sampling along one edge must not move the cabin');
  assert.deepEqual(carPlan([...denseCar].reverse(),carModel).cabin,carShape.cabin,'Outline winding must not move the cabin');
  for(const profile of ['car','suv','van']) {
    const shell=carSurfaces(carOutline,{...carModel,car:{profile,heading_deg:90}});
    const coordinates=[...shell.roof,...shell.glass],edges=new Map();
    for(let i=0;i<coordinates.length;i+=9) {
      const tri=[0,3,6].map(k=>coordinates.slice(i+k,i+k+3));
      for(const p of tri){assert(p.every(Number.isFinite));assert(p[0]>=0&&p[0]<=5&&p[1]>=0&&p[1]<=2);assert(p[2]>=3&&p[2]<=4.6+1e-9);}
      for(let e=0;e<3;e++){const key=[tri[e],tri[(e+1)%3]].map(p=>p.map(v=>v.toFixed(7)).join(',')).sort().join(':');edges.set(key,(edges.get(key)||0)+1);}
    }
    assert([...edges.values()].every(v=>v===2),'Sloped vehicle cabin must be a closed shell');
    assert.equal(Math.max(...coordinates.filter((_,i)=>i%3===2)),4.6);
  }
  const forward=carPlan(carOutline,carModel),backward=carPlan(carOutline,{...carModel,car:{profile:'car',heading_deg:270}});
  assert(forward.front[0]>forward.center[0]&&backward.front[0]<backward.center[0],'Flip front must reverse the heading');
  const narrowCabin=carPlan([[0,0],[5,0],[5,2],[3,2],[3,1.1],[2,1.1],[2,2],[0,2]],carModel);
  assert(narrowCabin.scale<.98&&narrowCabin.issue,'A notch must shrink the cabin and show a fit notice');
  const noCabin=carPlan(concaveCanopy,{...carModel,car:{profile:'car',heading_deg:90,cabin_offset_ratio:0}});
  assert.equal(noCabin.cabin.length,0,'A cabin must not float over an empty notch');
  assert(noCabin.issue);
  assert.equal(carSurfaces(carOutline,{...carModel,height_m:0}).roof.length,0);
  const isolationRoot=new THREE.Group(),context=[{id:'car'},{id:'roof'},{id:'panel',parent_id:'roof'},{id:'nested',parent_id:'panel'}];
  const imageFrame={pixel_to_meters:frame,image_width:512,image_height:512};
  for(const kind of ['car','tree']) {
    const fragment={...template,kind,ring:[[0,1],[5,1],[5,3],[0,3]]};
    assert(sceneVisibility(fragment,imageFrame).footprintOnly,'Unassessed edge fragments must not imply a complete body');
    for(const visibility of ['occluded','truncated','occluded_truncated']) {
      const root=new THREE.Group();populateScene(root,[{...fragment,ring,visibility}],imageFrame,null,new Set([kind]));
      assert.equal(root.children.length,1);assert.equal(root.children[0].userData.geometryRole,'visible-footprint');
      const bounds=new THREE.Box3().setFromObject(root);assert.equal(bounds.min.z,2);assert.equal(bounds.max.z,2);
    }
    assert(!sceneVisibility({...fragment,visibility:'full'},imageFrame).footprintOnly,'An explicit full-visibility review can resolve a coincidental edge touch');
    assert(!sceneVisibility({...fragment,ring},imageFrame).footprintOnly,'Interior unassessed outlines retain the estimated preview');
    for(const visibility of [undefined,'occluded','truncated','occluded_truncated','full']) {
      const gapped={...fragment,ring:courtyardRing,holes,visibility};
      assert.equal(sceneVisibility(gapped,imageFrame).footprintOnly,kind==='car'||visibility!=='full',
        'Only an explicitly fully visible tree can use a complete crown around interior gaps');
    }
  }
  for(const object of context){const mesh=new THREE.Mesh();mesh.userData.objectId=object.id;isolationRoot.add(mesh);}
  setSceneIsolation(isolationRoot,context,'roof');
  assert.deepEqual(isolationRoot.children.filter(m=>m.visible).map(m=>m.userData.objectId),['roof','panel','nested'],'Isolation retains attached descendants');
  setSceneIsolation(isolationRoot,context,'car');
  assert.deepEqual(isolationRoot.children.filter(m=>m.visible).map(m=>m.userData.objectId),['car']);
  setSceneIsolation(isolationRoot,context,null,undefined,new Set(['panel']));
  assert.deepEqual(isolationRoot.children.filter(m=>m.visible).map(m=>m.userData.objectId),['panel'],'Layer scope hides unrelated meshes while retaining their geometry for roof support');
  setSceneIsolation(isolationRoot,context,'roof',undefined,new Set(['panel']));
  assert.deepEqual(isolationRoot.children.filter(m=>m.visible).map(m=>m.userData.objectId),['panel'],'Object/descendant isolation intersects the layer scope');
  setSceneIsolation(isolationRoot,context,null,new Set(['roof','car']));
  assert.deepEqual(new Set(isolationRoot.children.filter(m=>m.visible).map(m=>m.userData.objectId)),new Set(['roof','car']),'Group isolation shows exactly its members, not unrelated attached objects');
  setSceneIsolation(isolationRoot,context,null,new Set(['panel']));
  assert.deepEqual(isolationRoot.children.filter(m=>m.visible).map(m=>m.userData.objectId),['panel'],'A rooftop group can hide its parent without changing placement');
  setSceneIsolation(isolationRoot,context,null,new Set());
  assert(isolationRoot.children.every(m=>!m.visible),'An empty group must not expose all other scene objects');
  setSceneIsolation(isolationRoot,context,null);
  assert(isolationRoot.children.every(m=>m.visible),'Restoring surroundings reveals the unchanged scene');
  const review={revision:0,model_signature:'model',objects:[{...template,kind:'tree',geometry_reviewed:false}],groups:[],reviewed_classes:['tree']};
  const replacement=replaceInstances(review,{pixel_to_meters:frame},['test'],'split',{rings:[[[1,1],[3,1],[3,3],[1,3]],[[3,1],[5,1],[5,3],[3,3]]],children:{}},randomUUID);
  const roadReview={...review,objects:[{...review.objects[0],kind:'road',visibility:'full',model:{...surfaceModel,surface:{profile:'crowned',grade_percent:3,crossfall_percent:2}}}]};
  const roadSplit=replaceInstances(roadReview,{pixel_to_meters:frame},['test'],'split',{rings:[[[1,1],[3,1],[3,3],[1,3]],[[3,1],[5,1],[5,3],[3,3]]],children:{}},randomUUID);
  assert(roadSplit.review.objects.slice(1).every(o=>o.model.surface===undefined),'Split surfaces must not inherit a profile tied to the former full extent');
  assert.equal(roadSplit.review.objects[0].model.surface.profile,'crowned','The original slope remains available in replacement history');
  const solarReview={...review,objects:[{...review.objects[0],kind:'solar_panel',model:{...surfaceModel,solar:{tilt_deg:25,azimuth_deg:120,clearance_m:.12}}}]};
  const solarSplit=replaceInstances(solarReview,{pixel_to_meters:frame},['test'],'split',{rings:[[[1,1],[3,1],[3,3],[1,3]],[[3,1],[5,1],[5,3],[3,3]]],children:{}},randomUUID);
  assert(solarSplit.review.objects.slice(1).every(o=>o.model.solar===undefined),'Replacement banks need a fresh mounting-plane check');
  assert.equal(solarSplit.review.objects[0].model.solar.tilt_deg,25,'Original mounting feedback remains in history');
  assert(roadSplit.review.objects.slice(1).every(o=>o.visibility===undefined),'Replacement visibility needs a fresh assessment');
  assert.equal(lineageIssue(replacement.review.objects),null);
  assert.equal(replacement.review.objects[0].status,'superseded');
  assert.equal(replacement.review.objects[1].geometry_reviewed,false);
  assert.deepEqual(replacement.review.reviewed_classes,[]);
  replacement.review.objects[1].review_issues=['outline','height'];
  replacement.review.objects[2].review_issues=['instances','height'];
  const merged=replaceInstances(replacement.review,{pixel_to_meters:frame},[replacement.review.objects[2].id,replacement.review.objects[1].id],'merge',{rings:[ring],children:{}},randomUUID);
  assert.deepEqual(merged.review.objects.at(-1).review_issues,['outline','instances','height'],'Replacement review keeps unresolved flags from every source');
  assert.equal(lineageIssue(merged.review.objects),null);
  assert(merged.review.objects.at(-1).label.startsWith(replacement.review.objects[2].label),'Merge metadata follows the actively selected source, not storage order');
  assert.equal(selectionAfterRestore(merged.selected,merged.review.objects,replacement.review.objects),replacement.review.objects[2].id,'Undo follows the selected merge source, not an unrelated object');
  assert.equal(selectionAfterRestore(replacement.selected,replacement.review.objects,review.objects),'test','Discarding a split returns to its original outline');
  assert.equal(selectionAfterRestore(null,replacement.review.objects,review.objects),null,'An exhausted swipe queue must retain its filtered selection behaviour');
  const fragments=Array.from({length:64},(_,i)=>({...solarReview.objects[0],id:`fragment-${i}`,status:'confirmed',geometry_reviewed:true,visibility:'full',group_id:'bank',parent_id:'building'}));
  const traceReview={...review,objects:[{...template,id:'building',kind:'building'},...fragments],reviewed_classes:['solar_panel'],groups:[{id:'bank',label:'Solar bank'}]};
  const beforeTrace=JSON.stringify(traceReview);
  const traced=replaceInstances(traceReview,{pixel_to_meters:frame},fragments.map(o=>o.id),'trace',{rings:[ring],children:{}},randomUUID);
  const tracedObject=traced.review.objects.at(-1);
  assert.equal(lineageIssue(traced.review.objects),null,'Tracing supports many fragments without losing reciprocal history');
  assert.equal(tracedObject.derived_from.length,64);
  assert.equal(tracedObject.parent_id,'building');assert.equal(tracedObject.group_id,'bank');
  assert.equal(tracedObject.source,'reviewer-trace');assert.equal(tracedObject.status,'needs_adjustment');
  assert.equal(tracedObject.visibility,undefined);assert.equal(tracedObject.model.solar,undefined);
  assert.equal(tracedObject.model.height_verified,false);assert.equal(tracedObject.geometry_reviewed,false);
  assert.deepEqual(traced.review.reviewed_classes,[]);
  assert(traced.review.objects.slice(1,-1).every(o=>o.status==='superseded'&&!o.geometry_reviewed));
  assert.equal(JSON.stringify(traceReview),beforeTrace,'Trace preview must not mutate saved objects');
  assert.equal(selectionAfterRestore(traced.selected,traced.review.objects,traceReview.objects),fragments[0].id);
  const overLimit=structuredClone(traced.review.objects);
  overLimit.push({...fragments[0],id:'fragment-65',status:'superseded',geometry_reviewed:false,replacement_ids:[tracedObject.id]});
  overLimit.find(o=>o.id===tracedObject.id).derived_from.push('fragment-65');
  assert(lineageIssue(overLimit),'History fan-in remains bounded');
  const broken=structuredClone(replacement.review.objects);broken[0].replacement_ids=['missing'];
  assert(lineageIssue(broken));
  const cycle=structuredClone(replacement.review.objects);
  cycle[0].derived_from=[cycle[1].id];cycle[1].replacement_ids=['test'];cycle[1].status='superseded';
  assert.match(lineageIssue(cycle),/loop/);
  const inactiveRoot=new THREE.Group();populateScene(inactiveRoot,replacement.review.objects,{pixel_to_meters:frame},null,new Set(['tree']));
  assert(!inactiveRoot.children.some(mesh=>mesh.userData.objectId==='test'),'Replaced outlines must not render duplicate meshes');
  // Reduced from a captured canopy. The affine frame makes pixel-collinear
  // edges almost collinear; old crown subdivision lifted mismatched edges into
  // cracks despite its correct projected area.
  const canopyFrame=[[.08525623751657543,.0000010617630009436489],[-.000000178165381593688,-.08525796823458164],[-22.708065587594078,20.612394380528126]];
  const digitizedCanopy=[[29,9],[121,94],[121,97],[123,99],[166,93],[169,87],[179,31],[180,25],[178,23],[164,9],[98,0]].map(p=>transformPoint(p,canopyFrame));
  const digitizedGap=[[69,4],[69,5],[68,5]].map(p=>transformPoint(p,canopyFrame));
  const treeCases=[{ring:concaveCanopy,holes:[],area:72},
    {ring:courtyardRing,holes,area:336},
    {ring:digitizedCanopy,holes:[digitizedGap],area:ringArea(digitizedCanopy)-ringArea(digitizedGap)},
    ...pointContactCases.map(holes=>({ring:courtyardRing,holes,area:footprintMetrics(courtyardRing,frame,holes).area_m2}))];
  for(const treeCase of treeCases)for(const reverse of [false,true])for(const reverseHoles of [false,true]) {
    const canopy=reverse?[...treeCase.ring].reverse():treeCase.ring;
    const gaps=treeCase.holes.map(h=>reverseHoles?[...h].reverse():h),before=JSON.stringify([canopy,gaps]);
    let previous;
    for(const profile of ['conical','rounded','spreading']) {
      const tree=treeSurfaces(canopy,{...template.model,height_m:12,tree:{crown_profile:profile,crown_base_ratio:.4}},gaps);
      assert.equal(JSON.stringify([canopy,gaps]),before,'Crown generation preserves the saved annotation');
      assert(pointInRoof(tree.anchor,canopy,gaps),'A canopy must not place its trunk in a notch or gap');
      for(let step=0;step<64;step++) {
        const angle=2*Math.PI*step/64,p=[tree.anchor[0]+tree.trunk_radius*Math.cos(angle),tree.anchor[1]+tree.trunk_radius*Math.sin(angle)];
        assert(pointInRoof(p,canopy,gaps),'The whole trunk cross-section stays inside the visible canopy');
      }
      assert(Math.abs(tree.crown_base_m-4.8)<1e-6);
      let area=0,min=Infinity,max=-Infinity;
      const edges=new Map();
      for(let i=0;i<tree.positions.length;i+=9) {
        const tri=[0,3,6].map(k=>tree.positions.slice(i+k,i+k+3));
        assert(pointInRoof([tri.reduce((s,p)=>s+p[0],0)/3,tri.reduce((s,p)=>s+p[1],0)/3],canopy,gaps),'Canopy triangles must stay inside the drawn outline');
        for(const hole of gaps) {
          const intersection=convexIntersection(tri.map(p=>p.slice(0,2)),hole);
          const area=Math.abs(intersection.reduce((s,p,j)=>{const q=intersection[(j+1)%intersection.length];return s+p[0]*q[1]-q[0]*p[1];},0))/2;
          assert(area<1e-7,'Neither crown surface may cover any part of an interior gap');
        }
        if(i%18===0)area+=Math.abs((tri[1][0]-tri[0][0])*(tri[2][1]-tri[0][1])-(tri[1][1]-tri[0][1])*(tri[2][0]-tri[0][0]))/2;
        for(const p of tri){assert(p.every(Number.isFinite));min=Math.min(min,p[2]);max=Math.max(max,p[2]);}
        for(let edge=0;edge<3;edge++) {
          const key=[tri[edge],tri[(edge+1)%3]].map(p=>p.map(v=>v.toFixed(7)).join(',')).sort().join(':');
          edges.set(key,(edges.get(key)||0)+1);
        }
      }
      assert(Math.abs(area-treeCase.area)<1e-6,'The crown must cover the exact visible outline area');
      assert(Math.abs(min-6.8)<1e-6);assert.equal(max,14);
      assert([...edges.values()].every(count=>count===2),`Crown shells must be closed without cracks or overlapping internal edges: ${JSON.stringify([...edges].filter(([,count])=>count!==2).slice(0,4))}`);
      if(previous)assert.notDeepEqual(tree.positions,previous,'Crown feedback must change the actual mesh');
      previous=tree.positions;
    }
  }
  for (const kind of ["tree", "car", "building"]) {
    const root = new THREE.Group();
    populateScene(
      root,
      [{ ...template, kind }],
      { pixel_to_meters: frame },
      null,
      new Set([kind]),
    );
    const bounds = new THREE.Box3().setFromObject(root);
    assert(
      Math.abs(bounds.min.z - 2) < 1e-5,
      `${kind} base must match the edited elevation`,
    );
    assert(
      Math.abs(bounds.max.z - 9) < 1e-5,
      `${kind} top must match the edited height`,
    );
    root.traverse((mesh) => {
      if (mesh.geometry)
        assert(
          [...mesh.geometry.attributes.position.array].every(Number.isFinite),
        );
    });
  }
  const root = new THREE.Group(),
    roofParts = [
      {
        ring: [
          [1, 1],
          [5, 1],
          [5, 2],
          [1, 2],
        ],
        z: [3, 3, 5, 5],
      },
      {
        ring: [
          [1, 2],
          [5, 2],
          [5, 3],
          [1, 3],
        ],
        z: [5, 5, 3, 3],
      },
    ];
  populateScene(
    root,
    [
      {
        ...template,
        kind: "building",
        roof_parts: roofParts,
        model: { ...template.model, height_m: 3 },
      },
    ],
    { pixel_to_meters: frame },
    null,
    new Set(["building"]),
  );
  const bounds = new THREE.Box3().setFromObject(root);
  assert.equal(bounds.min.z, 2);
  assert.equal(bounds.max.z, 7, "Gable apex must follow base elevation");
  const parent={...template,id:'roof',kind:'building',roof_parts:roofParts,model:{...template.model,height_m:3}};
  const panel={...template,id:'panel',kind:'solar_panel',parent_id:'roof',ring:[[2,1.25],[4,1.25],[4,2.75],[2,2.75]],model:{...template.model,height_m:.15,base_m:3}};
  const fitted=roofMount(panel,[parent,panel],frame);
  assert.equal(fitted.mode,'roof');assert(fitted.positions.length>0);
  assert.equal(fitted.support.state,'supported');assert.equal(fitted.support.outside_outline_m2,0);assert.equal(fitted.support.uncovered_roof_m2,0);
  assert(Math.abs(fitted.min-7.05)<1e-8&&Math.abs(fitted.max-7.05)<1e-8,'A bank crossing a symmetric ridge stays planar and clears the apex');
  assert(Math.abs(fitted.solar.roof_variation_m-1.5)<1e-8);assert(fitted.issue,'A roof spanning multiple slopes must expose the mounting mismatch');
  const mountedRoot=new THREE.Group();populateScene(mountedRoot,[parent,panel],{pixel_to_meters:frame},null,new Set(['solar_panel']));
  const mountedBounds=new THREE.Box3().setFromObject(mountedRoot);
  assert(Math.abs(mountedBounds.min.z-7.05)<1e-5);assert(Math.abs(mountedBounds.max.z-7.2)<1e-5);
  assert.equal(mountedBounds.min.x,2);assert.equal(mountedBounds.max.x,4);
  const mapSubset=new THREE.Group();
  populateScene(mapSubset,[parent,panel],{pixel_to_meters:frame},null,new Set(["building","solar_panel"]),new Set([panel.id]));
  const subsetBounds=new THREE.Box3().setFromObject(mapSubset);
  assert.deepEqual(subsetBounds,mountedBounds,"Map deduplication must hide the parent mesh while preserving its mounting surface for solar panels");
  const raised={...parent,roof_annotation:{vertices:[[1,1,3],[5,1,3],[5,2,6],[1,2,6],[5,3,3],[1,3,3]],faces:[[0,1,2,3],[3,2,4,5]],edges:[],roof_type:'gabled',schema_version:1}};
  {
    const selectedRoot=new THREE.Group();
    populateScene(selectedRoot,[raised],{pixel_to_meters:frame},raised.id,new Set(['building']));
    const lines=selectedRoot.children.filter(o=>o.isLineSegments);
    assert.equal(lines.length,1,'The selected roof exposes its rendered creases and outline');
    const line=lines[0],points=line.geometry.attributes.position.array;
    let ridgeLength=0;
    for(let i=0;i<points.length;i+=6){
      const [ax,ay,az,bx,by,bz]=points.slice(i,i+6);
      const ridge=Math.abs(ay-2)<1e-5&&Math.abs(by-2)<1e-5&&Math.abs(az-6)<1e-5&&Math.abs(bz-6)<1e-5;
      const boundary=(Math.abs(ax-bx)<1e-5&&[1,5].some(x=>Math.abs(ax-x)<1e-5))||
        (Math.abs(ay-by)<1e-5&&[1,3].some(y=>Math.abs(ay-y)<1e-5));
      assert(ridge||boundary,'A two-plane gable must not acquire hip lines or triangulation diagonals');
      if(ridge)ridgeLength+=Math.hypot(bx-ax,by-ay);
    }
    assert(Math.abs(ridgeLength-4)<1e-5,'The entire ridge is shown once');
    assert.equal(line.position.z,raised.model.base_m,'Edge overlays use the same base as the roof');
    const unselectedRoot=new THREE.Group();populateScene(unselectedRoot,[raised],{pixel_to_meters:frame},null,new Set(['building']));
    assert(!unselectedRoot.children.some(o=>o.isLineSegments),'Other roofs retain their normal surface view');
    setSceneIsolation(selectedRoot,[raised],'other');assert(!line.visible,'Isolation also hides the roof overlay');
  }
  assert(Math.abs(roofMount(panel,[raised,panel],frame).max-8.05)<1e-8,'Apex feedback must update mounted objects immediately');
  const manual={...panel,model:{...panel.model,placement:'absolute'}};
  assert.equal(roofMount(manual,[parent,manual],frame).mode,'absolute');
  assert.equal(roofMount(panel,[{...parent,status:'rejected'},panel],frame).mode,'absolute');
  const partial={...panel,ring:[[0,1.25],[4,1.25],[4,2.75],[0,2.75]]};
  assert(roofMount(partial,[parent,partial],frame).issue,'Do not silently clip off an unsupported portion of an object');
  assert.equal(roofMount(partial,[parent,partial],frame).positions.length,0);
  const outside=roofMount(partial,[parent,partial],frame);
  assert.equal(outside.support.state,'outside_outline');assert(Math.abs(outside.support.outside_outline_m2-1.5)<1e-9);
  assert(roofMount(panel,[{...parent,roof_parts:[...roofParts,...roofParts]},panel],frame).issue,'Overlapping roof planes are ambiguous');
  const half={ring:[[1,1],[3,1],[3,3],[1,3]],z:[3,3,3,3]};
  const fullPanel={...panel,ring};
  assert(roofMount(fullPanel,[{...parent,roof_parts:[half,half]},fullPanel],frame).issue,'A gap and equal overlap must not masquerade as complete support');
  const overlapping=roofMount(fullPanel,[{...parent,roof_parts:[half,half]},fullPanel],frame);
  assert.equal(overlapping.support.state,'roof_overlap');assert.equal(overlapping.support.uncovered_roof_m2,undefined,'Do not present double-counted roof area as coverage');
  const gapped=roofMount(fullPanel,[{...parent,roof_parts:[half]},fullPanel],frame);
  assert.equal(gapped.support.state,'roof_gap');assert.equal(gapped.support.uncovered_roof_m2,4);
  const narrowParent={...parent,ring:[[1,1],[3,1],[3,3],[1,3]]};
  const staleRoof=roofMount(panel,[narrowParent,panel],frame);
  assert.equal(staleRoof.support.state,'outside_outline','Old roof triangles cannot override a narrower edited building outline');
  assert.equal(staleRoof.support.uncovered_roof_m2,0);assert.equal(staleRoof.positions.length,0);
  const notchedParent={...parent,ring:concaveCanopy},bridge={...panel,ring:[[2,2],[8,2],[8,8],[2,8]]};
  const notch=outlineSupport(bridge,notchedParent,frame);
  assert.equal(notch.area_m2,36);assert(Math.abs(notch.outside_outline_m2-20)<1e-9,'Containment must account for concave notches even when every corner is inside');
  const rotatedFrame=[[0,.2],[-.2,0],[100,-150]],scaledNotch=outlineSupport(bridge,notchedParent,rotatedFrame);
  assert(Math.abs(scaledNotch.outside_outline_m2-.8)<1e-8,'Coverage uses metre-space area through rotated pixel frames');
  const nested={...parent,id:'nested',parent_id:panel.id},grandchild={...parent,id:'grandchild',parent_id:nested.id};
  const choices=availableParents(panel,[panel,parent,nested,grandchild,{...parent,id:'gone',status:'rejected'},{...parent,id:'old',status:'superseded'},{...parent,id:'tree',kind:'tree'}]);
  assert.deepEqual(choices.map(o=>o.id),[parent.id],'Rooftop objects can choose active buildings, excluding self and all descendants');
  assert(availableParents({...panel,kind:'other'},[{...parent,kind:'tree'}]).length,'Other kinds retain cross-layer grouping');
  const flat=roofMount(panel,[{...parent,roof_parts:[]},panel],frame);
  assert.equal(flat.mode,'flat');assert(Math.abs(flat.min-5.05)<1e-8&&Math.abs(flat.max-5.05)<1e-8);
  const reversed={...panel,ring:[...panel.ring].reverse()};
  assert(Math.abs(roofMount(reversed,[parent,reversed],frame).max-7.05)<1e-8);
  assert(fitted.positions.every(Number.isFinite));
  const skylight={...panel,kind:'skylight'},glazing=roofMount(skylight,[parent,skylight],frame);
  assert.equal(glazing.min,5.5);assert.equal(glazing.max,7,'Skylights retain the existing roof-following geometry');
  const solarRing=[[0,0],[10,0],[10,4],[0,4]],lift=([x,y])=>[x,y,3+.2*x+.4*y];
  const supports=[[solarRing[0],solarRing[1],solarRing[2]],[solarRing[0],solarRing[2],solarRing[3]]].map(t=>t.map(lift));
  const exact=solarPlane(solarRing,{...surfaceModel,solar:{clearance_m:.12}},supports);
  assert(Math.abs(exact.tilt_deg-Math.atan(Math.hypot(.2,.4))*180/Math.PI)<1e-8);
  assert(Math.abs(exact.azimuth_deg-206.565051177)<1e-8);assert(exact.roof_variation_m<1e-8);
  for(let i=0;i<exact.top_positions.length;i+=3){const[x,y,z]=exact.top_positions.slice(i,i+3);assert(Math.abs(z-(3+.2*x+.4*y+.12+.15))<1e-8);}
  const [sa,sb,sc]=supports[0],middle=sa.map((v,i)=>(v+sb[i]+sc[i])/3);
  const subdivided=solarPlane(solarRing,{...surfaceModel,solar:{clearance_m:.12}},[[sa,sb,middle],[sb,sc,middle],[sc,sa,middle],supports[1]]);
  assert(Math.abs(subdivided.tilt_deg-exact.tilt_deg)<1e-8&&Math.abs(subdivided.min-exact.min)<1e-8,'Changing roof tessellation must not bias the fit');
  for(const outline of [solarRing,concaveCanopy,digitizedPath])for(const reversed of [false,true]){
    const shape=solarPlane(reversed?[...outline].reverse():outline,{...surfaceModel,solar:{tilt_deg:30,azimuth_deg:90}},[]);
    assert(Math.abs(shape.min-surfaceModel.base_m)<1e-8,'Independent elevation anchors the lowest underside');
    assert(Math.abs(surfaceArea(shape.top_positions)-footprintMetrics(outline,frame).area_m2)<.011);
    const planeConstant=shape.top_positions[2]+Math.tan(Math.PI/6)*shape.top_positions[0];
    for(let i=0;i<shape.top_positions.length;i+=3)assert(Math.abs(shape.top_positions[i+2]+Math.tan(Math.PI/6)*shape.top_positions[i]-planeConstant)<1e-8,'Every bank vertex must lie on one plane');
    const edges=new Map();for(let i=0;i<shape.positions.length;i+=9){const tri=[0,3,6].map(k=>shape.positions.slice(i+k,i+k+3));for(let e=0;e<3;e++){const key=[tri[e],tri[(e+1)%3]].map(p=>p.map(v=>(Math.abs(v)<1e-7?0:v).toFixed(7)).join(',')).sort().join(':');edges.set(key,(edges.get(key)||0)+1);}}
    assert([...edges.values()].every(v=>v===2),'Solar shells must be closed without internal walls');
  }
  const tiltedPanel={...panel,model:{...panel.model,solar:{tilt_deg:30,azimuth_deg:90,clearance_m:.2}}};
  const tilted=roofMount(tiltedPanel,[parent,tiltedPanel],frame);
  assert(tilted.solar.max_gap_m>.2&&tilted.issue);assert(Math.abs(tilted.solar.tilt_deg-30)<1e-8);
  assert(Math.abs(at(tilted.solar,4,1.25)-at(tilted.solar,2,1.25)+2*Math.tan(Math.PI/6))<1e-8,'East-facing bank falls toward east');
  const z0=tilted.solar.top_positions[2]-.15+Math.tan(Math.PI/6)*tilted.solar.top_positions[0];
  for(const x of [2,3,4])for(const y of [1.25,1.5,2,2.5,2.75])assert(z0-Math.tan(Math.PI/6)*x-(7-2*Math.abs(y-2))>=.2-1e-8,'The entire bank must clear the roof, including the interior ridge');
  const sheet=solarPlane(solarRing,{...surfaceModel,height_m:0},supports);assert.deepEqual(sheet.positions,sheet.top_positions,'Zero thickness is honoured as one plane');
  console.log(
    "Scene geometry: invalid intersections, dimensions, tree/car/building meshes, closed paved surfaces, planar solar banks, support clearance, tessellation invariance, replacement profiles and gable apex bounds passed.",
  );
} finally {
  await rm(directory, { recursive: true, force: true });
}
