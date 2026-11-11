import assert from 'node:assert/strict';
import {test} from 'node:test';
import {build} from 'esbuild';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';

test('anonymous browser can load only the exact sign-in shell; MCP and data remain protected',async t=>{
 const bundle=await build({entryPoints:[new URL('./worker.ts',import.meta.url).pathname],bundle:true,write:false,format:'esm',platform:'neutral',target:'es2022',mainFields:['module','main'],external:['cloudflare:*'],plugins:[{name:'no-container-startup',setup(b){b.onResolve({filter:/^@cloudflare\/containers$/},()=>({path:'container',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class Container{}',loader:'js'}));}}]});
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:bundle.outputFiles[0].text,compatibilityDate:'2026-10-02',bindings:{FIELDWORK_SHARED_WORKSPACES:'true'},serviceBindings:{ASSETS:async request=>{assert.equal(new URL(request.url).pathname,'/mcp/connect');return new Response(request.method==='HEAD'?null:'<html>Sign in to Fieldwork</html>',{headers:{'Content-Type':'text/html'}})}}}));t.after(()=>mf.dispose());
 for(const method of ['GET','HEAD']){const r=await mf.dispatchFetch('https://fieldwork.example/mcp/connect?state=synthetic',{method});assert.equal(r.status,200);assert.match(r.headers.get('content-type'),/text\/html/)}
 for(const [path,method]of[['/mcp','POST'],['/mcp/private','GET'],['/api/v1/projects','GET'],['/mcp/connect','POST']]){const r=await mf.dispatchFetch('https://fieldwork.example'+path,{method});assert.equal(r.status,401,path+' must stay authenticated')}
});
