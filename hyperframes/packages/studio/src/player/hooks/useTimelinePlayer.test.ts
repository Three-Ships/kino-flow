import { describe, expect, it } from "vitest";
import {
  buildStandaloneRootTimelineElement,
  mergeTimelineElementsPreservingDowngrades,
  resolveStandaloneRootCompositionSrc,
  shouldIgnorePlaybackShortcutEvent,
  shouldIgnorePlaybackShortcutTarget,
} from "./useTimelinePlayer";

function mockTargetMatching(selectorNeedle: string): EventTarget {
  return {
    closest: (selector: string) => (selector.includes(selectorNeedle) ? ({} as Element) : null),
  } as unknown as EventTarget;
}

function mockKeyboardEvent(
  code: string,
  overrides: Partial<Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "target">> = {},
): Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "code" | "target"> {
  return {
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    code,
    target: mockTargetMatching("[data-missing]"),
    ...overrides,
  };
}

describe("buildStandaloneRootTimelineElement", () => {
  it("includes selector and source metadata for standalone composition fallback clips", () => {
    expect(
      buildStandaloneRootTimelineElement({
        compositionId: "hero",
        tagName: "DIV",
        rootDuration: 8,
        iframeSrc: "http://127.0.0.1:4173/api/projects/demo/preview/comp/scenes/hero.html?_t=123",
        selector: '[data-composition-id="hero"]',
      }),
    ).toEqual({
      id: "hero",
      key: 'scenes/hero.html:[data-composition-id="hero"]:0',
      tag: "div",
      start: 0,
      duration: 8,
      track: 0,
      compositionSrc: "scenes/hero.html",
      selector: '[data-composition-id="hero"]',
      selectorIndex: undefined,
      sourceFile: "scenes/hero.html",
    });
  });

  it("returns null for invalid fallback durations", () => {
    expect(
      buildStandaloneRootTimelineElement({
        compositionId: "hero",
        tagName: "div",
        rootDuration: 0,
        iframeSrc: "http://localhost/preview/comp/hero.html",
      }),
    ).toBe(null);
    expect(
      buildStandaloneRootTimelineElement({
        compositionId: "hero",
        tagName: "div",
        rootDuration: Number.NaN,
        iframeSrc: "http://localhost/preview/comp/hero.html",
      }),
    ).toBe(null);
  });
});

describe("resolveStandaloneRootCompositionSrc", () => {
  it("extracts the composition path from a preview iframe url", () => {
    expect(
      resolveStandaloneRootCompositionSrc(
        "http://127.0.0.1:4173/api/projects/demo/preview/comp/scenes/hero.html?_t=123",
      ),
    ).toBe("scenes/hero.html");
  });

  it("returns undefined for non-composition preview urls", () => {
    expect(
      resolveStandaloneRootCompositionSrc("http://127.0.0.1:4173/api/projects/demo/preview"),
    ).toBe(undefined);
  });
});

describe("mergeTimelineElementsPreservingDowngrades", () => {
  it("preserves missing current elements when a shorter manifest arrives", () => {
    expect(
      mergeTimelineElementsPreservingDowngrades(
        [
          { id: "hero", tag: "div", start: 0, duration: 4, track: 0 },
          { id: "cta", tag: "div", start: 4, duration: 2, track: 1 },
        ],
        [{ id: "hero", tag: "div", start: 0, duration: 4, track: 0 }],
        8,
        8,
      ),
    ).toEqual([
      { id: "hero", tag: "div", start: 0, duration: 4, track: 0 },
      { id: "cta", tag: "div", start: 4, duration: 2, track: 1 },
    ]);
  });

  it("accepts longer-duration or same-size updates as authoritative", () => {
    expect(
      mergeTimelineElementsPreservingDowngrades(
        [{ id: "hero", tag: "div", start: 0, duration: 4, track: 0 }],
        [{ id: "hero", tag: "div", start: 0, duration: 4, track: 0 }],
        4,
        6,
      ),
    ).toEqual([{ id: "hero", tag: "div", start: 0, duration: 4, track: 0 }]);
  });
});

describe("shouldIgnorePlaybackShortcutTarget", () => {
  it("ignores focused toolbar buttons so Space can activate the button itself", () => {
    expect(shouldIgnorePlaybackShortcutTarget(mockTargetMatching("button"))).toBe(true);
  });

  it("ignores the seek slider so ArrowRight reaches the slider key handler", () => {
    expect(shouldIgnorePlaybackShortcutTarget(mockTargetMatching("[role='slider']"))).toBe(true);
  });

  it("allows non-interactive preview targets to use playback shortcuts", () => {
    expect(shouldIgnorePlaybackShortcutTarget(mockTargetMatching("[data-missing]"))).toBe(false);
  });
});

describe("shouldIgnorePlaybackShortcutEvent", () => {
  it("ignores modified playback shortcuts so browser and app chords can handle them", () => {
    expect(
      shouldIgnorePlaybackShortcutEvent(mockKeyboardEvent("ArrowLeft", { altKey: true })),
    ).toBe(true);
    expect(shouldIgnorePlaybackShortcutEvent(mockKeyboardEvent("KeyK", { ctrlKey: true }))).toBe(
      true,
    );
    expect(shouldIgnorePlaybackShortcutEvent(mockKeyboardEvent("KeyL", { metaKey: true }))).toBe(
      true,
    );
  });

  it("defers Arrow frame shortcuts while caption edit mode has selected words", () => {
    const captionSelection = { isCaptionEditMode: true, selectedCaptionSegmentCount: 1 };

    expect(
      shouldIgnorePlaybackShortcutEvent(mockKeyboardEvent("ArrowLeft"), captionSelection),
    ).toBe(true);
    expect(
      shouldIgnorePlaybackShortcutEvent(mockKeyboardEvent("ArrowRight"), captionSelection),
    ).toBe(true);
    expect(shouldIgnorePlaybackShortcutEvent(mockKeyboardEvent("KeyJ"), captionSelection)).toBe(
      false,
    );
  });

  it("allows Arrow frame shortcuts when captions are not selected", () => {
    expect(
      shouldIgnorePlaybackShortcutEvent(mockKeyboardEvent("ArrowRight"), {
        isCaptionEditMode: true,
        selectedCaptionSegmentCount: 0,
      }),
    ).toBe(false);
    expect(
      shouldIgnorePlaybackShortcutEvent(mockKeyboardEvent("ArrowRight"), {
        isCaptionEditMode: false,
        selectedCaptionSegmentCount: 1,
      }),
    ).toBe(false);
  });
});
