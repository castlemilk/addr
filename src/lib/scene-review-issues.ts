import { SCENE_REVIEW_ISSUES, type SceneObject, type SceneReviewIssue } from "./scene-types";

export const REVIEW_ISSUE_LABELS: Record<SceneReviewIssue, string> = {
  outline: "Outline", class: "Object type", instances: "Separate or merged objects",
  shape_3d: "3D shape / roof apexes", height: "Height", placement: "3D placement",
};
export const OUTLINE_REVIEW_ISSUES: readonly SceneReviewIssue[] = ["outline", "class", "instances"];
export const hasOutlineIssue = (object: Pick<SceneObject, "review_issues">) =>
  object.review_issues?.some(issue => OUTLINE_REVIEW_ISSUES.includes(issue)) || false;

export function setReviewIssue(object: SceneObject, issue: SceneReviewIssue, checked: boolean): SceneObject {
  if (object.status === "superseded") return object;
  const issues = new Set(object.review_issues || []);
  if (checked) issues.add(issue); else issues.delete(issue);
  const review_issues = SCENE_REVIEW_ISSUES.filter(value => issues.has(value));
  const reopen = checked && OUTLINE_REVIEW_ISSUES.includes(issue) && object.status !== "rejected";
  return { ...object, review_issues: review_issues.length ? review_issues : undefined,
    ...(reopen ? { status: "needs_adjustment", geometry_reviewed: false } : {}),
    ...(checked && issue === "height" ? { model: { ...object.model, height_verified: false } } : {}),
  };
}
