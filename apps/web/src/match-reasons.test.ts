import { describe, expect, it } from "vitest";

import { matchReasons } from "./match-reasons";

describe("match reasons", () => {
  it("names every matched field in the server's order", () => {
    expect(matchReasons(["title", "filename", "tag", "note", "url", "content"])).toBe(
      "Gefunden in: Titel, Dateiname, Tag, Notiz, Link, Inhalt",
    );
    expect(matchReasons(["tag"])).toBe("Gefunden in: Tag");
  });

  it("shows nothing when no field is known, instead of guessing", () => {
    expect(matchReasons([])).toBeNull();
    expect(matchReasons(undefined)).toBeNull();
  });

  it("ignores fields a newer server may add", () => {
    expect(matchReasons(["tag", "future" as never])).toBe("Gefunden in: Tag");
    expect(matchReasons(["future" as never])).toBeNull();
  });
});
