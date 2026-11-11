import { REVIEW_STATUS_LABELS, type SceneProgress } from "@/lib/scene-queue";

export default function SceneReviewProgress({progress,dirty}:{progress:SceneProgress;dirty:boolean}) {
  return <div className="scene-review-progress">
    <div className="scene-progress-caption">This image{dirty&&<span>Unsaved counts</span>}</div>
    <dl aria-label="Image review progress">
      {(["candidate","needs_adjustment","confirmed","rejected"] as const).map(status=><div key={status} data-status={status}>
        <dt>{REVIEW_STATUS_LABELS[status]}</dt><dd>{progress[status]}</dd>
      </div>)}
    </dl>
    {progress.superseded>0&&<p className="review-muted">{progress.superseded} replaced outlines retained in history.</p>}
  </div>;
}
