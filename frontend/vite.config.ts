import { defineConfig } from 'vite';
import { existsSync, readFileSync } from 'node:fs';

export default defineConfig(() => {
  const path = new URL('../data/portable/config.json', import.meta.url);
  const port = existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')).BackendPort : 8010;
  return {
    server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy: { '/api': `http://127.0.0.1:${port}` } },
  };
});
