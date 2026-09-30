import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  envDir: '../',
  plugins: [react()],
  resolve: {
    alias: [
      { find: /^konva$/, replacement: path.resolve(__dirname, 'node_modules/konva/lib/index.js') },
    ],
  },
  server: {
    port: 3000,
    allowedHosts: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
  },
});
