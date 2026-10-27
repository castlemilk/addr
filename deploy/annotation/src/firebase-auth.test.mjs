import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";
import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT } from "jose";

const code = ts.transpileModule(await readFile(new URL("./firebase-auth.ts",import.meta.url),"utf8"),{
  compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext},
}).outputText.replace('from "jose"',`from ${JSON.stringify(import.meta.resolve("jose"))}`);
const { verifyGoogleIdentity, firebaseAuthRoute, authenticateFirebaseRequest, firebaseHelperRoute, isFirebaseHelperPath, googleClientId } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const pair = await generateKeyPair("RS256");
const jwk = {...await exportJWK(pair.publicKey),kid:"fixture-key",alg:"RS256"};
const keys = createLocalJWKSet({keys:[jwk]});
const env = {
  FIREBASE_WEB_CONFIG:JSON.stringify({apiKey:"public-browser-key",authDomain:"fieldwork-test.firebaseapp.com",projectId:"fieldwork-test",appId:"1:123:web:test",private_key:"must-never-be-returned"}),
  FIREBASE_ALLOWED_EMAILS:"Reviewer@example.com",ANNOTATION_TOKEN:"fixture-workspace-secret-32-characters",
  ANNOTATION_ALLOWED_ORIGINS:"https://fieldwork.example",
};
const now = Math.floor(Date.now()/1000);
const claims = {iss:"https://securetoken.google.com/fieldwork-test",aud:"fieldwork-test",sub:"google-user",iat:now,exp:now+3600,auth_time:now-30,email:"reviewer@example.com",email_verified:true,firebase:{sign_in_provider:"google.com"}};
const signed = (overrides={})=>new SignJWT({...claims,...overrides}).setProtectedHeader({alg:"RS256",kid:jwk.kid}).sign(pair.privateKey);
const req = (token,path="/api/v1/projects",options={})=>new Request(`https://fieldwork.example${path}`,{...options,headers:{...(token?{Authorization:`Bearer ${token}`} : {}),...options.headers}});

test("Firebase requires a signed, current Google token for this project and an allowed verified email",async t=>{
  assert.deepEqual(await verifyGoogleIdentity(await signed(),env,keys),{uid:"google-user",email:"reviewer@example.com"});
  for (const [name,overrides] of Object.entries({
    expired:{exp:now-90},future:{iat:now+180},wrongProject:{aud:"other-project"},wrongIssuer:{iss:"https://attacker.example"},
    unverified:{email_verified:false},notAllowed:{email:"other@example.com"},nonGoogle:{firebase:{sign_in_provider:"password"}},
    missingAuthTime:{auth_time:undefined},futureAuthTime:{auth_time:now+300},emptySubject:{sub:""},longSubject:{sub:"x".repeat(129)},numericSubject:{sub:123},
  })) await t.test(name,async()=>{const token=await signed(overrides); await assert.rejects(()=>verifyGoogleIdentity(token,env,keys));});
});

test("rejects forged tokens and missing access configuration",async()=>{
  const forged = await new SignJWT(claims).setProtectedHeader({alg:"HS256"}).sign(new TextEncoder().encode("untrusted-attacker-key-of-enough-length"));
  await assert.rejects(()=>verifyGoogleIdentity(forged,env,keys));
  const token=await signed();
  for(const disabled of [{FIREBASE_ALLOWED_EMAILS:""},{FIREBASE_ALLOWED_EMAILS:"*@example.com"},{FIREBASE_WEB_CONFIG:"{}"}]) {
    await assert.rejects(()=>verifyGoogleIdentity(token,{...env,...disabled},keys));
  }
});

test("public config returns only public fields and stays disabled without an allowlist",async()=>{
  const response=await firebaseAuthRoute(req(null,"/api/auth/config"),env,keys);
  const body=await response.json();assert.equal(body.enabled,true);
  assert.deepEqual(Object.keys(body.firebase).sort(),["apiKey","appId","authDomain","projectId"]);
  assert.ok(!JSON.stringify(body).includes("reviewer"));assert.ok(!JSON.stringify(body).includes(env.ANNOTATION_TOKEN));
  assert.match(response.headers.get("Cache-Control"),/no-store/);
  const disabled=await firebaseAuthRoute(req(null,"/api/auth/config"),{...env,FIREBASE_ALLOWED_EMAILS:""},keys);
  assert.deepEqual(await disabled.json(),{enabled:false});
});

