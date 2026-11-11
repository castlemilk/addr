"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Loader2, MapPinned, Sparkles } from "lucide-react";
import MetricInput from "@/components/reviews/MetricInput";
import { getCrmToken } from "@/lib/crm-client";
import {
  regionBounds,
  regionIssue,
  type SceneBounds,
} from "@/lib/scene-region";
import "../../reviews/reviews.css";
import "../scenes.css";
// After the page CSS so shell.css (via WorkbenchNav) wins the equal-specificity .review-header row.
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";

export default function NewScenePage() {
  const router = useRouter(),
    request = useRef<AbortController | null>(null);
  const [lat, setLat] = useState(-37.79044),
    [lon, setLon] = useState(145.31252),
    [size, setSize] = useState(90),
    [picked, setPicked] = useState<SceneBounds | null>(null);
  const [provider, setProvider] = useState("google"),
    [prompt, setPrompt] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    const params = new URLSearchParams(window.location.search),
      raw = params.get("bounds");
    if (raw) {
      const bounds = raw.split(",").map(Number);
      const issue = regionIssue(bounds);
      if (issue) setError(issue);
      if (bounds.length === 4 && bounds.every(Number.isFinite) && bounds[0] < bounds[2] && bounds[1] < bounds[3] && Math.abs(bounds[0]) < 180 && Math.abs(bounds[2]) < 180 && Math.abs(bounds[1]) < 85 && Math.abs(bounds[3]) < 85) {
        const b = bounds as SceneBounds;
        setPicked(b);
        setLat((b[1] + b[3]) / 2);
        setLon((b[0] + b[2]) / 2);
        setSize(
          Math.round(
            Math.max(
              (b[2] - b[0]) *
                111320 *
                Math.cos((((b[1] + b[3]) / 2) * Math.PI) / 180),
              (b[3] - b[1]) * 111320,
            ),
          ),
        );
      }
    }
    return () => request.current?.abort();
  }, []);
  const capture = async (detect: boolean) => {
    const bounds = picked || regionBounds(lat, lon, size),
      issue = regionIssue(bounds);
    if (issue) {
      setError(issue);
      return;
    }
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    try {
      const token = getCrmToken();
      const response = await fetch("/api/scenes/extract", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          bounds,
          provider,
          prompt,
          capture_only: !detect,
        }),
        signal: controller.signal,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      router.push(`/scenes?scene=${encodeURIComponent(result.id)}`);
    } catch (e) {
      if (!controller.signal.aborted)
        setError(
          e instanceof Error
            ? e.message
            : "The image could not be captured. Your area is still selected.",
        );
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  return (
    <div className="review-shell scene-shell">
      {/* Shell header: the Map link returns to the area being captured. */}
      <WorkbenchHeader hrefs={{ map: `/?lat=${lat}&lon=${lon}` }} />
      <main className="scene-capture">
        <h1>Capture a map area</h1>
        <p>
          Capture an aerial image of the area and find its objects. Proposals
          open in scene review and stay unverified until reviewed.
        </p>
        <Link
          className="review-button"
          href={`/?lat=${lat}&lon=${lon}&selectScene=1`}
        >
          <MapPinned size={16} />
          Choose an area on the map
        </Link>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void capture(true);
          }}
        >
          <fieldset disabled={busy}>
            <legend>Area coordinates</legend>
            <div className="scene-capture-fields">
              <label>
                Latitude
                <MetricInput
                  aria-label="Scene latitude"
                  value={lat}
                  step=".00001"
                  onValueChange={(v) => {
                    setLat(v);
                    setPicked(null);
                  }}
                />
              </label>
              <label>
                Longitude
                <MetricInput
                  aria-label="Scene longitude"
                  value={lon}
                  step=".00001"
                  onValueChange={(v) => {
                    setLon(v);
                    setPicked(null);
                  }}
                />
              </label>
              <label>
                Width (metres)
                <MetricInput
                  aria-label="Scene width in metres"
                  value={size}
                  min="10"
                  max="250"
                  onValueChange={(v) => {
                    setSize(v);
                    setPicked(null);
                  }}
                />
              </label>
            </div>
            <p className="review-muted">
              Choose 10–250 metres. A square image adds context around a
              rectangular selection. Smaller areas retain more detail for cars
              and solar panels.
            </p>
            <label>
              Imagery
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
              >
                <option value="google">Google aerial</option>
                <option value="esri">ESRI World Imagery</option>
              </select>
            </label>
            <label>
              Additional object to find
              <input
                value={prompt}
                maxLength={150}
                placeholder="e.g. garden shed"
                onChange={(e) => setPrompt(e.target.value)}
              />
            </label>
            <div className="scene-capture-actions">
              <button className="review-button review-primary" type="submit">
                {busy ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <Sparkles size={16} />
                )}
                Capture and find objects
              </button>
              <button
                className="review-button"
                type="button"
                onClick={() => void capture(false)}
              >
                Capture for manual annotation
              </button>
            </div>
          </fieldset>
          <div className="scene-status-space" aria-live="polite">
            {error ? (
              <p className="review-error" role="alert">
                {error}
              </p>
            ) : busy ? (
              <p role="status">
                Preparing the image and object proposals. This can take a few
                minutes.
              </p>
            ) : (
              <p className="review-muted">
                Proposals remain unverified until reviewed. Existing scenes and
                corrections are preserved.
              </p>
            )}
          </div>
        </form>
      </main>
    </div>
  );
}
