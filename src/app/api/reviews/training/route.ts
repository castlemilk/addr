import { NextRequest, NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import { validCohort } from "@/lib/review-feedback";

const execute=promisify(execFile);
export const dynamic="force-dynamic";
async function status(cohort:string) {
  try {
    const manifest=JSON.parse(await readFile(path.join(process.cwd(),"data/training",cohort,"latest.json"),"utf8"));
    let training=null;
    try {training=JSON.parse(await readFile(path.join(process.cwd(),"data/training",cohort,manifest.version,"training-report.json"),"utf8"));}catch{}
    return {manifest,training};
  }catch{return {manifest:null,training:null};}
}
export async function GET(request:NextRequest) {
  const cohort=request.nextUrl.searchParams.get("cohort")||"roof_training_250";
  if(!validCohort(cohort))return NextResponse.json({error:"Invalid cohort"},{status:400});
  return NextResponse.json(await status(cohort));
}
export async function POST(request:NextRequest) {
  try {
    if(request.headers.get("origin")&&request.headers.get("origin")!==request.nextUrl.origin)return NextResponse.json({error:"Use this app to prepare training."},{status:403});
    const body=await request.json();
    if(!validCohort(body.cohort)||["all_roofs","crm_corrections"].includes(body.cohort))return NextResponse.json({error:"Choose a saved training review set."},{status:400});
    const service=process.env.GEO_SERVICE_URL||process.env.NEXT_PUBLIC_GEO_SERVICE_URL||"http://localhost:8085";
    const response=await fetch(`${service}/crm.v1.CrmService/WhoAmI`,{method:"POST",headers:{"Content-Type":"application/json",...(request.headers.get("authorization")?{Authorization:request.headers.get("authorization")!}:{})},body:"{}",signal:AbortSignal.timeout(5000)});
    if(!response.ok)return NextResponse.json({error:"Sign in with roof editing access."},{status:response.status===403?403:401});
    const principal=await response.json();
    if(!principal.scopes?.includes("crm.roof.write"))return NextResponse.json({error:"Roof editing access is required."},{status:403});
    const python=path.join(process.cwd(),"scripts/ml/.venv/bin/python");
    for(const trainingModule of ["scripts.ml.training_dataset","scripts.ml.train_roof_form"])await execute(python,["-m",trainingModule,"--cohort",body.cohort],{cwd:process.cwd(),timeout:60000,maxBuffer:1024*1024});
    return NextResponse.json(await status(body.cohort));
  }catch(error){console.error("Training preparation failed",error instanceof Error?error.message:"unknown error");return NextResponse.json({error:"Training preparation failed. Your annotations are safe; check the local training service."},{status:503});}
}
