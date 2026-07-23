/**
 * Comprehensive asset cataloger.
 *
 * Scans rendered HTML and CSS for every referenced asset (images, videos,
 * fonts, icons, stylesheets, backgrounds) and records the HTML context
 * where each was found (e.g., img[src], css url(), link[rel=preload]).
 *
 * This is the programmatic Part 1 of DESIGN.md generation — deterministic
 * extraction, no AI involved.
 */

import type { Page } from "puppeteer-core";

export interface CatalogedAsset {
  url: string;
  type: "Image" | "Video" | "Font" | "Icon" | "Background" | "Other";
  contexts: string[];
  notes?: string;
  /** Alt text, figcaption, or aria-label */
  description?: string;
  /** Nearest heading (h1-h4) text */
  nearestHeading?: string;
  /** Parent section/container class names */
  sectionClasses?: string;
  /** Whether the image is above the fold (visible without scrolling) */
  aboveFold?: boolean;
}

/**
 * Extract all referenced assets from the rendered page with their HTML contexts.
 */
export async function catalogAssets(page: Page): Promise<CatalogedAsset[]> {
  const assets = await page.evaluate(`(() => {
    var assetMap = {};

    // Extract rich DOM context from any element (heading, section, position)
    function getElementContext(el) {
      var ctx = {};
      // Alt text, aria-label, figcaption
      var desc = el.alt || el.getAttribute('aria-label') || el.getAttribute('title') || '';
      var fig = el.closest('figure');
      if (fig) {
        var cap = fig.querySelector('figcaption');
        if (cap) desc = desc || cap.textContent.trim().slice(0, 100);
      }
      var ariaBy = el.getAttribute('aria-describedby');
      if (ariaBy) {
        var descEl = document.getElementById(ariaBy);
        if (descEl) desc = desc || descEl.textContent.trim().slice(0, 100);
      }
      if (desc) ctx.description = desc.slice(0, 150);
      // Nearest heading
      var section = el.closest('section, article, header, footer, main, [class*="hero"], [class*="banner"], [class*="feature"]');
      if (section) {
        var heading = section.querySelector('h1, h2, h3, h4');
        if (heading) ctx.nearestHeading = heading.textContent.trim().slice(0, 80);
        ctx.sectionClasses = (section.className || '').toString().slice(0, 120);
      }
      // Above fold?
      try {
        var rect = el.getBoundingClientRect();
        ctx.aboveFold = rect.top < window.innerHeight;
      } catch(e) {}
      return ctx;
    }

    function add(url, type, context, notes, richCtx) {
      if (!url || url === '' || url.startsWith('data:') || url.startsWith('blob:') || url === 'about:blank') return;
      // Normalize URL
      try { url = new URL(url, document.baseURI).href; } catch(e) { return; }
      // Skip tiny inline data URIs but keep base64 SVGs
      if (url.length > 50000) return;
      // Filter tracking pixels and analytics
      var lurl = url.toLowerCase();
      if (lurl.indexOf('analytics.') > -1 || lurl.indexOf('adsct') > -1 || lurl.indexOf('pixel.') > -1 || lurl.indexOf('tracking.') > -1 || lurl.indexOf('pdscrb.') > -1 || lurl.indexOf('doubleclick') > -1 || lurl.indexOf('googlesyndication') > -1 || lurl.indexOf('facebook.com/tr') > -1 || lurl.indexOf('bat.bing') > -1 || lurl.indexOf('clarity.ms') > -1) return;
      if (lurl.indexOf('bci=') > -1 && lurl.indexOf('twpid=') > -1) return;
      if (lurl.indexOf('cachebust=') > -1 || lurl.indexOf('event_id=') > -1) return;
      // Filter CSS fragment references to SVG filter IDs (not real downloadable assets)
      if (url.indexOf('.css#') > -1) return;
      if (url.indexOf('.css%23') > -1) return;
      // Filter same-page fragment references like "https://site.com/#clip-1"
      try { var parsed = new URL(url); if (parsed.hash && parsed.pathname.length <= 1) return; } catch(e2) {}

      if (!assetMap[url]) {
        assetMap[url] = { url: url, type: type, contexts: [], notes: null };
      }
      var entry = assetMap[url];
      if (entry.contexts.indexOf(context) === -1) {
        entry.contexts.push(context);
      }
      if (notes && !entry.notes) {
        entry.notes = notes;
      }
      // Merge rich context (first one wins)
      if (richCtx) {
        if (richCtx.description && !entry.description) entry.description = richCtx.description;
        if (richCtx.nearestHeading && !entry.nearestHeading) entry.nearestHeading = richCtx.nearestHeading;
        if (richCtx.sectionClasses && !entry.sectionClasses) entry.sectionClasses = richCtx.sectionClasses;
        if (richCtx.aboveFold !== undefined && entry.aboveFold === undefined) entry.aboveFold = richCtx.aboveFold;
      }
    }

    // ── Images: <img src="..."> and <img srcset="..."> ──
    document.querySelectorAll('img[src]').forEach(function(img) {
      var notes = img.alt || img.getAttribute('aria-label') || null;
      var ctx = getElementContext(img);
      add(img.src, 'Image', 'img[src]', notes, ctx);
      if (img.srcset) {
        img.srcset.split(',').forEach(function(entry) {
          var u = entry.trim().split(/\\s+/)[0];
          if (u) add(u, 'Image', 'img[srcset]', notes, ctx);
        });
      }
    });

    // ── Lazy-loaded images: data-src, data-lazy-src, data-original ──
    document.querySelectorAll('img[data-src], img[data-lazy-src], img[data-original], [data-background-image]').forEach(function(el) {
      var dataSrc = el.getAttribute('data-src') || el.getAttribute('data-lazy-src') || el.getAttribute('data-original') || el.getAttribute('data-background-image');
      if (dataSrc) add(dataSrc, 'Image', 'data-src', el.alt || el.getAttribute('aria-label') || null, getElementContext(el));
    });

    // ── CSS background-image on divs (Framer, Webflow, etc.) ──
    document.querySelectorAll('div, section, [class*="hero"], [class*="card"], [class*="image"], [data-framer-background]').forEach(function(el) {
      var bg = getComputedStyle(el).backgroundImage;
      if (bg && bg !== 'none') {
        var match = bg.match(/url\\(["']?(https?:\\/\\/[^"')]+)["']?\\)/);
        if (match && match[1]) {
          add(match[1], 'Background', 'css url()', el.getAttribute('aria-label') || null, getElementContext(el));
        }
      }
    });

    // ── Picture sources: <source srcset="..."> ──
    document.querySelectorAll('source[srcset]').forEach(function(src) {
      src.srcset.split(',').forEach(function(entry) {
        var u = entry.trim().split(/\\s+/)[0];
        if (u) add(u, 'Image', 'source[srcset]', null);
      });
    });

    // ── Videos: <video src="..."> and <video poster="..."> ──
    document.querySelectorAll('video[src]').forEach(function(v) {
      add(v.src, 'Video', 'video[src]', null);
    });
    document.querySelectorAll('video source[src]').forEach(function(s) {
      add(s.src, 'Video', 'video source[src]', null);
    });
    document.querySelectorAll('video[poster]').forEach(function(v) {
      add(v.poster, 'Image', 'video[poster]', null);
    });

    // ── Links: preload, icon, apple-touch-icon, stylesheet ──
    document.querySelectorAll('link[rel]').forEach(function(link) {
      var rel = link.rel.toLowerCase();
      var href = link.href;
      if (!href) return;

      if (rel.includes('preload')) {
        var asType = link.getAttribute('as') || '';
        if (asType === 'font') add(href, 'Font', 'link[rel="preload"]', null);
        else if (asType === 'image') add(href, 'Image', 'link[rel="preload"]', null);
        else if (asType === 'video') add(href, 'Video', 'link[rel="preload"]', null);
        else if (asType === 'style') add(href, 'Other', 'link[rel="preload"]', null);
        else add(href, 'Other', 'link[rel="preload"]', null);
      }
      if (rel.includes('icon')) add(href, 'Icon', 'link[rel="' + rel + '"]', null);
      if (rel === 'apple-touch-icon') add(href, 'Icon', 'link[rel="apple-touch-icon"]', null);
    });

    // ── Meta: og:image, twitter:image ──
    document.querySelectorAll('meta[property="og:image"], meta[content][name="twitter:image"]').forEach(function(m) {
      var content = m.getAttribute('content');
      if (content) {
        var prop = m.getAttribute('property') || m.getAttribute('name') || '';
        add(content, 'Image', 'meta[' + prop + ']', null);
      }
    });

    // ── CSS url() references from all stylesheets ──
    try {
      for (var i = 0; i < document.styleSheets.length; i++) {
        try {
          var sheet = document.styleSheets[i];
          var rules = sheet.cssRules || sheet.rules;
          if (!rules) continue;
          for (var j = 0; j < rules.length; j++) {
            var rule = rules[j];
            var cssText = rule.cssText || '';
            var urlMatches = cssText.match(/url\\(["']?([^"')]+)["']?\\)/g);
            if (urlMatches) {
              urlMatches.forEach(function(m) {
                var u = m.replace(/url\\(["']?/, '').replace(/["']?\\)/, '');
                if (u.startsWith('data:')) return;
                // Classify by file extension
                if (/\\.(woff2?|ttf|otf|eot)$/i.test(u)) {
                  add(u, 'Font', 'css url()', null);
                } else if (/\\.(png|jpg|jpeg|gif|webp|avif|svg)$/i.test(u)) {
                  add(u, 'Background', 'css url()', null);
                } else {
                  add(u, 'Other', 'css url()', null);
                }
              });
            }
          }
        } catch(e) { /* cross-origin stylesheet */ }
      }
    } catch(e) {}

    // ── Inline style url() references ──
    document.querySelectorAll('[style]').forEach(function(el) {
      var style = el.getAttribute('style') || '';
      var urlMatches = style.match(/url\\(["']?([^"')]+)["']?\\)/g);
      if (urlMatches) {
        urlMatches.forEach(function(m) {
          var u = m.replace(/url\\(["']?/, '').replace(/["']?\\)/, '');
          if (u.startsWith('data:')) return;
          if (/\\.(woff2?|ttf|otf|eot)$/i.test(u)) {
            add(u, 'Font', 'html inline style url()', null);
          } else {
            add(u, 'Other', 'html inline style url()', null);
          }
        });
      }
    });

    return Object.values(assetMap);
  })()`);

  const raw = (assets as CatalogedAsset[]) || [];

  // Deduplicate srcset resolution variants — keep highest resolution per base URL
  return deduplicateSrcsetVariants(raw);
}

