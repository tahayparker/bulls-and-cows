import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cows & Bulls Solver",
  description: "A local four-letter Bulls and Cows solver with an API-built word list.",
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
