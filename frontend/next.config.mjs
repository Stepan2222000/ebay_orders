/** @type {import('next').NextConfig} */
const config = {
  // Production-сборка через standalone — даёт минимальный self-contained Node-bundle
  // (frontend/.next/standalone + статика), который запускается командой `node server.js`
  // в production-контейнере. Dev по-прежнему обычный `next dev`.
  output: process.env.NODE_ENV === "production" ? "standalone" : undefined,
};

export default config;
