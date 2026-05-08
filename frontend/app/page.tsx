"use client";
import Sidebar from "@/components/Sidebar";
import ChatPane from "@/components/ChatPane";
import ConnectionBanner from "@/components/ConnectionBanner";
import styles from "./page.module.css";

export default function HomePage() {
  return (
    <main className={styles.app}>
      <Sidebar />
      <ChatPane />
      <ConnectionBanner />
    </main>
  );
}
