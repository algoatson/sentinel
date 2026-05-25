import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    // SPA mode — single index.html, client-side routing, served at /app/*
    // by FastAPI's StaticFiles + SPA fallback in src/sentinel/dashboard/v2_serve.py
    adapter: adapter({
      fallback: 'index.html',
      strict: false
    }),
    // Frontend is mounted under /app/ by the backend; tell SvelteKit so
    // all asset URLs are prefixed correctly.
    paths: {
      base: '/app',
      relative: false
    },
    alias: {
      $components: 'src/lib/components',
      $api: 'src/lib/api.ts'
    }
  }
};

export default config;
