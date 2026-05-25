import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    // During `pnpm dev`, proxy /api/* to the running bot on 8730 so the
    // Svelte app talks to the live backend. In production both the SPA
    // and the API are served by the same FastAPI app under the same
    // origin, so this proxy is only needed for local dev.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8730',
        changeOrigin: true
      }
    }
  }
});
