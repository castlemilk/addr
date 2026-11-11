import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,mkdir,rm} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';

const root=await mkdtemp(path.join(tmpdir(),'addr-roof-runs-'));
const hash=value=>createHash('sha256').update(value).digest('hex');
try {
  for(const name of ['scene-roof-runs','scene-roof-run-store','scene-training']) {
    const code=ts.transpileModule(await readFile(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll(/from "\.\/([a-z-]+)"/g,'from "./$1.mjs"');
    await writeFile(path.join(root,`${name}.mjs`),code);
  }
  const {roofTrainingRuns}=await import(pathToFileURL(path.join(root,'scene-roof-run-store.mjs')));
  const {roofRunGaps}=await import(pathToFileURL(path.join(root,'scene-roof-runs.mjs')));
  assert.deepEqual(await roofTrainingRuns(root),{runs:[],unavailable:0});
  const dataset='{"fixture":"frozen training data"}\n',version=hash(dataset).slice(0,16),id='a'.repeat(16);
  const folder=path.join(root,'scene-roofs',id),datasetPath=path.join(root,'scenes',version,'dataset.jsonl');
  await mkdir(folder,{recursive:true});await mkdir(path.dirname(datasetPath),{recursive:true});await writeFile(datasetPath,dataset);
  const counts=()=>Object.fromEntries(['train','validation','test'].map(split=>[split,Object.fromEntries(['hipped','gabled'].map(f=>[f,{roofs:3,images:3,areas:2}]))]));
  const config={schema_version:1,dataset_version:version,dataset_sha256:hash(dataset),requirements:Object.fromEntries(['train','validation','test'].map(s=>[s,{roofs_per_class:3,areas_per_class:2}]))};
  const score={ends:8,balanced_accuracy:.75,log_loss:.4};
  const report={schema_version:1,run_id:id,dataset_version:version,model_kind:'regularized-logistic-roof-end-classifier',
    status:'experimental-candidate',requirements:config.requirements,reviewed_roofs:counts(),usable_roofs:counts(),
    test_evaluation:'incomplete-visible-evidence',validation:score,validation_baseline:{...score,balanced_accuracy:.5,log_loss:.7},
    selection_weights_sha256:'b'.repeat(64),promotion_eligible:false,
    review_links:[{scene_id:'qa_train',object_id:'roof_train',family:'gabled',split:'train',url:'https://untrusted.invalid'},
      {scene_id:'qa_test',object_id:'roof_test',family:'gabled',split:'test'}],
    feature_exclusions:[{scene_id:'qa_train',object_id:'roof_train',reason:'unsupported_geometry'},
      {scene_id:'qa_test',object_id:'roof_test',reason:'insufficient_visible_evidence'}]};
  report.usable_roofs.test.gabled={roofs:2,images:2,areas:2};
  const model={validation:score,validated:false,provenance:{run_id:id,...config}};
  const files={'config.json':config,'features.json':[],'training-report.json':report,'roof-form.json':model};
  const publish=async()=>{
    const manifest={};
    for(const [name,value] of Object.entries(files)) {const text=JSON.stringify(value);await writeFile(path.join(folder,name),text);manifest[name]=hash(text);}
    await writeFile(path.join(folder,'artifacts.json'),JSON.stringify(manifest));
  };
  await publish();
  let response=await roofTrainingRuns(root),run=response.runs[0];assert.equal(response.unavailable,0);assert.equal(run.id,id);
  assert.deepEqual(roofRunGaps(run),[{split:'test',family:'gabled',roofs:1,areas:0,assessed:true}]);
  assert.equal(run.test,undefined,'Incomplete test evidence cannot become a test score');
  assert.equal(run.held_out_exclusions,1);assert.equal(run.exclusions.length,1);
  assert.equal(run.exclusions[0].url,'/scenes?scene=qa_train&object=roof_train&kind=building&status=all');
  assert(!JSON.stringify(run).includes('roof_test'),'Held-out object identities are not offered for tuning');
  await writeFile(path.join(folder,'training-report.json'),JSON.stringify(report)+' ');
  assert.deepEqual(await roofTrainingRuns(root),{runs:[],unavailable:1},'Changed report omitted');await publish();
  await writeFile(datasetPath,dataset+' ');assert.equal((await roofTrainingRuns(root)).unavailable,1,'Changed frozen dataset omitted');await writeFile(datasetPath,dataset);
  files['roof-form.json']={...model,validation:{...score,balanced_accuracy:1}};await publish();
  assert.equal((await roofTrainingRuns(root)).unavailable,1,'Model/report disagreement omitted');files['roof-form.json']=model;
  report.test={...score};await publish();assert.equal((await roofTrainingRuns(root)).unavailable,1,'Premature test metric omitted');delete report.test;
  report.test_evaluation='frozen-weights-report-only';report.test=score;model.test=score;await publish();
  assert.equal((await roofTrainingRuns(root)).runs[0].test.ends,8,'Completed frozen test score retained');
  delete report.test;delete model.test;delete report.validation;delete report.validation_baseline;
  report.status='needs-reviewed-labels';report.test_evaluation='not-run';report.usable_roofs.test={hipped:{roofs:0,images:0,areas:0},gabled:{roofs:0,images:0,areas:0}};
  report.usable_roofs.train.gabled={roofs:1,images:1,areas:1};report.feature_exclusions=report.feature_exclusions.slice(0,1);
  delete files['roof-form.json'];await rm(path.join(folder,'roof-form.json'));await publish();
  response=await roofTrainingRuns(root);run=response.runs[0];assert.equal(run.validation,undefined);
  assert.deepEqual(roofRunGaps(run),[{split:'train',family:'gabled',roofs:2,areas:1,assessed:true}], 'Unopened test features do not invent a zero-label shortage');
  report.usable_roofs.train.gabled.roofs=-1;await publish();assert.equal((await roofTrainingRuns(root)).unavailable,1);
  console.log('Roof training reports: immutable files/export, model binding, test isolation, readiness, missing data and corrupt states passed.');
} finally {await rm(root,{recursive:true,force:true});}
