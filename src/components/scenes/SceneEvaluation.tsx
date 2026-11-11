import Link from "next/link";
import {CLASS_NAMES, type SceneKind} from "@/lib/scene-types";
import {SPLIT_NAMES, TRAINING_SPLITS, type SceneEvaluation, type TrainingLayer} from "@/lib/scene-training";

const percent=(value:number|null)=>value===null?"—":`${(value*100).toFixed(1)}%`;
export default function SceneEvaluationPanel({evaluation,kind,split}:{evaluation?:SceneEvaluation;kind:SceneKind;split:TrainingLayer["split"]|"all"}) {
  const category=evaluation?.classes.find(c=>c.kind===kind);
  if(!category)return null;
  const splits=TRAINING_SPLITS.filter(s=>split==="all"||split===s);
  const layers=category.layers.filter(l=>split==="all"||l.split===split)
    .sort((a,b)=>(b.extra+b.missed)-(a.extra+a.missed)||(a.outline_iou??1)-(b.outline_iou??1)||a.title.localeCompare(b.title,"en-AU"));
  return <section className="training-evaluation" aria-labelledby="scene-evaluation-heading">
    <h3 id="scene-evaluation-heading">Model check · {CLASS_NAMES[kind]}</h3>
    <p className="training-explainer">Original saved detections compared with fully checked image layers. This uses the selected class and split; Layer status filters only the review list.</p>
    {!layers.length?<p className="review-notice">No fully checked, scanned image layers qualify in this selection. Finish image review and resolve any split warnings to establish a baseline.</p>:<>
      <div className="training-table-scroll" tabIndex={0} role="region" aria-label="Model check by dataset split">
        <table className="evaluation-table"><caption className="training-sr-only">Fixed-threshold instance matches and mean outline overlap against saved references. A dash means there is no denominator.</caption>
          <thead><tr><th scope="col">Split · images / areas</th><th scope="col">Matched / references</th><th scope="col">Extra</th><th scope="col">Missed</th><th scope="col">Precision</th><th scope="col">Recall</th><th scope="col">Outline overlap</th></tr></thead>
          <tbody>{splits.map(s=>{const m=category.splits[s];return <tr key={s} data-evaluation-split={s}>
            <th scope="row">{SPLIT_NAMES[s]}<small>{m.images} {m.images===1?"image":"images"} / {m.groups} {m.groups===1?"area":"areas"}</small></th>
            <td>{m.matched} / {m.references}</td><td>{m.extra}</td><td>{m.missed}</td><td>{percent(m.precision)}</td><td>{percent(m.recall)}</td><td>{percent(m.mean_outline_iou)}</td>
          </tr>;})}</tbody>
        </table>
      </div>
      <p className="evaluation-scroll-hint">Scroll the table sideways for extras, misses and overlap scores.</p>
      <details className="evaluation-method"><summary>How to interpret these checks</summary>
        <p>A match needs at least {evaluation!.iou_threshold*100}% polygon intersection-over-union (IoU), including interior gaps. Each detection can match one reference. Extra detections include duplicates and invalid outlines; missed references have no qualifying match.</p>
        <p>Precision is matched / detections; recall is matched / references. Outline overlap averages the union IoU within each image, counting each image equally. An empty prediction and empty reference have no overlap score. These are fixed-threshold diagnostics, not COCO average precision.</p>
        <p>Saved snapshots may use different pipeline versions. Reviewed imagery is the reference, not independently measured truth. Area counts expose repeated views; small or empty splits cannot establish robust accuracy. Training results are descriptive, validation guides development, and test results should be reserved for final comparisons. Heights and apexes are not evaluated.</p>
      </details>
      <details className="evaluation-images"><summary>Inspect {layers.length} evaluated {layers.length===1?"image":"images"}</summary>
        <ul>{layers.map(l=><li key={l.scene_id} data-evaluation-scene={l.scene_id}>
          <div><strong>{l.title}</strong><p>{SPLIT_NAMES[l.split]} · {l.matched}/{l.references} matched · {l.extra} extra · {l.missed} missed · {percent(l.outline_iou)} overlap</p>
            <small>Review {l.revision} · model {l.model_signature.slice(0,10)}{l.invalid_predictions?` · ${l.invalid_predictions} invalid outlines`:""}</small></div>
          <Link className="review-button" href={l.review_url} aria-label={`Inspect model check for ${CLASS_NAMES[kind]} in ${l.title}`}>Inspect layer</Link>
        </li>)}</ul>
      </details>
    </>}
  </section>;
}
