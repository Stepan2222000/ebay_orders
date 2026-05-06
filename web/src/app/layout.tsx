import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Order details inbox.",
  description: "Local-only inbox for eBay Order details screenshots.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
