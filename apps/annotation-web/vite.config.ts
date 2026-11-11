import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  publicDir: "public",
  resolve: { alias: { "@": fileURLToPath(new URL("../../src", import.meta.url)) } },
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    host: "127.0.0.1",
    port: 4173,
    proxy: {
      "/annotation.v1.AnnotationService": "http://127.0.0.1:8095",
      "/api/v1": "http://127.0.0.1:8095",
    },
  },
});
