# Analysed roof models on the main map

The map uses the CRM scene geometry through `/api/scenes/map`. These meshes
are distinct from the older LoD2 vector-tile fallback. Saved feedback is the
default; the source selector can show original model estimates.

On 25 September 2026 the live route repeatedly returned HTTP 503 at its
20-second worker deadline, leaving older map geometry and an earlier capture
set visible. The capture limit also favoured small, older training images over
the completed Mooroolbark tiles. A pitched viewport wider than 2.4 km removed
all detailed meshes even when the camera was close to nearby houses.

The loader now:

- Prioritises suburb tiles within its 24-capture proposal budget, ordered by
  distance to the camera focus. Saved corrections and rejections are considered
  separately; unchanged neighbours in those older reviews cannot crowd out
  current tiles. Model-only mode does not consume review geometry.
- Deduplicates via a spatial index and transports only visible render objects
  plus their mounting parents. Geometry, coordinate frames, gaps and heights
  remain exact; diagnostics and redundant roof parts are omitted in transit.
- Bounds nearby detail to 1.2 km when the distant horizon exceeds the supported
  request size. Navigating the map loads a new local set rather than downloading
  the whole suburb's meshes at once.
- Allows up to 60 seconds for a cold worker, reports loading failures, and shows
  an explicit loaded-roof count and 3D state. The 2D mode offers a direct
  **Show roof models in 3D** action.

Verification: 14 scene-map regressions, the shared scene-geometry checks,
TypeScript and production build passed. ESLint reported six existing warnings
in MapComponent and no errors. The previously failing live request returned
HTTP 200 in 20.45 seconds. The live view loaded 921 roofs in 3D. All 257 frozen
suburb scene hashes and the 27/24 Cambridge source/review hashes were unchanged.

These are display and loading changes. They do not retrain weights, apply
self-review candidates, approve annotations or establish measured 3D accuracy.
