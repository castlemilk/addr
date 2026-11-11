import React from "react";
import { createRoot } from "react-dom/client";
import AnnotationWorkspace from "../../../src/components/annotations/AnnotationWorkspace";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<React.StrictMode><AnnotationWorkspace /></React.StrictMode>);
