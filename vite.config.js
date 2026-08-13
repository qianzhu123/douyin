import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // 避免浏览器扩展目录里的 popup.html 被当作项目页面热重载
  server: {
    host: "127.0.0.1",
    port: 5175,
    strictPort: true,
    watch: {
      ignored: ["**/script/**", "**/external/**", "**/node_modules/**"],
    },
  },
});
