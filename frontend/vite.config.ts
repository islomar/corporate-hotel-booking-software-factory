/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { copyFileSync, createReadStream, existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";

const swaggerDist = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "node_modules/swagger-ui-dist",
);
// Served by the backend at /api/docs-assets (backend/src/chb/app.py). Only these files.
const swaggerFiles: Record<string, string> = {
  "swagger-ui.css": "text/css",
  "swagger-ui-bundle.js": "text/javascript",
  "favicon-32x32.png": "image/png",
  LICENSE: "text/plain",
};

// The API docs page must load Swagger UI from the app itself: the CSP allows no CDN (CHB-22).
// The build copies the files next to the frontend, where the backend serves them.
// `make dev` serves them from node_modules on this server (http://localhost:5173/api/docs).
function swaggerUiAssets(): Plugin {
  return {
    name: "swagger-ui-assets",
    configureServer(server) {
      server.middlewares.use("/api/docs-assets", (request, response, next) => {
        const name = (request.url ?? "").split("?")[0].replace(/^\//, "");
        if (!Object.hasOwn(swaggerFiles, name)) return next();
        response.setHeader("content-type", swaggerFiles[name]);
        createReadStream(join(swaggerDist, name)).pipe(response);
      });
    },
    writeBundle(options) {
      const target = join(options.dir ?? "dist", "swagger-ui");
      if (!existsSync(swaggerDist))
        throw new Error("swagger-ui-dist is not installed");
      mkdirSync(target, { recursive: true });
      for (const name of Object.keys(swaggerFiles)) {
        copyFileSync(join(swaggerDist, name), join(target, name));
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), swaggerUiAssets()],
  server: {
    host: true,
    port: 5173,
    proxy: { "/api": backendUrl },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
  },
});
