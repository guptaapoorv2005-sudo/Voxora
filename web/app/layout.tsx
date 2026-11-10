import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Voxora — AI Voice Receptionist",
  description: "AI-Powered Voice Receptionist for BrightSmile Dental Clinic",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="h-full antialiased font-sans">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
