import { defineConfig } from 'astro/config';

export default defineConfig({
  output: 'static',
  site: 'https://altner.github.io',
  base: '/octobeat',
  trailingSlash: 'always',
  build: {
    format: 'directory',
  },
});
