/**
 * Stable client_capture_id per logical capture. The server replays a known key
 * with the item it first stored, even for a different payload, so a key is only
 * reused for exactly the same payload and forgotten once the save is confirmed.
 */
export class CaptureKeys {
  private readonly keys = new Map<string, string>();
  private readonly files = new WeakMap<File, number>();
  private nextFile = 0;

  constructor(private readonly newKey: () => string = () => crypto.randomUUID()) {}

  /** Identity of a picked or shared file object; picking a file again is a new capture. */
  file(file: File): string {
    let token = this.files.get(file);
    if (token === undefined) {
      token = this.nextFile++;
      this.files.set(file, token);
    }
    return `file:${token}`;
  }

  /** The key for this payload: the unconfirmed one from an earlier attempt, else a new one. */
  key(...payload: unknown[]): string {
    const fingerprint = JSON.stringify(payload);
    let key = this.keys.get(fingerprint);
    if (key === undefined) {
      key = this.newKey();
      this.keys.set(fingerprint, key);
    }
    return key;
  }

  /** The server confirmed the payload; repeating it later is a deliberate new capture. */
  confirm(...payload: unknown[]): void {
    this.keys.delete(JSON.stringify(payload));
  }
}

/** A file with the note it was first sent with; a retry resends exactly this payload. */
export interface PendingFile {
  file: File;
  note: string | null;
}

export interface FileBatch {
  saved: number;
  /** Files not confirmed, in order: the failed one first, then those not attempted. */
  pending: PendingFile[];
  error: unknown;
}

/** Uploads files in order, stopping at the first failure so it can be retried without the confirmed ones. */
export async function uploadFiles(
  files: PendingFile[],
  keys: CaptureKeys,
  upload: (file: File, note: string | null, key: string) => Promise<unknown>,
): Promise<FileBatch> {
  let saved = 0;
  for (const [index, { file, note }] of files.entries()) {
    const payload = [keys.file(file), note];
    try {
      await upload(file, note, keys.key(...payload));
    } catch (error: unknown) {
      return { saved, pending: files.slice(index), error };
    }
    keys.confirm(...payload);
    saved += 1;
  }
  return { saved, pending: [], error: null };
}
