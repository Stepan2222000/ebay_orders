// Recursively walks a DataTransferItemList (drag&drop) and returns a flat
// list of File objects, including everything inside dropped folders.
// Uses webkitGetAsEntry — supported across Chromium / WebKit / Firefox.

type FsEntry = {
  isFile: boolean;
  isDirectory: boolean;
  fullPath: string;
  file?: (cb: (f: File) => void, err?: (e: unknown) => void) => void;
  createReader?: () => {
    readEntries: (cb: (entries: FsEntry[]) => void, err?: (e: unknown) => void) => void;
  };
};

function readEntry(entry: FsEntry): Promise<File[]> {
  return new Promise((resolve) => {
    if (entry.isFile && entry.file) {
      entry.file(
        (f) => resolve([f]),
        () => resolve([]),
      );
    } else if (entry.isDirectory && entry.createReader) {
      const reader = entry.createReader();
      const all: FsEntry[] = [];
      const readBatch = () => {
        reader.readEntries(
          (entries) => {
            if (entries.length === 0) {
              Promise.all(all.map(readEntry))
                .then((arrs) => resolve(arrs.flat()))
                .catch(() => resolve([]));
            } else {
              all.push(...entries);
              readBatch();
            }
          },
          () => resolve([]),
        );
      };
      readBatch();
    } else {
      resolve([]);
    }
  });
}

export async function walkEntries(items: DataTransferItemList): Promise<File[]> {
  const out: File[] = [];
  const tasks: Promise<File[]>[] = [];
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    // Only image files; ignore text, urls, etc.
    if (item.kind !== "file") continue;
    const entry =
      typeof (item as DataTransferItem & {
        webkitGetAsEntry?: () => FsEntry | null;
      }).webkitGetAsEntry === "function"
        ? (item as DataTransferItem & {
            webkitGetAsEntry: () => FsEntry | null;
          }).webkitGetAsEntry()
        : null;
    if (entry) {
      tasks.push(readEntry(entry));
    } else {
      const f = item.getAsFile();
      if (f) out.push(f);
    }
  }
  const collected = await Promise.all(tasks);
  for (const arr of collected) out.push(...arr);
  return out.filter((f) => f.type.startsWith("image/"));
}

export function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
