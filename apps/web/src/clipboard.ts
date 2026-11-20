/**
 * Clipboard helpers for the capture form and item actions. Browsers differ a lot here
 * (permissions, user activation, which types they expose), so every helper reports failure
 * instead of guessing, and callers fall back to the normal paste or download paths.
 */

/** Image types the clipboard API exposes and the server previews (ADR-0023). */
export const CLIPBOARD_IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"] as const;

const EXTENSION: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
};

const pad = (value: number) => String(value).padStart(2, "0");

/** "Zwischenablage 2026-11-21 14-05-09.png": pasted images arrive as "image.png" or nameless. */
export function pastedName(type: string, now: Date = new Date()): string {
  const stamp =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
    `${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}`;
  const extension = EXTENSION[type];
  return extension ? `Zwischenablage ${stamp}.${extension}` : `Zwischenablage ${stamp}`;
}

/** A pasted file keeps a real name (a copied file) and gets a dated one when it has none. */
export function pastedFile(file: File, now: Date = new Date()): File {
  const generic = file.name === "" || /^image\.(png|jpe?g|gif|webp)$/i.test(file.name);
  if (!generic) return file;
  return new File([file], pastedName(file.type, now), { type: file.type, lastModified: file.lastModified });
}

/** Files of a paste event, renamed where the browser gave them a placeholder name. */
export function clipboardFiles(data: DataTransfer | null, now: Date = new Date()): File[] {
  return Array.from(data?.files ?? [], (file) => pastedFile(file, now));
}

/** Whether a paste or key event happened where the browser edits text itself. */
export function inEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

export type ClipboardContent = { files: File[] } | { text: string } | null;

/** Whether the page can read the clipboard on a button press (Chromium, Safari, Firefox 125+). */
export function canReadClipboard(): boolean {
  const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
  return typeof clipboard?.read === "function" || typeof clipboard?.readText === "function";
}

/**
 * Reads images (preferred) or text from the clipboard; null when it is empty. Throws when the
 * browser refuses, e.g. a denied permission, so the caller can explain the keyboard paste.
 */
export async function readClipboard(now: Date = new Date()): Promise<ClipboardContent> {
  const clipboard = navigator.clipboard;
  if (typeof clipboard.read === "function") {
    const items = await clipboard.read();
    const files: File[] = [];
    let text: string | null = null;
    for (const item of items) {
      const type = CLIPBOARD_IMAGE_TYPES.find((candidate) => item.types.includes(candidate));
      if (type) {
        const blob = await item.getType(type);
        files.push(new File([blob], pastedName(type, now), { type }));
      } else if (text === null && item.types.includes("text/plain")) {
        text = await (await item.getType("text/plain")).text();
      }
    }
    if (files.length > 0) return { files };
    return text ? { text } : null;
  }
  const text = await clipboard.readText();
  return text ? { text } : null;
}

/**
 * Copies text. A loader runs inside the click's user activation where the browser supports
 * a pending ClipboardItem (Safari requires that); elsewhere the text is fetched first.
 */
export async function copyText(source: string | (() => Promise<string>)): Promise<void> {
  const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
  if (!clipboard) throw new Error("clipboard unavailable");
  if (typeof source === "string") {
    await clipboard.writeText(source);
    return;
  }
  const text = source();
  if (typeof ClipboardItem !== "undefined" && typeof clipboard.write === "function") {
    const blob = text.then((value) => new Blob([value], { type: "text/plain" }));
    try {
      await clipboard.write([new ClipboardItem({ "text/plain": blob })]);
      return;
    } catch (error: unknown) {
      // Some browsers do not accept a pending blob; they get the loaded text below.
      if (!(error instanceof TypeError)) throw error;
    }
  }
  await clipboard.writeText(await text);
}
