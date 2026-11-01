import { useEffect, useRef, useState } from "preact/hooks";

import { ApiError, type ItemDetail } from "./api";

/** First wait between two refreshes; it grows while nothing changes. */
export const REFRESH_BASE_MS = 3000;
export const REFRESH_MAX_MS = 30000;
/** Items looked up per round, so a long feed never causes a burst of requests. */
export const REFRESH_BATCH = 10;
/** Rounds in a row that fail before the refresh stops and offers a manual retry. */
export const REFRESH_MAX_FAILURES = 5;

export const isPending = (status: string): boolean => status === "queued" || status === "processing";

/** The fields background processing can change. Everything else on a card stays as it is. */
type Refreshable = Pick<ItemDetail, "processing_status" | "thumbnail_available" | "title" | "language">;

function mergeDetail<T extends Refreshable & { id: string }>(row: T, detail: ItemDetail): T {
  if (
    row.processing_status === detail.processing_status &&
    row.thumbnail_available === detail.thumbnail_available &&
    row.title === detail.title &&
    row.language === detail.language
  ) {
    return row;
  }
  return {
    ...row,
    processing_status: detail.processing_status,
    thumbnail_available: detail.thumbnail_available,
    title: detail.title,
    language: detail.language,
  };
}

/** Rows with fresh processing results; the same array (and rows) when nothing changed. */
export function mergeDetails<T extends Refreshable & { id: string }>(rows: T[], details: ItemDetail[]): T[] {
  const byId = new Map(details.map((detail) => [detail.id, detail]));
  let changed = false;
  const merged = rows.map((row) => {
    const detail = byId.get(row.id);
    const next = detail ? mergeDetail(row, detail) : row;
    if (next !== row) changed = true;
    return next;
  });
  return changed ? merged : rows;
}

interface RefreshOptions {
  /** Items still being processed, in display order. */
  ids: string[];
  enabled: boolean;
  fetchOne: (id: string) => Promise<ItemDetail>;
  /** Merges fresh details into the list; returns whether anything changed. */
  apply: (details: ItemDetail[]) => boolean;
  /** Changes whenever the list is replaced; answers that began earlier are discarded. */
  epoch: () => number;
}

/**
 * Looks up the items that are still queued or processing, one round at a time, and stops as soon
 * none is left. Waits while the tab is hidden, backs off while nothing changes or requests fail.
 */
export function usePendingRefresh(options: RefreshOptions): { stalled: boolean; retry: () => void } {
  const latest = useRef(options);
  latest.current = options;
  const inFlight = useRef(false);
  const [stalled, setStalled] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const active = options.enabled && options.ids.length > 0;

  useEffect(() => {
    setStalled(false);
    if (!active) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let idle = 0;
    let failures = 0;
    const gone = new Set<string>();
    let goneEpoch = latest.current.epoch();

    const hidden = () => document.visibilityState === "hidden";
    const delay = () =>
      failures > 0
        ? Math.min(REFRESH_BASE_MS * 2 ** failures, REFRESH_MAX_MS)
        : Math.min(REFRESH_BASE_MS * 1.5 ** idle, REFRESH_MAX_MS);
    const schedule = (ms: number) => {
      clearTimeout(timer);
      timer = alive && !hidden() ? setTimeout(() => void tick(), ms) : undefined;
    };

    const tick = async (): Promise<void> => {
      timer = undefined;
      if (!alive || hidden()) return;
      if (inFlight.current) return schedule(delay());
      const { ids, fetchOne, apply, epoch } = latest.current;
      const started = epoch();
      if (started !== goneEpoch) {
        gone.clear();
        goneEpoch = started;
      }
      const batch = ids.filter((id) => !gone.has(id)).slice(0, REFRESH_BATCH);
      if (batch.length === 0) return;

      inFlight.current = true;
      const results = await Promise.allSettled(batch.map(fetchOne));
      inFlight.current = false;
      if (!alive) return;
      if (epoch() !== started) return schedule(REFRESH_BASE_MS);

      const fresh: ItemDetail[] = [];
      let errors = 0;
      results.forEach((result, index) => {
        if (result.status === "fulfilled") fresh.push(result.value);
        else if (result.reason instanceof ApiError && result.reason.status === 404) gone.add(batch[index]!);
        else errors += 1;
      });

      if (errors === results.length) {
        failures += 1;
        if (failures >= REFRESH_MAX_FAILURES) {
          setStalled(true);
          return;
        }
      } else {
        failures = 0;
        idle = apply(fresh) ? 0 : idle + 1;
      }
      schedule(delay());
    };

    const onVisibility = () => {
      if (hidden()) {
        clearTimeout(timer);
        timer = undefined;
      } else {
        idle = 0;
        schedule(0);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    schedule(REFRESH_BASE_MS);
    return () => {
      alive = false;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [active, attempt]);

  return { stalled, retry: () => setAttempt((count) => count + 1) };
}
