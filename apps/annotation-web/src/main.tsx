import React from "react";
import { createRoot } from "react-dom/client";
import AnnotationWorkspace from "../../../src/components/annotations/AnnotationWorkspace";
import McpConnect from "../../../src/components/annotations/McpConnect";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<React.StrictMode>{window.location.pathname==="/mcp/connect"?<McpConnect/>:<AnnotationWorkspace/>}</React.StrictMode>);