/**
 * Deduplicate Next.js image variants (same image at different w= sizes).
 * Keeps the highest resolution version and merges contexts.
 */
function deduplicateSrcsetVariants(assets: CatalogedAsset[]): CatalogedAsset[] {
  const byBase = new Map<string, CatalogedAsset>();

  for (const a of assets) {
    // Extract base URL by stripping w= and q= params from _next/image URLs
    let baseKey = a.url;
    try {
      const u = new URL(a.url);
      if (u.pathname.includes("_next/image") || u.searchParams.has("w")) {
        u.searchParams.delete("w");
        u.searchParams.delete("q");
        baseKey = u.toString();
      }
    } catch {
      /* not a valid URL, keep as-is */
    }

    const existing = byBase.get(baseKey);
    if (existing) {
      // Merge contexts
      for (const ctx of a.contexts) {
        if (!existing.contexts.includes(ctx)) {
          existing.contexts.push(ctx);
        }
      }
      // Keep notes from whichever has them
      if (a.notes && !existing.notes) {
        existing.notes = a.notes;
      }
      // Keep the URL with highest w= value (largest image)
      const existingW = getWidthParam(existing.url);
      const newW = getWidthParam(a.url);
      if (newW > existingW) {
        existing.url = a.url;
      }
    } else {
      byBase.set(baseKey, { ...a, contexts: [...a.contexts] });
    }
  }

  return [...byBase.values()];
}

