// Рекурсивный обход drop-входов (DataTransferItemList) через webkitGetAsEntry.
// Спека: https://wicg.github.io/entries-api/

const ALLOWED = /^image\/(png|jpeg|webp|gif)$/;

function entryFile(entry: any): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function readAll(reader: any): Promise<any[]> {
  const out: any[] = [];
  while (true) {
    const batch: any[] = await new Promise((resolve, reject) =>
      reader.readEntries(resolve, reject),
    );
    if (!batch.length) return out;
    out.push(...batch);
  }
}

async function walk(entry: any, files: File[]): Promise<void> {
  if (entry.isFile) {
    try {
      const f = await entryFile(entry);
      if (ALLOWED.test(f.type) || ALLOWED.test(guessTypeByName(f.name))) {
        files.push(f);
      }
    } catch {
      // молча пропускаем недоступный файл
    }
    return;
  }
  if (entry.isDirectory) {
    const reader = entry.createReader();
    const children = await readAll(reader);
    await Promise.all(children.map((c) => walk(c, files)));
  }
}

function guessTypeByName(name: string): string {
  const ext = name.toLowerCase().split(".").pop() || "";
  if (ext === "png") return "image/png";
  if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
  if (ext === "webp") return "image/webp";
  if (ext === "gif") return "image/gif";
  return "";
}

/** Из items одного drop'а собираем список image-файлов (рекурсивно из папок). */
export async function collectImageFiles(items: DataTransferItemList): Promise<File[]> {
  const out: File[] = [];
  const promises: Promise<void>[] = [];
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.kind !== "file") continue;
    // webkitGetAsEntry — стандарт de-facto в Chrome/FF/Safari/Edge.
    const entry = (item as any).webkitGetAsEntry?.();
    if (entry) {
      promises.push(walk(entry, out));
    } else {
      const f = item.getAsFile();
      if (f && (ALLOWED.test(f.type) || ALLOWED.test(guessTypeByName(f.name)))) {
        out.push(f);
      }
    }
  }
  await Promise.all(promises);
  return out;
}

/** Файлы из обычного <input type="file" multiple webkitdirectory>. */
export function collectInputFiles(input: HTMLInputElement): File[] {
  const out: File[] = [];
  const list = input.files;
  if (!list) return out;
  for (let i = 0; i < list.length; i++) {
    const f = list[i];
    if (ALLOWED.test(f.type) || ALLOWED.test(guessTypeByName(f.name))) out.push(f);
  }
  return out;
}
