import { describe, expect, it } from "vitest";
import { parseHTML } from "linkedom";
import { scopeCssToComposition, wrapScopedCompositionScript } from "./compositionScoping";

describe("composition scoping", () => {
  it("scopes regular selectors while preserving global at-rules", () => {
    const scoped = scopeCssToComposition(
      `
@import url("https://example.com/font.css");
.title, .card:hover { opacity: 0; }
@media (min-width: 800px) {
  .title { transform: translateY(30px); }
}
@keyframes rise {
  from { opacity: 0; }
  to { opacity: 1; }
}
[data-composition-id="scene"] .already { color: red; }
body { margin: 0; }
`,
      "scene",
    );

    expect(scoped).toContain('@import url("https://example.com/font.css");');
    expect(scoped).toContain(
      '[data-composition-id="scene"] .title, [data-composition-id="scene"] .card:hover',
    );
    expect(scoped).toContain('[data-composition-id="scene"] .title { transform');
    expect(scoped).toContain("@keyframes rise");
    expect(scoped).toContain("from { opacity: 0; }");
    expect(scoped).toContain('[data-composition-id="scene"] .already { color: red; }');
    expect(scoped).toContain("body { margin: 0; }");
  });

  it("wraps classic scripts without render-loop requestAnimationFrame waits", () => {
    const wrapped = wrapScopedCompositionScript("window.__ran = true;", "scene");

    expect(wrapped).toContain('var __hfCompId = "scene";');
    expect(wrapped).toContain("new Proxy(window.document");
    expect(wrapped).toContain("new Proxy(__hfBaseGsap");
    expect(wrapped).not.toContain("requestAnimationFrame");
  });

  it("normalizes root timing attributes when scoping selectors", () => {
    const scoped = scopeCssToComposition(
      '[data-composition-id="scene"][data-start="0"] .title { opacity: 0; }',
      "scene",
    );

    expect(scoped).toContain('[data-composition-id="scene"] .title { opacity: 0; }');
    expect(scoped).not.toContain('[data-start="0"]');
  });

  it("executes document and GSAP selectors inside the composition root", () => {
    const { document } = parseHTML(`
      <div data-composition-id="scene" data-start="intro"><h1 class="title">Scene</h1></div>
      <div data-composition-id="other"><h1 class="title">Other</h1></div>
    `);
    const gsapTargets: string[][] = [];
    const fakeWindow = {
      document,
      __selectedTitle: "",
      __selectedRootTitle: "",
      __timelines: {},
      gsap: {
        timeline: () => ({
          to(targets: Element[]) {
            gsapTargets.push(Array.from(targets).map((target) => target.textContent || ""));
            return this;
          },
        }),
      },
    };
    const wrapped = wrapScopedCompositionScript(
      `
const tl = gsap.timeline({ paused: true });
tl.to('.title', { opacity: 1 });
tl.to('[data-composition-id="scene"][data-start="0"] .title', { opacity: 1 });
window.__selectedTitle = document.querySelector('.title')?.textContent || '';
window.__selectedRootTitle = document.querySelector('[data-composition-id="scene"][data-start="0"] .title')?.textContent || '';
window.__timelines.scene = tl;
`,
      "scene",
    );

    new Function("window", "gsap", wrapped)(fakeWindow, fakeWindow.gsap);

    expect(fakeWindow.__selectedTitle).toBe("Scene");
    expect(fakeWindow.__selectedRootTitle).toBe("Scene");
    expect(gsapTargets).toEqual([["Scene"], ["Scene"]]);
  });
});
