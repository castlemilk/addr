// Tokens first, then the page CSS, then the workspace (whose shell.css must load after reviews.css).
import "../../reviews/reviews.css";
import "./imagery.css";
import TemporalWorkspace from "@/components/scenes/TemporalWorkspace";
export const metadata = { title: "Imagery dates · GeoAU" };
export default async function ImageryPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  return <TemporalWorkspace scene={typeof params.scene === "string" ? params.scene : ""} initialRun={typeof params.run === "string" ? params.run : ""}/>;
}
