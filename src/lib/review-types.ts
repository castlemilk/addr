export const REVIEW_STATUSES = ["unreviewed", "needs_adjustment", "approved", "rejected"] as const;
export const REVIEW_ISSUES = ["boundary", "apex", "roof_planes", "vegetation", "wrong_building"] as const;
export type ReviewStatus = typeof REVIEW_STATUSES[number];
import type { RoofAnnotation, RoofObject } from "./roof-annotations";
export interface ReviewFeedback {
  status: ReviewStatus;
  notes: string;
  rejected_cuts: [number, number][][];
  issues: string[];
  boundary_verified: boolean;
  boundary_signature?: string;
  updated_at?: string;
  updated_by?: string;
  revision: number;
  model_signature?: string;
  annotation?: RoofAnnotation | null;
  objects?: RoofObject[];
  apex_verified?: boolean;
  roof_form_verified?: boolean;
  objects_verified?: boolean;
  object_reviewed_classes?: RoofObject["kind"][];
  annotation_audit?: {passed:boolean;issues:string[]};
  history?: Omit<ReviewFeedback, "history">[];
}
