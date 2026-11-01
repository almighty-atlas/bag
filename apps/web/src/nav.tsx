import { useEffect, useRef, useState } from "preact/hooks";

import { FEED, href, type Route } from "./app";

/** Which entry marks the current page (`aria-current`); null when the page has no entry of its own. */
export function currentEntry(route: Route): "feed" | "organize" | "trash" | "tokens" | null {
  if (route.name === "organize" || route.name === "tokens") return route.name;
  if (route.name === "feed") return route.trashed ? "trash" : "feed";
  return null;
}

/**
 * Inline links on wide screens; on phones a single "Menü" button discloses them. The links stay in
 * the DOM either way (hidden by CSS only when closed), so touch, keyboard and screen readers get
 * the same set, including token management and sign-out.
 */
export function Nav({ route, user, onSignOut }: { route: Route; user: string; onSignOut: () => void }) {
  const [open, setOpen] = useState(false);
  const toggle = useRef<HTMLButtonElement>(null);
  const root = useRef<HTMLElement>(null);
  const current = currentEntry(route);

  useEffect(() => setOpen(false), [route]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      toggle.current?.focus();
    };
    const onPointer = (event: Event) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    addEventListener("keydown", onKey);
    addEventListener("pointerdown", onPointer);
    return () => {
      removeEventListener("keydown", onKey);
      removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  const link = (entry: NonNullable<typeof current>, target: string, label: string) => (
    <a href={target} aria-current={current === entry ? "page" : undefined}>
      {label}
    </a>
  );

  return (
    <nav ref={root} aria-label="Hauptmenü">
      <button
        ref={toggle}
        type="button"
        class="menu-toggle"
        aria-expanded={open}
        aria-controls="nav-links"
        onClick={() => setOpen(!open)}
      >
        <span aria-hidden="true">{open ? "✕" : "☰"}</span> Menü
      </button>
      <div id="nav-links" class={open ? "nav-links open" : "nav-links"}>
        {link("feed", href(FEED), "Feed")}
        {link("organize", href({ name: "organize" }), "Tags & Sammlungen")}
        {link("trash", href({ ...FEED, trashed: true }), "Papierkorb")}
        {link("tokens", href({ name: "tokens" }), "Token")}
        <button type="button" class="link signout" onClick={onSignOut}>
          Abmelden <span class="user">({user})</span>
        </button>
      </div>
    </nav>
  );
}
