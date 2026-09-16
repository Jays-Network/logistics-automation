import type { Metadata } from "next";
import { IBM_Plex_Sans, IBM_Plex_Mono } from "next/font/google";
import Sidebar from "@/components/Sidebar";
import "./globals.css";

const plexSans = IBM_Plex_Sans({
  variable: "--font-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

export const metadata: Metadata = {
  title: "OmniOrchestrator",
  description: "Live job monitoring and control for Jay's Network",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${plexSans.variable} ${plexMono.variable} h-full`}>
      <body className="min-h-full bg-void text-ink antialiased">
        <div className="flex min-h-full">
          <Sidebar />
          <div className="flex flex-1 flex-col">{children}</div>
        </div>
      </body>
    </html>
  );
}