test("edge authorization protects assets and API routes without exposing the workspace credential",async()=>{
  const token=await signed();
  for(const path of ["/api/v1/projects","/api/v1/assets/"+"a".repeat(64),"/annotation.v1.AnnotationService/ListProjects","/mcp"]) {
    const forwarded=await authenticateFirebaseRequest(req(token,path,{headers:{Cookie:"should-not-forward=1"}}),env,keys);
    assert.ok(forwarded instanceof Request);assert.equal(forwarded.headers.get("Authorization"),`Bearer ${env.ANNOTATION_TOKEN}`);
    assert.equal(forwarded.headers.get("Cookie"),null);
    for(const bad of [null,"bad-token",await signed({email:"intruder@example.com"})]) {
      const response=await authenticateFirebaseRequest(req(bad,path),env,keys);assert.equal(response.status,401);
      assert.ok(!(await response.text()).includes(env.ANNOTATION_TOKEN));
    }
  }
  const original=req(env.ANNOTATION_TOKEN);assert.equal(await authenticateFirebaseRequest(original,env,keys),original);
  const body=JSON.stringify({id:"isolated-fixture",expectedRevision:2});
  const post=req(token,"/annotation.v1.AnnotationService/SaveItem",{method:"POST",body,headers:{"Content-Type":"application/json"}});
  const forwarded=await authenticateFirebaseRequest(post,env,keys);
  assert.equal(forwarded.method,"POST");assert.equal(await forwarded.text(),body);
});

test("same-origin session identity, hostile origin rejection and preflight",async()=>{
  const token=await signed();
  const session=await firebaseAuthRoute(req(token,"/api/auth/session"),env,keys);
  assert.deepEqual(await session.json(),{user:{uid:"google-user",email:"reviewer@example.com"}});
  assert.equal((await firebaseAuthRoute(req(null,"/api/auth/session"),env,keys)).status,401);
  assert.equal((await firebaseAuthRoute(req(token,"/api/auth/session",{method:"POST"}),env,keys)).status,405);
  assert.equal((await authenticateFirebaseRequest(req(token,undefined,{headers:{Origin:"https://hostile.example"}}),env,keys)).status,403);
  assert.equal((await firebaseAuthRoute(req(null,"/api/auth/config",{headers:{Origin:"https://hostile.example"}}),env,keys)).status,403);
  const preflight=req(null,undefined,{method:"OPTIONS"});assert.equal(await authenticateFirebaseRequest(preflight,env,keys),preflight);
  const browserPreflight=await firebaseAuthRoute(req(null,"/api/auth/session",{method:"OPTIONS",headers:{Origin:"https://fieldwork.example","Access-Control-Request-Headers":"authorization"}}),env,keys);
  assert.equal(browserPreflight.status,204);
  assert.equal(browserPreflight.headers.get("Access-Control-Allow-Headers"),"Authorization");
  const denied=await authenticateFirebaseRequest(req(null,undefined,{headers:{Origin:"https://fieldwork.example"}}),env,keys);
  assert.equal(denied.headers.get("Access-Control-Allow-Origin"),"https://fieldwork.example");
  const hostile=await firebaseAuthRoute(req(null,"/api/auth/session",{method:"OPTIONS",headers:{Origin:"https://hostile.example"}}),env,keys);
  assert.equal(hostile.status,403);assert.equal(hostile.headers.get("Access-Control-Allow-Origin"),null);
});

test("config hands the browser this Worker's host as authDomain unless the firebase mode is chosen",async()=>{
  const body=await (await firebaseAuthRoute(req(null,"/api/auth/config"),env,keys)).json();
  assert.equal(body.firebase.authDomain,"fieldwork.example");
  const firebase=await (await firebaseAuthRoute(req(null,"/api/auth/config"),{...env,FIREBASE_AUTH_DOMAIN_MODE:"firebase"},keys)).json();
  assert.equal(firebase.firebase.authDomain,"fieldwork-test.firebaseapp.com");
});

test("sign-in helper is proxied to <projectId>.firebaseapp.com only, without credentials or cookies",async()=>{
  const calls=[];
  const upstream=async(url,init)=>{calls.push({url,init});return new Response("<html>handler</html>",{status:200,headers:{"Content-Type":"text/html","Set-Cookie":"tracker=1","Cache-Control":"public, max-age=60"}});};
  assert.equal(isFirebaseHelperPath("/__/auth/handler"),true);assert.equal(isFirebaseHelperPath("/__/firebase/init.json"),true);
  assert.equal(isFirebaseHelperPath("/__/other"),false);assert.equal(isFirebaseHelperPath("/api/auth/config"),false);
  const response=await firebaseHelperRoute(req("some-bearer","/__/auth/handler?apiKey=k&authType=signInViaPopup",{headers:{Cookie:"session=secret",Accept:"text/html","User-Agent":"UA"}}),env,upstream);
  assert.equal(response.status,200);assert.equal(await response.text(),"<html>handler</html>");
  assert.equal(response.headers.get("Set-Cookie"),null);assert.equal(response.headers.get("Content-Type"),"text/html");
  assert.equal(calls.length,1);assert.equal(calls[0].url,"https://fieldwork-test.firebaseapp.com/__/auth/handler?apiKey=k&authType=signInViaPopup");
  const sent=calls[0].init.headers;assert.equal(sent.get("Authorization"),null);assert.equal(sent.get("Cookie"),null);
  assert.equal(sent.get("Accept"),"text/html");assert.equal(calls[0].init.redirect,"manual");
  for(const path of ["/__/auth/../../evil","/__/auth/iframe.js"]) {
    calls.length=0;await firebaseHelperRoute(req(null,path),env,upstream);
    if(calls.length) assert.ok(calls[0].url.startsWith("https://fieldwork-test.firebaseapp.com/"),calls[0].url);
  }
  assert.equal((await firebaseHelperRoute(req(null,"/__/auth/handler",{method:"POST",body:"x"}),env,upstream)).status,405);
  assert.equal((await firebaseHelperRoute(req(null,"/__/other"),env,upstream)).status,404);
  calls.length=0;assert.equal((await firebaseHelperRoute(req(null,"/__/auth/handler"),{...env,FIREBASE_WEB_CONFIG:"{}"},upstream)).status,404);
  assert.equal(calls.length,0);
  const head=await firebaseHelperRoute(req(null,"/__/auth/iframe",{method:"HEAD"}),env,upstream);assert.equal(head.status,200);assert.equal(await head.text(),"");
});

