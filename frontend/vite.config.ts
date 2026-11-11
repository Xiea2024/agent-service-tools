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
    plugins: [react(), ...(mode === 'demo' ? [{
      name: 'static-demo-metadata',
      transformIndexHtml: (html: string) => html
        .replace(/<title>.*?<\/title>/, '<title>SupportPilot · 静态交互演示</title>')
        .replace(/(<meta name="description" content=")[^"]*(")/, '$1SupportPilot 研发排查工作台：预录问答、模拟流式输出与知识库引用。演示账号 guest / guest。$2'),
    }] : [])],
    server: { port: 5173, strictPort: true, proxy: mode === 'demo' ? undefined : proxy },
    preview: { port: 4173, strictPort: true, proxy: mode === 'demo' ? undefined : proxy },
  };
});
