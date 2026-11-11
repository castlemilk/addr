import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,mkdir,rm} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
const directory=await mkdtemp(path.join(tmpdir(),'addr-candidate-checks-'));
const hash=text=>createHash('sha256').update(text).digest('hex');
const write=(file,value)=>writeFile(file,JSON.stringify(value));
try {
 for(const name of ['scene-candidate-checks','review-feedback','review-types','roof-annotations']) {
  const code=ts.transpileModule(await readFile(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll(/from "\.\/([a-z-]+)"/g,'from "./$1.mjs"');
  await writeFile(path.join(directory,`${name}.mjs`),code);
 }
 const {candidateChecks,readCandidateCheck}=await import(pathToFileURL(path.join(directory,'scene-candidate-checks.mjs')));
 const expected={id:'a'.repeat(16),modelHash:'b'.repeat(64),kind:'solar_panel',prompt:'array',scenes:[]};
 assert.deepEqual(await candidateChecks(directory,expected),{checks:[],unavailable_checks:0});
 const layer={scene_id:'qa_later',split:'validation',spatial_group:'area',revision:1,image_sha256:'c'.repeat(64),model_signature:'d'.repeat(64)};
 const config={schema_version:1,candidate_id:expected.id,candidate_model_sha256:expected.modelHash,kind:expected.kind,prompt:expected.prompt,dataset_version:'e'.repeat(16),layers:[layer]};
 const encoded=JSON.stringify(config),id=hash(encoded).slice(0,16),folder=path.join(directory,'checks',id);await mkdir(folder,{recursive:true});await writeFile(path.join(folder,'config.json'),encoded);
 assert.deepEqual(await candidateChecks(directory,expected),{checks:[],unavailable_checks:0},'An unfinished extraction is not a published check');
 const before={predictions:2,references:1,matched:1,extra:1,missed:0},after={predictions:0,references:1,matched:0,extra:0,missed:1};
 const artifact={check_id:id,result:{...layer,prompts:{array:{metrics:before}}},filtered:after};
 const artifactText=JSON.stringify(artifact);await writeFile(path.join(folder,`${layer.scene_id}.json`),artifactText);
 const report={check_id:id,candidate_id:expected.id,before:{...before,images:1,groups:1},after:{...after,images:1,groups:1},layers:[{before:{...before,...layer},after:{...after,...layer}}],scene_files:{[layer.scene_id]:hash(artifactText)}};
 await write(path.join(folder,'report.json'),report);
 const check=await readCandidateCheck(directory,id,expected);assert.equal(check.summary.scenes[0].lost_matches,1);assert.equal(check.summary.scenes[0].filtered,2);
 assert.equal((await candidateChecks(directory,expected)).checks.length,1);
 await assert.rejects(readCandidateCheck(directory,'../escape',expected));
 await assert.rejects(readCandidateCheck(directory,id,{...expected,modelHash:'f'.repeat(64)}));
 await assert.rejects(readCandidateCheck(directory,id,{...expected,prompt:'other prompt'}));
 await assert.rejects(readCandidateCheck(directory,id,{...expected,scenes:[{id:layer.scene_id}]}),'Previously selected images cannot be labelled additional');
 const inconsistent=structuredClone(report);inconsistent.before.matched=2;await write(path.join(folder,'report.json'),inconsistent);
 assert.equal((await candidateChecks(directory,expected)).unavailable_checks,1);
 await write(path.join(folder,'report.json'),report);await writeFile(path.join(folder,`${layer.scene_id}.json`),artifactText+' ');
 await assert.rejects(readCandidateCheck(directory,id,expected),'Changed predictions are detected');
 await writeFile(path.join(folder,`${layer.scene_id}.json`),artifactText);
 const changed=structuredClone(report);Object.assign(changed.before,{matched:0,extra:2,missed:1});Object.assign(changed.layers[0].before,{matched:0,extra:2,missed:1});await write(path.join(folder,'report.json'),changed);
 await assert.rejects(readCandidateCheck(directory,id,expected),'Internally consistent report still must match the saved predictions');
 console.log('Later validation: frozen identity, model/prompt binding, new-image scope, exact counts, changed predictions and unfinished/corrupt states passed.');
}finally{await rm(directory,{recursive:true,force:true});}
