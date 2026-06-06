// 唤星 embedded_desktop 同源嵌入（设计 12 §6.3 / 任务 #1245）：daemon 把
// `/api/v1/apps/presentation/ui/*` 反代到本 sidecar（透传完整前缀，不剥成 root）。设
// basePath/assetPrefix 让 Next 的页面、`/_next/*` 资源、客户端路由全部带该前缀，与反代一致；
// 否则资源发出裸 `/_next/*` 会命中 daemon 根 404。env 未设（Docker/Electron）即为空 → 零行为变更。
const embedUiBase = (process.env.NEXT_PUBLIC_HX_EMBED_UI_BASE || "").trim().replace(/\/+$/, "");

const nextConfig = {
  ...(embedUiBase ? { basePath: embedUiBase, assetPrefix: embedUiBase } : {}),
  reactStrictMode: false,
  distDir: ".next-build",
  output: "standalone",
  ...(process.env.NODE_ENV !== "production"
    ? {
        allowedDevOrigins: [
          "http://127.0.0.1:40001",
          "http://localhost:40001",
          "127.0.0.1",
          "localhost",
        ],
      }
    : {}),

  // Rewrites for development - proxy font requests to FastAPI backend
  async rewrites() {
    return [
      {
        source: '/app_data/fonts/:path*',
        destination: 'http://localhost:5000/app_data/fonts/:path*',
      },
    ];
  },

  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "pub-7c765f3726084c52bcd5d180d51f1255.r2.dev",
      },
      {
        protocol: "https",
        hostname: "pptgen-public.ap-south-1.amazonaws.com",
      },
      {
        protocol: "https",
        hostname: "pptgen-public.s3.ap-south-1.amazonaws.com",
      },
      {
        protocol: "https",
        hostname: "img.icons8.com",
      },
      {
        protocol: "https",
        hostname: "present-for-me.s3.amazonaws.com",
      },
      {
        protocol: "https",
        hostname: "yefhrkuqbjcblofdcpnr.supabase.co",
      },
      {
        protocol: "https",
        hostname: "images.unsplash.com",
      },
      {
        protocol: "https",
        hostname: "picsum.photos",
      },
      {
        protocol: "https",
        hostname: "unsplash.com",
      },
    ],
  },
  
};

export default nextConfig;
