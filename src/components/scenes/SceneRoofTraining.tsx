import Link from "next/link";
import { SPLIT_NAMES, TRAINING_SPLITS, type SceneTrainingCoverage } from "@/lib/scene-training";
const names = { family: "Roof family", linework: "Rooflines in imagery" };
const reasons: Record<string, string> = { unchecked: "not checked", outline_unreviewed: "confirm the outline", visibility: "full visibility needs checking",
  changed_outline: "outline changed", changed_linework: "rooflines changed", shape_flag: "resolve the shape feedback flag",
  linework_coverage: "roof faces need consistent coverage", invalid_linework: "rooflines need correction", stale: "model changed",
  image_geography: "image geography withheld", unassigned: "split unresolved", missing_group: "area group missing", split_conflict: "area appears in multiple splits" };
export default function SceneRoofTraining({ feedback }: { feedback: SceneTrainingCoverage["roof_feedback"] }) {
  if (!feedback) return null;
  return <section className="training-roof-feedback" aria-label="Roof training feedback coverage">
    <h3>Roof family and roofline labels</h3>
    <p className="training-explainer">In Object details, record the observed roof family and check rooflines against imagery. These targets use fully visible, confirmed buildings. Each checked building can contribute while other buildings in its image still need review. Heights are excluded.</p>
    <div className="training-table-scroll" tabIndex={0} role="region" aria-label="Roof feedback by split">
      <table className="training-table"><caption className="training-sr-only">Exportable roof observations by split, independent of whole-image mask coverage.</caption>
        <thead><tr><th scope="col">Target</th>{TRAINING_SPLITS.map(s => <th scope="col" key={s}>{SPLIT_NAMES[s]}</th>)}<th scope="col">Checked but withheld</th></tr></thead>
        <tbody>{(["family", "linework"] as const).map(target => <tr key={target}><th scope="row">{names[target]}</th>
          {TRAINING_SPLITS.map(s => <td key={s}><strong>{feedback.targets[target].splits[s].objects}</strong><small>{feedback.targets[target].splits[s].groups} area groups</small></td>)}
          <td>{feedback.targets[target].withheld}</td></tr>)}</tbody>
      </table>
    </div>
    {feedback.layers.length ? <details><summary>Review saved roof checks ({feedback.layers.length})</summary>
      <ul>{feedback.layers.map(layer => <li key={`${layer.scene_id}:${layer.object_id}`}>
        <Link href={layer.review_url}>{layer.title}</Link> · {SPLIT_NAMES[layer.split]}
        {(["family", "linework"] as const).filter(target => layer.checked[target]).map(target => <p key={target}>
          {names[target]}: {layer.eligible[target] ? "ready for export" : layer.blockers[target].map(reason => reasons[reason] || reason).join("; ")}
        </p>)}
      </li>)}</ul>
    </details> : <p className="training-explainer">No saved roof checks yet. Open a building below to begin. Creating a roof proposal or confirming its outline does not check its rooflines.</p>}
  </section>;
}
