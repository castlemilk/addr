import type { SceneObject } from "@/lib/scene-types";
import { carriesVegetationType, VEGETATION_TYPE_LABELS, VEGETATION_TYPES, type VegetationType } from "@/lib/scene-vegetation";

/** Inspector quick buttons for the vegetation type of a tree or vegetation outline (grass, hedge, shrub, tree) and Not set, which
 *  clears it. A press reaches the page's change(), so Undo restores the prior draft and the reviewer still saves. It never changes
 *  the status, the outline or the label. Renders nothing for other kinds; disabled on rejected and replaced outlines. No hooks, so a
 *  test can call it as a function. */
export default function SceneVegetationType({ object, disabled = false, onChange }: {
  object: Pick<SceneObject, "kind" | "status" | "vegetation_type">;
  disabled?: boolean;
  onChange: (type: VegetationType | null) => void;
}) {
  if (!carriesVegetationType(object.kind)) return null;
  const current = object.vegetation_type ?? null;
  const off = disabled || object.status === "superseded" || object.status === "rejected";
  return (
    <div className="scene-vegetation-type" role="group" aria-label="Vegetation type" data-testid="scene-vegetation-type" data-vegetation-type={current ?? ""}>
      <span>Vegetation type</span>
      <div className="scene-quick-kinds" style={{ justifyContent: "flex-start", padding: 0 }}>
        {VEGETATION_TYPES.map((type) => (
          <button key={type} type="button" className={`scene-quick-kind${current === type ? " is-current" : ""}`} aria-pressed={current === type}
            disabled={off} data-vegetation-type={type}
            title={current === type ? `${VEGETATION_TYPE_LABELS[type]} is set.` : `Set ${VEGETATION_TYPE_LABELS[type].toLowerCase()}. The status, outline and label stay as they are; Undo edit takes it back.`}
            onClick={() => onChange(type)}>
            {VEGETATION_TYPE_LABELS[type]}
          </button>
        ))}
        <button type="button" className={`scene-quick-kind${current === null ? " is-current" : ""}`} aria-pressed={current === null}
          disabled={off || current === null} data-vegetation-type="" title="Clear the vegetation type. The status, outline and label stay as they are."
          onClick={() => onChange(null)}>
          Not set
        </button>
      </div>
    </div>
  );
}
