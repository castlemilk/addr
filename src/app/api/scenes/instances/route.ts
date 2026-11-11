import { NextRequest, NextResponse } from "next/server";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { loadScene, objectScope, scenePrincipal, validateSceneReview } from "@/lib/scene-store";
import { MAX_TRACE_SOURCES, replaceInstances } from "@/lib/scene-instances";
import { ReviewError } from "@/lib/review-feedback";
import { polygonIssues, type ScenePoint } from "@/lib/scene-types";

export async function POST(request: NextRequest) {
  try {
    const principal=await scenePrincipal(request), body=await request.json();
    if(!body || !["split","merge","trace"].includes(body.operation) || !Array.isArray(body.ids) ||
      body.ids.length<(body.operation==="merge"?2:1) || body.ids.length>(body.operation==="trace"?MAX_TRACE_SOURCES:body.operation==="split"?1:8) || new Set(body.ids).size!==body.ids.length)
      throw new ReviewError(`Choose one outline to split, two to eight to merge, or up to ${MAX_TRACE_SOURCES} to trace again.`);
    const data=await loadScene(body.id), review=validateSceneReview(body.review,data.model);
    if(data.stale || review.revision!==(data.review?.revision||0)) throw new ReviewError("This scene changed. Reload before replacing outlines.",409);
    const sources=review.objects.filter(o=>body.ids.includes(o.id));
    if(sources.length!==body.ids.length || sources.some(o=>["rejected","superseded"].includes(o.status) || o.kind!==sources[0].kind))
      throw new ReviewError("Choose active outlines of the same object class.");
    const children=review.objects.filter(o=>body.ids.includes(o.parent_id) && !["rejected","superseded"].includes(o.status) && !body.ids.includes(o.id));
    if([...sources,...children].some(o=>!principal.scopes.includes(objectScope(o))))
      throw new ReviewError("Editing access is required for the selected outlines and their attached objects.",403);
    if(body.operation==="split" && (!Array.isArray(body.cut) || body.cut.length!==2 || body.cut.some((p:unknown)=>
      !Array.isArray(p) || p.length!==2 || p.some((v:unknown)=>typeof v!=="number" || !Number.isFinite(v)) ||
      p[0]<0 || p[1]<0 || p[0]>data.model.image_width || p[1]>data.model.image_height)))
      throw new ReviewError("Place both cut endpoints inside the image.");
    if(body.operation==="trace") {
      const issues=polygonIssues(body.outline?.ring,data.model.image_width,data.model.image_height,body.outline?.holes);
      if(issues.length)throw new ReviewError(issues[0]);
    }
    const payload={operation:body.operation,rings:sources.map(o=>o.ring),holes:sources.map(o=>o.holes||[]),cut:body.cut,outline:body.outline,children:children.map(o=>({id:o.id,ring:o.ring,holes:o.holes}))};
    const geometry=await new Promise<{rings:ScenePoint[][];holes:ScenePoint[][][];children:Record<string,number|null>}>((resolve,reject)=>{
      const child=spawn(path.join(process.cwd(),"scripts/ml/.venv/bin/python"),["-m","scripts.ml.scene_instances"],{cwd:process.cwd(),signal:request.signal});
      let stdout="";
      const timer=setTimeout(()=>{child.kill();reject(new ReviewError("Outline replacement timed out. Your draft is preserved.",503));},10000);
      child.stdout.on("data",chunk=>{stdout+=chunk.toString();if(stdout.length>2*1024*1024){child.kill();reject(new ReviewError("The replacement is too large.",422));}});
      child.stderr.resume();
      child.stdin.on("error",()=>{});
      child.on("error",error=>{clearTimeout(timer);reject(error);});
      child.on("close",code=>{
        clearTimeout(timer);
        try { const result=JSON.parse(stdout);if(code!==0)reject(new ReviewError(result.error||"The outlines could not be replaced.",422));else resolve(result); }
        catch {reject(new ReviewError("The outlines could not be replaced. Your draft is preserved.",422));}
      });
      child.stdin.end(JSON.stringify(payload));
    });
    const result=replaceInstances(review,data.model,body.ids,body.operation,geometry,randomUUID);
    if(JSON.stringify(result.review.reviewed_classes)!==JSON.stringify(review.reviewed_classes) && !principal.scopes.includes("crm.object.write"))
      throw new ReviewError("Object editing access is required to reopen completed layer reviews.",403);
    return NextResponse.json({...result,review:validateSceneReview(result.review,data.model)});
  } catch(error) {
    return NextResponse.json({error:error instanceof ReviewError?error.message:"Could not replace these outlines. Your draft is preserved."},
      {status:error instanceof ReviewError?error.status:500});
  }
}