function getWidthParam(url: string): number {
  try {
    const u = new URL(url);
    const w = u.searchParams.get("w");
    return w ? parseInt(w) : 0;
  } catch {
    return 0;
  }
}

/**
 * Format cataloged assets as markdown for the DESIGN.md Assets section.
 * Matches Aura.build's format: grouped by type, named from file paths.
 */
export function formatAssetCatalog(assets: CatalogedAsset[]): string {
  if (assets.length === 0) return "No assets detected.\n";

  // Group by type
  const groups: Record<string, CatalogedAsset[]> = {};
  for (const a of assets) {
    const group = a.type;
    if (!groups[group]) groups[group] = [];
    groups[group]!.push(a);
  }

  const lines: string[] = [];

  // Output in order: Fonts, Images, Videos, Icons, Background, Other
  const order: CatalogedAsset["type"][] = ["Font", "Image", "Video", "Icon", "Background", "Other"];
  for (const type of order) {
    const group = groups[type];
    if (!group || group.length === 0) continue;

    const sectionName =
      type === "Font"
        ? "Fonts"
        : type === "Image"
          ? "Images"
          : type === "Video"
            ? "Videos"
            : type === "Icon"
              ? "Icons"
              : type === "Background"
                ? "Backgrounds"
                : "Other";
    lines.push(`### ${sectionName}`);

    for (const a of group) {
      const name = a.notes || deriveAssetName(a.url);
      const contexts = a.contexts.join(", ");
      lines.push(`- **${name}**: ${a.url} — contexts: ${contexts}`);
    }
    lines.push("");
  }

  return lines.join("\n");
}

/**
 * Derive a human-readable name from a URL's file path.
 * E.g., "ConnectBentoBackground.jpg" → "Connect Bento Background"
 */
function deriveAssetName(url: string): string {
  try {
    const u = new URL(url);
    const path = u.pathname;
    // Get filename without extension
    const filename = path.split("/").pop() || "";
    const nameWithoutExt = filename.replace(/\.[^.]+$/, "");
    // Remove hash suffixes (e.g., "Sohne.cb178166" → "Sohne")
    const cleaned = nameWithoutExt.replace(/\.[a-f0-9]{6,}$/, "");
    // Convert camelCase/PascalCase to spaces
    const spaced = cleaned
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .replace(/[-_]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    return spaced || filename;
  } catch {
    return "Asset";
  }
}
