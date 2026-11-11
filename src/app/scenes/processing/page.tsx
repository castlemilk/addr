import ProcessingWorkspace from "@/components/scenes/ProcessingWorkspace";
import "./processing.css";

export const metadata = { title: "Mooroolbark processing · GeoAU" };
export default async function ProcessingPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const initial = Object.fromEntries(Object.entries(params).flatMap(([key, value]) => typeof value === "string" ? [[key, value]] : []));
  return <ProcessingWorkspace initial={initial}/>;
}
