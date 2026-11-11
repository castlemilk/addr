# Editable roof-line feedback

In **Scenes**, select a building and choose **Edit roof lines**. The editor starts
with its current estimate. Use **Move lines** to drag an endpoint/junction or a
whole line. Shared points move adjoining lines. Use **Draw roof line** and click
two endpoints to add a ridge/apex, hip or valley/saddle. Crossings become shared
junctions; **Add junction** splits a selected line so it can bend. Lines snap to
nearby points and the captured eaves. Arrow keys move the selection by 10 cm.

Select a line to change its type or delete it. **Clear lines**, **Reset to starting
estimate**, undo/redo and the dashed starting-estimate comparison support larger
corrections. Lines cannot cross outside the captured outline or through a gap.
Escape cancels an unfinished line or drag. **3D apexes** retains the existing
height/roof-face editor separately.

**Save roof line feedback** uses the canonical scene save route, permissions,
revision checks and saved history. Save partial work without checking it. The
explicit imagery checkbox records that the current visible linework was checked;
further geometry changes clear that check. The existing outline approval and
height verification are independent. The original model snapshot and estimated
3D faces are not overwritten. The aerial **Your edits** layer displays these
lines; **Prediction** retains the original model. Changing the outline makes old
line feedback stale instead of silently moving it to a different roof.

This is 2D observed linework, not a complete planar roof mesh. It can describe
missing/extra apex lines even when estimated heights are wrong. Existing roof
training targets retain their separate eligibility gates; the new observations
are not automatically promoted to complete or measured training labels.

## API and agent loader

Read saved feedback (unsaved browser drafts are excluded):

```
GET /api/scenes/roof-feedback?id=SCENE_ID&object=OBJECT_ID
```

Omit `object` to list saved roof-line corrections in that scene. The response
includes scene/object identity, review revision/author/time, image hash and URL,
coordinate transforms, current outlines, metre/pixel vertices, typed edges,
starting estimate, added/removed-or-moved counts, notes, checked/draft status,
original predictions and the separate saved 3D estimate. Stale models return 409;
stale outlines and inactive objects have `usable_as_review_constraints: false`.
The endpoint requires `crm.read` and is not cached. Saves still go through
`POST /api/scenes` with the full revisioned review.

```
scripts/ml/.venv/bin/python -m scripts.ml.scene_line_feedback \
  --scene roof_training_250-29789 \
  --object 33e97eb2-b3e1-4184-ab4f-8fbf22d0f974
```

The loader defaults to `http://localhost:3008`; `ADDR_APP_URL` or `--base-url`
selects a different app. `CRM_TOKEN` supplies a bearer token when authentication
is enabled. It prints JSON and never writes reviews. The project MCP server
also exposes `roof_line_feedback(scene_id, object_id=None)` through this loader;
a running MCP client must reconnect/restart to discover a newly added tool.
The API and CLI are immediately available while the app is running.

To apply feedback, fetch the latest saved revision, verify
`usable_as_review_constraints`, inspect the registered source image and compare
the edited graph with its starting estimate. Use the visible XY lines as
constraints for a new reconstruction and validate its planes/drainage separately.
Keep the user's line observations intact when generating or saving another 3D
proposal. Do not infer measured Z, checked outlines, full visibility or complete
negative labels from a saved line edit. Do not tune against held-out test scenes.

The focused editor now keeps the image and line properties together. **Roof
layers** hides/shows ridges, hips and valleys independently. **Find a line**
selects short branches without precision clicking and reveals their type/length.
**Compare & visibility** independently controls edited lines, the starting
estimate and the roof boundary; these are view preferences only. Reset/clear
live under **Advanced edits**. **Back to review** retains the current draft;
**Save roof line feedback** is the workspace's single save action. The full
scene history remains below it. [Design reference](design/roof-workspace-review.md).
## Keyboard history

In the roof line and 3D apex editors, Ctrl/Cmd+Z undoes a completed edit.
Ctrl/Cmd+X and Ctrl/Cmd+Shift+Z redo it. Text fields keep their native undo and
cut shortcuts. Geometry history is suspended during drawing, dragging and
saving; a new edit clears redo. History lasts for the current editor session;
saved revisions remain available after reloading or closing the editor.
