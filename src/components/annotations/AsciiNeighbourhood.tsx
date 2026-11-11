"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Pause, Play } from "lucide-react";

type Point = [number, number, number];
type Pixel = { glyph: string; depth: number; light: number; accent: boolean };
const COLS = 154, ROWS = 116, CELL_X = 6, CELL_Y = 9;
const MOTION_QUERY = "(prefers-reduced-motion: reduce)";
const subscribeMotion = (update: () => void) => {
  const query = window.matchMedia(MOTION_QUERY);
  query.addEventListener("change", update);
  return () => query.removeEventListener("change", update);
};
const reducedMotion = () => window.matchMedia(MOTION_QUERY).matches;
const serverMotion = () => true;

/** A small orthographic rasteriser: every visible mark is a monospace character.
 * Geometry is rendered once; the animation only composites a lit scan over it. */
function neighbourhood() {
  const pixels: (Pixel | undefined)[] = Array(COLS * ROWS);
  // Account for the non-square character cells to keep the isometric houses
  // proportionate: a text row is taller than a character is wide.
  const project = ([x, y, z]: Point): Point => [COLS / 2 + (x - y) * 8.2, 48 + (x + y) * 3.2 - z * 6.5, x + y + z];
  const noise = (x: number, y: number) => ((Math.imul(x + 91, 374761393) ^ Math.imul(y + 47, 668265263)) >>> 0) % 997 / 997;
  function put(x: number, y: number, depth: number, glyph: string, light: number, accent = false) {
    x = Math.round(x); y = Math.round(y);
    if (x < 0 || x >= COLS || y < 0 || y >= ROWS) return;
    const index = y * COLS + x;
    if (!pixels[index] || depth >= pixels[index]!.depth) pixels[index] = { glyph, depth, light, accent };
  }
  function line(a: Point, b: Point, light: number, accent = false, dashed = false) {
    const p = project(a), q = project(b), dx = q[0] - p[0], dy = q[1] - p[1];
    const steps = Math.ceil(Math.max(Math.abs(dx), Math.abs(dy)) * 1.5);
    const glyph = Math.abs(dx) < Math.abs(dy) * .35 ? "|" : Math.abs(dy) < Math.abs(dx) * .22 ? "-" : dx * dy > 0 ? "\\" : "/";
    for (let i = 0; i <= steps; i++) {
      if (dashed && Math.floor(i / 4) % 3 === 0) continue;
      const t = i / Math.max(1, steps);
      put(p[0] + dx * t, p[1] + dy * t, p[2] + (q[2] - p[2]) * t + .06, i === 0 || i === steps ? "+" : glyph, light, accent);
    }
  }
  function triangle(a: Point, b: Point, c: Point, light: number, accent = false) {
    const p = project(a), q = project(b), r = project(c);
    const area = (q[1] - r[1]) * (p[0] - r[0]) + (r[0] - q[0]) * (p[1] - r[1]);
    if (Math.abs(area) < .001) return;
    const minX = Math.max(0, Math.floor(Math.min(p[0], q[0], r[0]))), maxX = Math.min(COLS - 1, Math.ceil(Math.max(p[0], q[0], r[0])));
    const minY = Math.max(0, Math.floor(Math.min(p[1], q[1], r[1]))), maxY = Math.min(ROWS - 1, Math.ceil(Math.max(p[1], q[1], r[1])));
    for (let y = minY; y <= maxY; y++) for (let x = minX; x <= maxX; x++) {
      const u = ((q[1] - r[1]) * (x - r[0]) + (r[0] - q[0]) * (y - r[1])) / area;
      const v = ((r[1] - p[1]) * (x - r[0]) + (p[0] - r[0]) * (y - r[1])) / area;
      if (u < 0 || v < 0 || u + v > 1) continue;
      const n = noise(x, y);
      put(x, y, u * p[2] + v * q[2] + (1 - u - v) * r[2], n > .78 ? ":" : n > .48 ? "." : "·", light * (.55 + n * .45), accent);
    }
  }
  function face(points: Point[], light: number, accent = false) {
    for (let i = 1; i < points.length - 1; i++) triangle(points[0], points[i], points[i + 1], light, accent);
    points.forEach((point, i) => line(point, points[(i + 1) % points.length], light * 1.5, accent));
  }
  function house(x: number, y: number, width: number, length: number, axis: "x" | "y", accent = false) {
    const h = 1.2, peak = 2.25, wall = accent ? .45 : .27, roof = accent ? .67 : .43;
    const a: Point = [x, y, h], b: Point = [x + width, y, h], c: Point = [x + width, y + length, h], d: Point = [x, y + length, h];
    face([[x, y, 0], [x + width, y, 0], b, a], wall, accent);
    face([[x + width, y, 0], [x + width, y + length, 0], c, b], wall * .8, accent);
    face([[x + width, y + length, 0], [x, y + length, 0], d, c], wall, accent);
    face([[x, y + length, 0], [x, y, 0], a, d], wall * .85, accent);
    // Openings are linework on the walls, not extra roof facets.
    for (let offset = .4; offset < width - .3; offset += .7) face([[x + offset, y + length + .01, .35], [x + offset + .25, y + length + .01, .35], [x + offset + .25, y + length + .01, .85], [x + offset, y + length + .01, .85]], wall * 1.15, accent);
    for (let offset = .4; offset < length - .3; offset += .7) face([[x + width + .01, y + offset, .35], [x + width + .01, y + offset + .25, .35], [x + width + .01, y + offset + .25, .85], [x + width + .01, y + offset, .85]], wall, accent);
    const r: Point = axis === "x" ? [x, y + length / 2, peak] : [x + width / 2, y, peak];
    const s: Point = axis === "x" ? [x + width, y + length / 2, peak] : [x + width / 2, y + length, peak];
    if (axis === "x") { face([a, b, s, r], roof * .7, accent); face([r, s, c, d], roof, accent); face([b, c, s], wall, accent); face([a, r, d], wall, accent); }
    else { face([a, r, s, d], roof, accent); face([r, b, c, s], roof * .75, accent); face([d, s, c], wall, accent); face([a, b, r], wall, accent); }
    line(r, s, accent ? 1 : .72, accent);
  }
  function tree(x: number, y: number, size: number) {
    line([x, y, 0], [x, y, size * 1.5], .36);
    for (let i = 0; i < 300; i++) {
      const theta = noise(i, 17) * Math.PI * 2, z = noise(i, 41) * 2 - 1;
      const radius = Math.sqrt(1 - z * z) * size * (.55 + noise(i, 8) * .45);
      const [u, v, depth] = project([x + Math.cos(theta) * radius * .65, y + Math.sin(theta) * radius * .65, size * (1.5 + z)]);
      put(u, v, depth, i % 9 === 0 ? "+" : i % 3 ? ":" : ".", .22 + noise(i, 3) * .32);
    }
  }
  // Ground samples and road remain deliberately quieter than the architecture.
  for (let y = 5; y < ROWS - 8; y += 2) for (let x = 5; x < COLS - 5; x += 3) {
    if (noise(x, y) > .36) put(x, y, -100, ".", .07 + noise(x, y) * .12);
  }
  face([[-6, 4.4, -.04], [6, 4.4, -.04], [6, 6.1, -.04], [-6, 6.1, -.04]], .08);
  line([-6, 4.4, 0], [6, 4.4, 0], .32);
  line([-6, 6.1, 0], [6, 6.1, 0], .32);
  line([-6, 5.25, 0], [6, 5.25, 0], .25, false, true);
  house(-4.5, -3.2, 2.8, 2.2, "x");
  house(.5, -4.7, 2.5, 2.4, "y");
  house(-5.2, 1, 2.2, 2.8, "y");
  house(-1.35, -.9, 3.6, 2.25, "x", true);
  house(.65, .25, 1.6, 2.8, "y", true);
  [[-2.1, -4.6, .8], [-.8, -4, 1.1], [4, -1.5, .9], [-4.6, -.3, .85], [-2, 2.7, .65], [3.6, 2.3, .75]].forEach(([x, y, size]) => tree(x, y, size));
  return pixels;
}

