import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// 仅 Node 端读取凭据；禁止使用 VITE_ 前缀将共享密钥打包到浏览器。
export default defineConfig(({ mode }) => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const env = { ...loadEnv(mode, root, ''), ...process.env };
  const proxy = {
    '/api': {
      target: env.WEB_API_TARGET || `http://127.0.0.1:${env.PORT || '8080'}`,
      changeOrigin: true,
      rewrite: (path: string) => path.replace(/^\/api(?=\/|$)/, ''),
      headers: env.TENANT_MODE !== 'true' && env.AUTH_SECRET ? { Authorization: `Bearer ${env.AUTH_SECRET}` } : {},
      timeout: 120_000,
      proxyTimeout: 120_000,
    },
  };
  return {
    plugins: [react()],
    server: { port: 5173, strictPort: true, proxy },
    preview: { port: 4173, strictPort: true, proxy },
  };
});