test("config carries the OAuth client ID for quick sign-in only when it is well formed and login is enabled",async()=>{
  const clientId="1046530555412-3gs210m74r2bosv5sj3lu111g20sliqs.apps.googleusercontent.com";
  const config=async extra=>(await firebaseAuthRoute(req(null,"/api/auth/config"),{...env,...extra},keys)).json();
  const body=await config({GOOGLE_OAUTH_CLIENT_ID:clientId});
  assert.equal(body.enabled,true);assert.equal(body.googleClientId,clientId);
  assert.deepEqual(Object.keys(body.firebase).sort(),["apiKey","appId","authDomain","projectId"]);
  assert.equal("googleClientId" in await config({}),false);
  for(const bad of ["",`${clientId} `,` ${clientId}`,`${clientId}\n`,clientId.toUpperCase(),"1046530555412-3gs210m74r2bosv5sj3lu111g20sliqs.apps.googleusercontent.com.evil.example",
    "evil.example","abc-def.apps.googleusercontent.com","1046530555412-.apps.googleusercontent.com","1046530555412-x_y.apps.googleusercontent.com",
    `1-${"a".repeat(130)}.apps.googleusercontent.com`,"https://1046530555412-abc.apps.googleusercontent.com"]) {
    assert.equal(googleClientId({GOOGLE_OAUTH_CLIENT_ID:bad}),null,JSON.stringify(bad));
    assert.equal("googleClientId" in await config({GOOGLE_OAUTH_CLIENT_ID:bad}),false,JSON.stringify(bad));
  }
  assert.deepEqual(await config({GOOGLE_OAUTH_CLIENT_ID:clientId,FIREBASE_ALLOWED_EMAILS:""}),{enabled:false});
});

test("Google's signing keys that cannot be loaded answer 503, never a refusal; token faults stay 401",async()=>{
  const token=await signed();
  const failures={timeout:Object.assign(new Error("request timed out"),{code:"ERR_JWKS_TIMEOUT"}),network:new TypeError("fetch failed"),
    badAnswer:Object.assign(new Error("Expected 200 OK from the JSON Web Key Set HTTP response"),{code:"ERR_JOSE_GENERIC"})};
  for(const [name,failure] of Object.entries(failures)){
    const broken=async()=>{throw failure;};
    for(const response of [await firebaseAuthRoute(req(token,"/api/auth/session"),env,broken),await authenticateFirebaseRequest(req(token),env,broken)]){
      assert.equal(response.status,503,name);
      assert.equal(response.headers.get("Retry-After"),"5",name);
      const body=await response.json();
      assert.equal(body.code,"unavailable",name);
      assert.ok(!JSON.stringify(body).includes(env.ANNOTATION_TOKEN),name);
    }
  }
  // An unknown key id is the token's fault: still refused.
  const otherKey=await new SignJWT(claims).setProtectedHeader({alg:"RS256",kid:"unknown-key"}).sign(pair.privateKey);
  assert.equal((await firebaseAuthRoute(req(otherKey,"/api/auth/session"),env,keys)).status,401);
  assert.equal((await authenticateFirebaseRequest(req(otherKey),env,keys)).status,401);
  // Not on the allowlist, with keys working: refused.
  assert.equal((await authenticateFirebaseRequest(req(await signed({email:"intruder@example.com"})),env,keys)).status,401);
  // A workspace token never needs Google's keys.
  const broken=async()=>{throw new TypeError("fetch failed");};
  assert.ok(await authenticateFirebaseRequest(req(env.ANNOTATION_TOKEN),env,broken) instanceof Request);
});
