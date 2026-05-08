/** @type {import('next').NextConfig} */
const isProd = process.env.NODE_ENV === "production";

const config = {
  // Только клиент-сайд — без SSR. В prod собираем static export, FastAPI отдаёт его сам.
  output: isProd ? "export" : undefined,
};

export default config;
