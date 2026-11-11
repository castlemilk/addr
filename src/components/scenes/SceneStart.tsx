"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { sceneTitle, type SceneSummary } from "@/lib/scene-queue";
import { readRecentScenes, type RecentScene } from "@/lib/recent-scenes";

/**
 * Entry page for /scenes when no scene is selected. It lists the saved scenes the API
 * already returns and links each row into the canonical editor (/scenes?scene=<id>).
 * Nothing here reads imagery, approves, rejects or changes a roof.
 */
export type SceneStartScene = Pick<SceneSummary, "id" | "title" | "address_id" | "objects" | "reviewed" | "stale"> & {
  property_title?: string;
  /** Not provided by the list API today; shown when a caller supplies it. */
  updated?: string;
};

export const TILE_PREFIX = "mooroolbark-";
export const COHORT = "roof_training_250";
export const TILE_ROW_LIMIT = 40;

export type SceneStartGroups = {
  houses: SceneStartScene[];
  tiles: SceneStartScene[];
  cohort: SceneStartScene[];
  captures: SceneStartScene[];
  other: SceneStartScene[];
};

const byTitle = (a: SceneStartScene, b: SceneStartScene) =>
  sceneTitle(a).localeCompare(sceneTitle(b), "en-AU", { numeric: true }) || a.id.localeCompare(b.id);
const byId = (a: SceneStartScene, b: SceneStartScene) => a.id.localeCompare(b.id, "en-AU", { numeric: true });

/** Group by id prefix: house-* captures, suburb tiles, cohort roofs, ad-hoc map captures, anything else. */
export function groupScenes(scenes: ReadonlyArray<SceneStartScene>): SceneStartGroups {
  const groups: SceneStartGroups = { houses: [], tiles: [], cohort: [], captures: [], other: [] };
  for (const scene of scenes) {
    if (scene.id.startsWith("house-")) groups.houses.push(scene);
    else if (scene.id.startsWith(TILE_PREFIX)) groups.tiles.push(scene);
    else if (scene.id.startsWith(`${COHORT}-`)) groups.cohort.push(scene);
    else if (scene.id.startsWith("map-")) groups.captures.push(scene);
    else groups.other.push(scene);
  }
  groups.houses.sort(byTitle);
  groups.tiles.sort(byId);
  groups.cohort.sort(byId);
  groups.captures.sort(byTitle);
  groups.other.sort(byTitle);
  return groups;
}

/** Suburb tiles are only listed against a typed id fragment; an empty query lists none. */
export function filterTiles(tiles: ReadonlyArray<SceneStartScene>, query: string): SceneStartScene[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  return tiles.filter(tile => tile.id.toLowerCase().includes(needle));
}

const dateFormat = new Intl.DateTimeFormat("en-AU", { day: "numeric", month: "short", year: "numeric" });
const formatDate = (value: string | undefined) => {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : dateFormat.format(date);
};
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

/** A row's counts are optional: a recently opened scene that is not in the current list has none to show. */
export type SceneStartRowScene = Pick<SceneStartScene, "id" | "title" | "address_id" | "property_title"> & Partial<Pick<SceneStartScene, "objects" | "reviewed" | "stale" | "updated">>;

function SceneRow({ scene, opened, note }: { scene: SceneStartRowScene; opened?: string; note?: string }) {
  const updated = formatDate(scene.updated), openedOn = formatDate(opened);
  const counts = typeof scene.objects === "number" && typeof scene.reviewed === "number" ? { objects: scene.objects, reviewed: scene.reviewed } : null;
  return (
    <li className="scene-start-row">
      <Link href={`/scenes?scene=${encodeURIComponent(scene.id)}`} className="scene-start-row-link">
        <span className="scene-start-row-title">{sceneTitle(scene)}</span>
        <span className="scene-start-row-meta">
          {counts && <span>{plural(counts.objects, "object", "objects")}</span>}
          {counts && <span>{counts.reviewed} of {counts.objects} reviewed</span>}
          {updated && <span>Updated {updated}</span>}
          {openedOn && <span>Opened {openedOn}</span>}
          {note && <span>{note}</span>}
          {scene.stale && <span className="review-notice scene-start-stale">Feedback needs recovery</span>}
        </span>
      </Link>
    </li>
  );
}

export type SceneStartProps = {
  /** The list the page already requested for its rail; null before the first response. */
  scenes: ReadonlyArray<SceneStartScene> | null;
  loading: boolean;
  error?: string;
  onRetry?: () => void;
};

