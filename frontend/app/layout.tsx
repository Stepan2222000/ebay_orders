import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "eBay orders",
  description: "Локальная сборка заказов eBay из скриншотов.",
};

// Inline-скрипт до гидрации — без flash светлой/тёмной темы при загрузке.
const themeBootScript = `
(function() {
  try {
    var t = localStorage.getItem('eo-theme');
    if (t !== 'light' && t !== 'dark') t = 'dark';
    document.documentElement.dataset.theme = t;
  } catch (e) {
    document.documentElement.dataset.theme = 'dark';
  }
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" data-theme="dark">
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeBootScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