function renderCharacters(pixels: (Pixel | undefined)[], illuminated = false) {
  const canvas = document.createElement("canvas");
  canvas.width = COLS * CELL_X; canvas.height = ROWS * CELL_Y;
  const context = canvas.getContext("2d");
  if (!context) return canvas;
  context.textBaseline = "top";
  pixels.forEach((pixel, index) => {
    if (!pixel) return;
    const x = index % COLS, y = Math.floor(index / COLS);
    const fade = Math.min(1, x / 12, (COLS - x) / 12, y / 14, (ROWS - y) / 14);
    context.font = `${pixel.accent && pixel.light > .8 ? "600 10" : "9"}px ui-monospace, 'SFMono-Regular', Consolas, monospace`;
    context.fillStyle = pixel.accent ? "#a2ecf4" : illuminated ? "#b2d7ed" : "#a1b9d3";
    context.globalAlpha = Math.min(1, pixel.light * (illuminated ? 2.1 : 1.7)) * fade;
    context.fillText(pixel.glyph, x * CELL_X, y * CELL_Y);
  });
  return canvas;
}

export default function AsciiNeighbourhood() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [paused, setPaused] = useState(false);
  const reduced = useSyncExternalStore(subscribeMotion, reducedMotion, serverMotion);
  const stopped = paused || reduced;
  useEffect(() => {
    const canvas = canvasRef.current, context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    const pixels = neighbourhood(), base = renderCharacters(pixels), lit = renderCharacters(pixels, true);
    let frame = 0, last = 0, start = 0;
    function draw(time = 0) {
      if (!context || !canvas) return;
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(base, 0, 0);
      if (!stopped && time) {
        const position = (((time - start) / 12000 + .32) % 1) * (canvas.height + 240) - 120;
        context.save();
        context.beginPath(); context.rect(0, position - 48, canvas.width, 96); context.clip();
        context.drawImage(lit, 0, 0); context.restore();
        context.fillStyle = "#8fdfeb"; context.globalAlpha = .24;
        context.fillRect(28, position, canvas.width - 56, 1); context.globalAlpha = 1;
      }
    }
    function tick(time: number) {
      if (!start) start = time;
      if (time - last > 65) { draw(time); last = time; }
      frame = requestAnimationFrame(tick);
    }
    function visibility() {
      cancelAnimationFrame(frame);
      if (!document.hidden && !stopped) frame = requestAnimationFrame(tick);
      else draw();
    }
    draw(); visibility();
    document.addEventListener("visibilitychange", visibility);
    return () => { cancelAnimationFrame(frame); document.removeEventListener("visibilitychange", visibility); };
  }, [stopped]);

  return <section className="fw-login-art" aria-label="Illustrated neighbourhood">
    <div className="fw-login-crosshair" aria-hidden="true" />
    <canvas ref={canvasRef} className="fw-login-ascii" width={COLS * CELL_X} height={ROWS * CELL_Y} aria-hidden="true" />
    <div className="fw-login-art-footer"><p>A closer look at the world around us.</p>
      <button type="button" className="fw-login-motion" onClick={() => setPaused(!paused)} disabled={reduced} aria-label={reduced ? "Animation paused for reduced motion" : paused ? "Play ASCII animation" : "Pause ASCII animation"} aria-pressed={stopped} title={reduced ? "Follows your reduced-motion preference" : undefined}>{stopped ? <Play size={14} /> : <Pause size={14} />}<span>{stopped ? "Paused" : "Pause"}</span></button>
    </div>
  </section>;
}
