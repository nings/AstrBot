import { readFileSync } from 'fs';
import { fileURLToPath, URL } from 'url';
import { defineConfig, type Plugin } from 'vite';
import vue from '@vitejs/plugin-vue';
import vuetify from 'vite-plugin-vuetify';
import webfontDl from 'vite-plugin-webfont-dl';
// @ts-ignore — .mjs not in TS project scope; Vite resolves this at runtime
import { runMdiSubset } from './scripts/subset-mdi-font.mjs';

// Second Vite entry point for the end-user (multi-user) portal SPA.
// Mirrors dashboard/vite.config.ts's plugin/alias/sass setup exactly so that
// components shared under dashboard/src/** (e.g. Vuetify wiring, shiki/markdown
// aliases) behave identically in both builds. The only intentional differences
// are: entry html (portal.html instead of index.html), output dir (dist-portal,
// per astrbot/portal/api/static_router.py's expected path), the `base` (served
// at /portal/ by the backend), and the dev server port (kept distinct from the
// operator dashboard's dev server so both can run side by side).

const t2iShikiRuntimePath = fileURLToPath(
  new URL('../astrbot/core/utils/t2i/template/shiki_runtime.iife.js', import.meta.url)
);

// Vite plugin: run MDI icon font subsetting (build only) — shared with the
// operator dashboard build since both entries load the same subsetted CSS
// asset via src/plugins/vuetify.ts.
function mdiSubset() {
  return {
    name: 'vite-plugin-mdi-subset',
    async buildStart() {
      console.log('\n🔧 Running MDI icon font subsetting (portal)...');
      await runMdiSubset();
    },
  };
}

function t2iShikiRuntimeAsset(): Plugin {
  return {
    name: 'vite-plugin-t2i-shiki-runtime',
    configureServer(server) {
      server.middlewares.use('/t2i/shiki_runtime.iife.js', (_req, res) => {
        try {
          res.statusCode = 200;
          res.setHeader('Content-Type', 'text/javascript; charset=utf-8');
          res.end(readFileSync(t2iShikiRuntimePath));
        } catch (error) {
          res.statusCode = 404;
          res.end(`T2I Shiki runtime not found: ${error instanceof Error ? error.message : String(error)}`);
        }
      });
    },
    generateBundle() {
      try {
        this.emitFile({
          type: 'asset',
          fileName: 't2i/shiki_runtime.iife.js',
          source: readFileSync(t2iShikiRuntimePath)
        });
      } catch (error) {
        this.warn(
          `Skipping T2I Shiki runtime asset because it could not be read: ${
            error instanceof Error ? error.message : String(error)
          }`
        );
      }
    }
  };
}

// https://vitejs.dev/config/
export default defineConfig(({ command }) => ({
  // Served by astrbot/portal/api/static_router.py at /portal/ — must match so
  // that built <script>/<link> asset URLs resolve to /portal/assets/... instead
  // of the site root (which is reserved for the operator dashboard).
  base: '/portal/',
  plugins: [
    // Only run MDI subsetting during production builds, skip in dev server
    ...(command === 'build' ? [mdiSubset()] : []),
    t2iShikiRuntimeAsset(),
    vue({
      template: {
        compilerOptions: {
          isCustomElement: (tag) => ['v-list-recognize-title'].includes(tag)
        }
      }
    }),
    vuetify({
      autoImport: true
    }),
    webfontDl()
  ],
  resolve: {
    alias: [
      {
        find: /^shiki$/,
        replacement: fileURLToPath(new URL('./src/utils/shikiLimitedBundle.js', import.meta.url))
      },
      {
        find: /^stream-monaco$/,
        replacement: fileURLToPath(new URL('./src/utils/streamMonacoDisabled.js', import.meta.url))
      },
      {
        find: 'mermaid',
        replacement: 'mermaid/dist/mermaid.js'
      },
      {
        find: '@',
        replacement: fileURLToPath(new URL('./src', import.meta.url))
      }
    ]
  },
  css: {
    preprocessorOptions: {
      scss: {}
    }
  },
  build: {
    // Must be named dist-portal (sibling of the operator dashboard's dist/) —
    // astrbot/portal/api/static_router.py's get_portal_dist_dir()/
    // get_bundled_portal_dist_path() hard-code this directory name.
    outDir: 'dist-portal',
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL('./portal.html', import.meta.url))
    },
    sourcemap: false,
    chunkSizeWarningLimit: 1024 * 1024 // Set the limit to 1 MB
  },
  optimizeDeps: {
    exclude: ['vuetify'],
    entries: ['./src/**/*.vue']
  },
  server: {
    host: '0.0.0.0',
    // Distinct from the operator dashboard's dev server (port 3000) so both
    // can be run concurrently during development.
    port: 3001,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:6185/',
        changeOrigin: true,
        ws: true
      }
    }
  }
}));
