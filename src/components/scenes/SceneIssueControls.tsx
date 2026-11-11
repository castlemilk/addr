import { SCENE_REVIEW_ISSUES, type SceneObject } from "@/lib/scene-types";
import { hasOutlineIssue, REVIEW_ISSUE_LABELS, setReviewIssue } from "@/lib/scene-review-issues";

export default function SceneIssueControls({object,onChange}:{object:SceneObject;onChange:(object:SceneObject)=>void}) {
  return <details className="scene-issue-controls" open>
    <summary>Needs attention{object.review_issues?.length ? ` · ${object.review_issues.length}` : ""}</summary>
    <p className="review-muted">Flag what needs correcting. Clear each flag after checking the correction.</p>
    <div className="scene-issue-options">
      {SCENE_REVIEW_ISSUES.map(issue=><label className="review-checkbox" key={issue}>
        <input type="checkbox" aria-label={`Needs attention: ${REVIEW_ISSUE_LABELS[issue]}`} checked={object.review_issues?.includes(issue)||false}
          onChange={event=>onChange(setReviewIssue(object,issue,event.target.checked))}/>
        {REVIEW_ISSUE_LABELS[issue]}
      </label>)}
    </div>
    {hasOutlineIssue(object)?<p className="review-notice">Resolve outline, object type and separation flags before confirming this outline.</p>:
      <p className="review-muted">3D flags preserve outline approval. Clearing a flag does not verify a height or approve an outline.</p>}
  </details>;
}
