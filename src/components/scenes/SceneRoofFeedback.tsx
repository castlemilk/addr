"use client";
import { currentRoofLineEdits } from "@/lib/roof-line-edits";
import { currentRoofFeedback, roofLineworkIssue, withRoofFeedback } from "@/lib/scene-roof-feedback";
import type { RoofFamily, SceneObject } from "@/lib/scene-types";

export default function SceneRoofFeedback({ object, frame, onChange }: {
  object: SceneObject; frame: number[][]; onChange: (object: SceneObject) => void;
}) {
  const lineEdits=currentRoofLineEdits(object);
  const checked = currentRoofFeedback(object), issue = roofLineworkIssue(object, frame);
  const stale = object.roof_feedback && ((!checked.family && object.roof_feedback.family) || (!lineEdits && !checked.linework && object.roof_feedback.linework));
  return <details className="scene-roof-feedback" open>
    <summary>Roof training feedback</summary>
    <label>Observed roof family<select aria-label="Observed roof family" value={checked.family || ""}
      onChange={e => onChange(withRoofFeedback(object, { family: e.target.value as RoofFamily || null }))}>
      <option value="">Not checked / unclear</option><option value="gabled">Gabled — vertical triangular ends</option>
      <option value="hipped">Hipped — sloping ends</option><option value="flat">Flat</option><option value="mixed">Mixed roof forms</option>
    </select></label>
    {lineEdits ? <p className="review-muted">Your {lineEdits.edges.length} edited roof lines are {lineEdits.checked ? "checked against imagery" : "draft observations"}. Use Edit roof lines to review or change them, then save the scene. They remain separate from the estimated 3D faces and training labels.</p> : <label className="scene-placement-toggle"><input type="checkbox" aria-label="Rooflines checked against imagery"
      checked={!!checked.linework} disabled={!!issue} onChange={e => onChange(withRoofFeedback(object, { linework: e.target.checked }))}/>
      I checked the rooflines against imagery
    </label>}
    <p className="review-muted">Check the family and visible ridge, hip and valley positions separately. Choosing a family records feedback; use the roof editor to change its shape. Heights remain estimates.</p>
    {issue && <p className="review-muted">{issue}</p>}
    {stale && <p className="review-notice">Roof geometry changed. Recheck the affected feedback before exporting it.</p>}
    {(checked.family || checked.linework) && <p className="review-muted">Save the scene to retain this feedback. Training also needs a confirmed outline, full visibility and a valid geographic split.</p>}
  </details>;
}
