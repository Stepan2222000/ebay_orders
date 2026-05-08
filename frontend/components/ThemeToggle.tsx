"use client";
import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import styles from "./ThemeToggle.module.css";

type Theme = "light" | "dark";

function readTheme(): Theme {
  if (typeof document === "undefined") return "dark";
  return (document.documentElement.dataset.theme as Theme) || "dark";
}

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    setTheme(readTheme());
  }, []);

  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("eo-theme", next);
    } catch {
      // приватный режим / quota — просто игнорируем
    }
    setTheme(next);
  };

  return (
    <button
      type="button"
      onClick={toggle}
      className={styles.btn}
      aria-label={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
      data-testid="theme-toggle"
      data-theme={theme}
    >
      {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  );
}
