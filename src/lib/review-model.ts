import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import path from "node:path";
import type { RoofAnnotation } from "./roof-annotations";

export function modelSignature(parts: Array<Record<string, unknown>>): string {
  const source = parts.map(p => {
    const xy=(p.ring_meters || p.ring || []) as number[][], z=(p.ring_z || p.z || []) as number[];
    return xy.map(([x,y],i)=>`${x.toFixed(5)},${y.toFixed(5)},${(z[i]??0).toFixed(5)}`).join(";");
  }).join("|");
  return createHash("sha256").update(source).digest("hex");
}
export async function auditAnnotation(annotation: RoofAnnotation, ring: number[][], eave: number) {
  return new Promise<{passed:boolean;issues:string[]}>((resolve,reject)=>{
    const child=spawn(path.join(process.cwd(),"scripts/ml/.venv/bin/python"),["-m","scripts.ml.annotation_check"],{cwd:process.cwd(),stdio:["pipe","pipe","pipe"]});
    let output="";
    const timer=setTimeout(()=>{child.kill();reject(new Error("Roof validation timed out"));},10000);
    child.stdout.on("data",data=>{output+=String(data);});
    child.stderr.resume();
    child.on("error",error=>{clearTimeout(timer);reject(error);});
    child.on("close",code=>{clearTimeout(timer);if(code!==0) reject(new Error("Roof validation failed"));else {try{resolve(JSON.parse(output));}catch{reject(new Error("Invalid roof validation response"));}}});
    child.stdin.on("error",()=>{});
    child.stdin.end(JSON.stringify({annotation,ring,eave}));
  });
}
