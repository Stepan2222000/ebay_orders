"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Loader2, Plus, Trash2, X } from "lucide-react";
import {
  deleteListingPhoto,
  fetchListingPhotos,
  listingPhotoUrl,
  uploadListingPhotos,
} from "@/lib/api";
import type { ListingPhoto } from "@/lib/types";
import styles from "./ItemGallery.module.css";

/** Галерея фото товара по item_number: eBay-фото (с листинга) + ручные снимки.
 * Одна и та же галерея во всех плашках с этим номером. Картинки идут через
 * https-прокси бэкенда (mixed content). «+» — загрузить своё фото. */
export default function ItemGallery({ itemNumber }: { itemNumber: string }) {
  const [photos, setPhotos] = useState<ListingPhoto[] | null>(null);
  const [active, setActive] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(
    () => fetchListingPhotos(itemNumber).then(setPhotos).catch(() => setPhotos([])),
    [itemNumber],
  );

  useEffect(() => {
    load();
  }, [load]);

  const onPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (fileRef.current) fileRef.current.value = "";
    if (!files.length) return;
    setBusy(true);
    setError(null);
    try {
      const res = await uploadListingPhotos(itemNumber, files);
      await load();                                   // ждём refetch — превью появляется сразу
      if (res.length && res.every((p) => p.duplicate)) {
        setError(files.length > 1 ? "эти фото уже добавлены" : "это фото уже добавлено");
      }
    } catch (err) {
      setError((err as Error).message || "не удалось загрузить");
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (photo: ListingPhoto) => {
    await deleteListingPhoto(itemNumber, photo.id);
    setActive(null);
    load();
  };

  useEffect(() => {
    if (active === null || !photos) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setActive(null);
      else if (e.key === "ArrowLeft") setActive((i) => (i !== null && i > 0 ? i - 1 : i));
      else if (e.key === "ArrowRight")
        setActive((i) => (i !== null && i < photos.length - 1 ? i + 1 : i));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [active, photos]);

  if (photos === null) {
    return <div className={styles.row}><span className={styles.loading}>фото…</span></div>;
  }

  const cur = active !== null ? photos[active] : null;

  return (
    <div className={styles.wrap}>
      <div className={styles.row}>
        {photos.map((p, i) => (
          <button
            key={p.id}
            type="button"
            className={styles.thumb}
            onClick={() => setActive(i)}
            title={p.source === "manual" ? "моё фото" : "фото eBay"}
          >
            <img src={listingPhotoUrl(p)} alt="" loading="lazy" draggable={false} />
            {p.source === "manual" ? <span className={styles.badge}>моё</span> : null}
          </button>
        ))}
        <button
          type="button"
          className={styles.add}
          onClick={() => fileRef.current?.click()}
          disabled={busy}
          title="добавить своё фото"
        >
          {busy ? <Loader2 size={16} className={styles.spin} /> : <Plus size={16} />}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/*,.heic,.heif"
          multiple
          hidden
          onChange={onPick}
        />
      </div>

      {error ? <div className={styles.error}>{error}</div> : null}

      {cur ? (
        <div className={styles.lightbox} onClick={() => setActive(null)}>
          <button className={styles.lbClose} onClick={() => setActive(null)} aria-label="Закрыть">
            <X size={20} />
          </button>
          {active !== null && active > 0 ? (
            <button
              className={`${styles.nav} ${styles.prev}`}
              onClick={(e) => {
                e.stopPropagation();
                setActive(active - 1);
              }}
              aria-label="Назад"
            >
              <ChevronLeft size={28} />
            </button>
          ) : null}
          <img
            className={styles.lbImg}
            src={listingPhotoUrl(cur)}
            alt=""
            onClick={(e) => e.stopPropagation()}
            draggable={false}
          />
          {active !== null && active < photos.length - 1 ? (
            <button
              className={`${styles.nav} ${styles.next}`}
              onClick={(e) => {
                e.stopPropagation();
                setActive(active + 1);
              }}
              aria-label="Вперёд"
            >
              <ChevronRight size={28} />
            </button>
          ) : null}
          <div className={styles.lbBar} onClick={(e) => e.stopPropagation()}>
            <span className={styles.counter}>
              {(active ?? 0) + 1} / {photos.length}
              {cur.source === "manual" ? " · моё" : ""}
            </span>
            {cur.source === "manual" ? (
              <button className={styles.lbDelete} onClick={() => onDelete(cur)}>
                <Trash2 size={14} /> удалить
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