export default function SceneStart({ scenes, loading, error = "", onRetry }: SceneStartProps) {
  const [recent] = useState<RecentScene[]>(() => readRecentScenes());
  const [tileQuery, setTileQuery] = useState("");
  const groups = useMemo(() => groupScenes(scenes || []), [scenes]);
  const listedById = useMemo(() => new Map((scenes || []).map(scene => [scene.id, scene])), [scenes]);
  const matchingTiles = useMemo(() => filterTiles(groups.tiles, tileQuery), [groups.tiles, tileQuery]);
  const total = scenes?.length || 0;
  const listed = !loading && !error && scenes !== null;
  return (
    <section className="scene-start" aria-labelledby="scene-start-heading">
      <div className="scene-start-heading">
        <h1 id="scene-start-heading">Open a scene</h1>
        {listed && total > 0 && <p>{plural(total, "saved scene", "saved scenes")}. Opening a scene does not change its saved feedback.</p>}
        <div className="scene-start-actions">
          <Link className="review-button review-primary" href="/scenes/new">Capture map area</Link>
          <Link className="review-button" href="/?selectScene=1">Choose on the map</Link>
        </div>
      </div>
      {loading && (
        <p className="scene-start-state" role="status"><Loader2 className="animate-spin" size={15} aria-hidden="true" />Listing saved scenes</p>
      )}
      {!loading && error && (
        <p className="review-error scene-start-state" role="alert">
          {error}{" "}
          {onRetry && <button type="button" className="review-text-button" onClick={onRetry}>Retry listing</button>}
        </p>
      )}
      {listed && total === 0 && <p className="scene-start-state">No saved scenes yet. Capture a map area to start.</p>}
      {(listed || recent.length > 0) && (total > 0 || recent.length > 0) && (
        <div className="scene-start-sections">
          <section className="scene-start-section" aria-labelledby="scene-start-recent">
            <div className="scene-start-section-title">
              <h2 id="scene-start-recent">Recently opened</h2>
              <p>{plural(recent.length, "scene", "scenes")} on this browser</p>
            </div>
            {recent.length === 0
              ? <p className="scene-start-note">Scenes you open are listed here.</p>
              : <ul className="scene-start-list">
                  {recent.map(entry => {
                    // Counts come from the list only; a stored title never invents objects or progress.
                    const listedScene = listedById.get(entry.id);
                    return <SceneRow key={entry.id} scene={listedScene || { id: entry.id, title: entry.title, address_id: null }} opened={entry.opened}
                      note={listed && !listedScene ? "Not in the saved list" : undefined} />;
                  })}
                </ul>}
          </section>
          {listed && (
            <>
              <section className="scene-start-section" aria-labelledby="scene-start-houses">
                <div className="scene-start-section-title">
                  <h2 id="scene-start-houses">Houses</h2>
                  <p>{plural(groups.houses.length, "scene", "scenes")} captured per address</p>
                </div>
                {groups.houses.length === 0
                  ? <p className="scene-start-note">No house captures yet. Select an address on the map to analyse one.</p>
                  : <ul className="scene-start-list is-bounded">{groups.houses.map(scene => <SceneRow key={scene.id} scene={scene} />)}</ul>}
              </section>
              <section className="scene-start-section" aria-labelledby="scene-start-tiles">
                <div className="scene-start-section-title">
                  <h2 id="scene-start-tiles">Suburb tiles</h2>
                  <p>{plural(groups.tiles.length, "tile", "tiles")} from the suburb scan</p>
                </div>
                <p className="scene-start-note">
                  Tile progress and roof inventory live in <Link href="/scenes/processing">Processing</Link>. Type part of a tile id to open one here.
                </p>
                <label className="scene-start-search">
                  Find a tile
                  <input
                    type="search"
                    aria-label="Find a suburb tile by id"
                    placeholder="118442-80417"
                    maxLength={100}
                    value={tileQuery}
                    onChange={event => setTileQuery(event.target.value)}
                  />
                </label>
                {tileQuery.trim() && (
                  matchingTiles.length === 0
                    ? <p className="scene-start-note" role="status">No tile ids contain that text.</p>
                    : <>
                        <p className="scene-start-note" role="status">
                          {matchingTiles.length > TILE_ROW_LIMIT
                            ? `Showing ${TILE_ROW_LIMIT} of ${matchingTiles.length} matching tiles. Add more of the id to narrow the list.`
                            : plural(matchingTiles.length, "matching tile", "matching tiles")}
                        </p>
                        <ul className="scene-start-list is-bounded">{matchingTiles.slice(0, TILE_ROW_LIMIT).map(scene => <SceneRow key={scene.id} scene={scene} />)}</ul>
                      </>
                )}
              </section>
              <section className="scene-start-section" aria-labelledby="scene-start-cohort">
                <div className="scene-start-section-title">
                  <h2 id="scene-start-cohort">Cohort roofs</h2>
                  <p>{plural(groups.cohort.length, "scene", "scenes")} from the roof training cohort</p>
                </div>
                <p className="scene-start-note">
                  Roof boundary review for this cohort is in <Link href={`/reviews?cohort=${COHORT}`}>Roof review</Link>. Rows open the scene editor.
                </p>
                {groups.cohort.length > 0 && <ul className="scene-start-list is-bounded">{groups.cohort.map(scene => <SceneRow key={scene.id} scene={scene} />)}</ul>}
              </section>
              {groups.captures.length > 0 && (
                <section className="scene-start-section" aria-labelledby="scene-start-captures">
                  <div className="scene-start-section-title">
                    <h2 id="scene-start-captures">Map captures</h2>
                    <p>{plural(groups.captures.length, "scene", "scenes")} captured from a drawn map area</p>
                  </div>
                  <ul className="scene-start-list is-bounded">{groups.captures.map(scene => <SceneRow key={scene.id} scene={scene} />)}</ul>
                </section>
              )}
              {groups.other.length > 0 && (
                <section className="scene-start-section" aria-labelledby="scene-start-other">
                  <div className="scene-start-section-title">
                    <h2 id="scene-start-other">Other scenes</h2>
                    <p>{plural(groups.other.length, "scene", "scenes")}</p>
                  </div>
                  <ul className="scene-start-list is-bounded">{groups.other.map(scene => <SceneRow key={scene.id} scene={scene} />)}</ul>
                </section>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
