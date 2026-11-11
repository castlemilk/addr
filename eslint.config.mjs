import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      "react-hooks/set-state-in-effect": "warn",
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    ".run/**",
    "data/**",
    "**/.venv/**",
    "**/.venv*/**", // suffixed venvs too (scripts/ml/training/.venv-sam3 ships vendored JavaScript)
    "scripts/ml/.venv/**",
    "out/**",
    "build/**",
    "apps/annotation-web/dist/**",
    "deploy/annotation/.wrangler/**",
    "deploy/annotation/node_modules/**",
    "next-env.d.ts",
    // Generated MapLibre worker assets copied by scripts/copy-maplibre-worker.mjs
    "public/maplibre/**",
  ]),
]);

export default eslintConfig;
