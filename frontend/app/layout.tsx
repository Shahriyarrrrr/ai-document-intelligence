import type { Metadata } from "next";
import "./globals.css";
import Nav from "@/components/Nav";

export const metadata: Metadata = {
  title: "Document Intelligence",
  description: "Automated invoice extraction, validation and search",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="top">
            <h1>Document Intelligence</h1>
            <Nav />
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
